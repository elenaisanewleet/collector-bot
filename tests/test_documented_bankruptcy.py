"""Ответ ``bankrot_person`` из документации — через настоящую карту до отчёта.

Тот же приём, что и в ``test_documented_pledge``: связка собирается целиком и
без подмен, от дословного тела ответа до текста, который увидит оператор.

    дословный ответ из архивной документации
        -> config/field_maps/example_newdb.json (тот самый файл, что в репозитории)
        -> NewDBBankruptcyProvider
        -> IdentityMatcher / Aggregator
        -> render_report + RecoveryScoreEngine

Тело лежит в ``tests/data/newdb_bankruptcy_response.json`` и вырезано из снимка
от 07.02.2026 (страница ``fiz_05-fedresurs_bankrot``, раздел «Пример ответа»)
байт в байт. Правки в нём делают сами тесты и только там, где документация
примера не даёт: активного дела на странице нет, а второй субъект в ``data[]``
не показан ни разу.

Проверяются два подлога, оба — про то, что источник сказал меньше, чем от него
прочитали:

*   состояние процедуры источник отдаёт одной строкой и не всегда узнаваемой;
    непрочитанное состояние, показанное как «завершено», — это «дело закрыто»
    там, где на самом деле «неизвестно»;
*   ИНН, по которому шёл поиск, раньше проставлялся на все строки ответа, и в
    ответе с двумя субъектами дела второго доставались первому.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import BankruptcyStatus, MatchLevel, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import BankruptcyRecord, DebtorReport
from app.domain.scoring import ACTIVE_BANKRUPTCY_PENALTY
from app.providers.fedresurs import NewDBBankruptcyProvider
from app.providers.newdb import NewDBFieldMaps
from app.services.aggregation import Aggregator
from app.services.reporting import render_report
from app.services.scoring import RecoveryScoreEngine

BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"

SHIPPED_MAP = Path("config/field_maps/example_newdb.json")
DOC_RESPONSE = Path(__file__).parent / "data" / "newdb_bankruptcy_response.json"

# Субъект ровно из «Примера запроса» страницы: ИНН оттуда, ФИО — из блока
# commmon того же ответа.
DOCUMENTED_PERSON = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Иванов", first_name="Иван", middle_name="Иванович"),
    inn="270311112222",
)
DOCUMENTED_CASE = "А73-1111/2017"
STRANGERS_CASE = "А40-500100/2024"


def documented_response() -> dict[str, Any]:
    payload = json.loads(DOC_RESPONSE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def subjects_of(response: dict[str, Any]) -> list[dict[str, Any]]:
    """Строки ``data`` — по одной на субъекта, с делами во вложенном массиве."""
    rows = response["results"]["bankrot_person"]["result"]["data"]
    assert isinstance(rows, list)
    return rows


@pytest.fixture
def shipped_maps() -> NewDBFieldMaps:
    return NewDBFieldMaps.load(SHIPPED_MAP)


@pytest.fixture
def shipped_settings(live_settings: Settings) -> Settings:
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_field_map": SHIPPED_MAP,
            "provider_max_retries": 0,
            "provider_retry_backoff_seconds": 0.0,
        }
    )


async def bankruptcy_report(
    settings: Settings,
    maps: NewDBFieldMaps,
    response: dict[str, Any],
    subject: SearchSubject = DOCUMENTED_PERSON,
) -> DebtorReport:
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=response))
    result = await NewDBBankruptcyProvider(settings, maps).fetch(subject)
    report = Aggregator().build(subject, [result])
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


def case(report: DebtorReport, number: str) -> BankruptcyRecord:
    return next(item for item in report.bankruptcies if item.case_number == number)


def factor_names(report: DebtorReport) -> set[str]:
    assert report.recovery_score is not None
    return {factor.name for factor in report.recovery_score.factors}


# --------------------------------------------------------- завершённое дело


@respx.mock
async def test_the_documented_case_reaches_the_report_with_its_own_identity(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Кто должник — сказано ответом, а не вопросом.

    ФИО и ИНН лежат в ``data[].commmon``, рядом с массивом дел, и читаются
    ``row_fields``. Пока их никто не читал, запись держалась на ИНН, по которому
    шёл поиск, — на записи стояло то, что мы сами в неё и вписали.
    """
    report = await bankruptcy_report(shipped_settings, shipped_maps, documented_response())

    record = case(report, DOCUMENTED_CASE)
    assert record.debtor_name == "Иванов Иван Иванович"
    assert record.inn == "270311112222"
    assert record.status is BankruptcyStatus.COMPLETED
    assert record.match_level is MatchLevel.CONFIRMED

    text = render_report(report)
    assert DOCUMENTED_CASE in text
    assert "завершено" in text
    assert factor_names(report) == {"completed_bankruptcy"}


# ------------------------------------------------- непрочитанное состояние


@respx.mock
async def test_an_unread_procedure_state_is_never_shown_as_completed(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Единственная строка состояния, и та незнакомая, — это «неизвестно».

    В документации активного дела нет: показанное дело завершено. Поэтому
    формулировка здесь подменена на живую («Рассматривается…»), и подменена
    сознательно — угадывать, какими словами источник пишет идущую процедуру, мы
    не имеем права, а обязаны только не выдавать незнакомую за завершение.

    Печаталось же ровно оно: ``"активно" if is_active else "завершено"``. У
    флага два значения, у состояния три, и лишнее состояние сваливалось в то,
    которое взыскатель читает как «путь свободен».
    """
    response = copy.deepcopy(documented_response())
    subjects_of(response)[0]["bankruptcy"][0]["status"] = "Рассматривается в первой инстанции"

    report = await bankruptcy_report(shipped_settings, shipped_maps, response)

    assert case(report, DOCUMENTED_CASE).status is BankruptcyStatus.UNKNOWN

    text = render_report(report)
    assert "состояние процедуры не определено" in text
    assert "завершено" not in text


@respx.mock
async def test_an_unread_procedure_state_costs_the_same_as_a_running_one(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Скидку в 25 баллов за молчание источника должник не получает.

    Разница между активным банкротством и завершённым — это и есть скидка, и
    непрочитанное состояние попадало в дешёвую половину. Своё имя у фактора
    тоже не для красоты: «идёт процедура» — это утверждение, которого мы делать
    не можем, и вердикт по нему требование в реестр не отправляет.
    """
    response = copy.deepcopy(documented_response())
    subjects_of(response)[0]["bankruptcy"][0]["status"] = "Рассматривается в первой инстанции"

    report = await bankruptcy_report(shipped_settings, shipped_maps, response)
    score = report.recovery_score
    assert score is not None

    assert factor_names(report) == {"bankruptcy_state_unknown"}
    factor = score.factors[0]
    assert factor.delta == ACTIVE_BANKRUPTCY_PENALTY
    assert "не сообщил" in factor.reason
    # Утверждения об идущей процедуре в вердикте нет: активной её никто не
    # объявлял, дело просто ушло человеку из-за низкой перспективы.
    assert report.active_bankruptcies == []


# ------------------------------------------------------------ второй субъект


@respx.mock
async def test_a_second_subject_in_the_answer_keeps_its_own_cases(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Чужое дело не становится нашим оттого, что приехало в одном ответе.

    ``data`` — массив субъектов, и документация обещает, что их может быть
    больше одного. Пока ИНН на каждую строку проставлял код, дело постороннего
    человека получало ИНН должника, доходило до «возможного совпадения» и
    печаталось в его отчёте как его банкротство.
    """
    response = copy.deepcopy(documented_response())
    stranger = copy.deepcopy(subjects_of(response)[0])
    stranger["commmon"]["name_or_fio"] = "Петров Пётр Петрович"
    stranger["commmon"]["inn"] = "500100732259"
    stranger["bankruptcy"] = [
        {
            "case_number": STRANGERS_CASE,
            "case_url": "/legalcases/00000000-0000-0000-0000-000000000000",
            "status": "Производство по делу завершено",
        }
    ]
    subjects_of(response).append(stranger)

    report = await bankruptcy_report(shipped_settings, shipped_maps, response)

    ours = case(report, DOCUMENTED_CASE)
    theirs = case(report, STRANGERS_CASE)
    assert ours.match_level is MatchLevel.CONFIRMED
    assert theirs.inn == "500100732259"
    assert theirs.match_level is MatchLevel.WEAK
    assert "ИНН не совпадает" in theirs.match_reasons

    text = render_report(report)
    assert DOCUMENTED_CASE in text
    assert STRANGERS_CASE not in text


@respx.mock
async def test_a_container_without_identity_is_reported_unchecked_not_assumed(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Дела есть, а чьи они — не прочитано. Это «не проверено», а не «его».

    Опечатка вендора (``commmon`` с тремя «m») — часть контракта ровно до того
    дня, когда её починят. Пока карта дотягивалась до блока, ФИО, ИНН и дата
    рождения приезжали из ответа; после переименования все три пути промахнутся
    молча, и код припишет делам ИНН, по которому шёл поиск, — в ответе с двумя
    субъектами это отдаёт дела второго первому.

    Раньше этот тест требовал обратного: считать такие дела делами должника.
    Тогда живого ответа никто не видел; теперь он есть, ``commmon`` в нём стоит
    у каждой непустой строки, и пропажа всего блока перестала быть нормой,
    которую нужно переживать молча.
    """
    response = copy.deepcopy(documented_response())
    del subjects_of(response)[0]["commmon"]

    report = await bankruptcy_report(shipped_settings, shipped_maps, response)

    result = report.provider_results[0]
    assert not result.status.is_answered
    assert result.error_code == "unexpected_schema"
    assert not report.bankruptcies
    # И никакого «банкротство не обнаружено» в оценке.
    assert "no_bankruptcy" not in factor_names(report)


@respx.mock
async def test_no_date_is_printed_for_dates_the_source_never_sent(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Дат в ответе нет вовсе — и в отчёте их тоже нет.

    Источник не отдаёт ни процедуры, ни дат начала и окончания; относительную
    ссылку он отдаёт, и она доезжает кликабельной. «Начало» и «Завершение»
    держатся на значениях, а не на заголовках, поэтому пустой процедуре
    соответствует общее «процедура банкротства» — и ни одной придуманной даты.
    """
    report = await bankruptcy_report(shipped_settings, shipped_maps, documented_response())

    record = case(report, DOCUMENTED_CASE)
    assert record.procedure is None
    assert record.started_at is None and record.completed_at is None
    assert record.source_url == (
        "https://fedresurs.ru/legalcases/11111111-2222-4333-8444-555555555555"
    )

    text = render_report(report)
    assert "Начало:" not in text
    assert "Завершение:" not in text


def test_the_saved_body_is_the_one_the_docs_show() -> None:
    """Страховка от «причёсывания» файла с телом ответа.

    Ценность ``tests/data/newdb_bankruptcy_response.json`` в том, что его писали
    не мы. Опечатка вендора в ``commmon`` — часть контракта, и карта полей ходит
    именно по ней; исправленная «на глаз», она увела бы за собой карту.
    """
    row = subjects_of(documented_response())[0]

    assert "commmon" in row and "common" not in row
    assert row["commmon"]["inn"] == DOCUMENTED_PERSON.inn
    assert [item["case_number"] for item in row["bankruptcy"]] == [DOCUMENTED_CASE]
    # Метод зовётся не так, как страница: снимок озаглавлен fedresurs_bankrot,
    # а его же пример ответа — и живой API — знают bankrot_person.
    assert documented_response()["params"]["method"] == "bankrot_person"
