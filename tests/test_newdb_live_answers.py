"""Живые ответы NewDB от 05.09.2026 — через настоящую карту полей и до текста отчёта.

Каждый файл в ``tests/data/newdb_live_*.json`` — это то, что сервис прислал на
самом деле: конверт целиком, ни один ключ не переименован, ни одно значение не
выброшено. Заменены только персональные данные, и только «значение на значение
той же формы» — ФИО на ФИО, двенадцать цифр на двенадцать цифр, дата на дату,
GUID на GUID. Как именно, видно в ``scripts/make_live_fixtures.py``: фикстуры
нарезаны скриптом из захваченных тел, а не набраны руками, потому что набранная
руками фикстура согласуется с тем, что код уже делает.

Собрано без подмен, целиком:

    дословный живой ответ
        -> config/field_maps/example_newdb.json (тот самый файл, что в репозитории)
        -> провайдер источника
        -> IdentityMatcher / Aggregator
        -> render_report + RecoveryScoreEngine

Ловится этим шесть подлогов, и все они — «НАЙДЕНО, показанное как НЕ НАЙДЕНО»:

*   ``arbitr_person`` отдаёт обёртку на запрос, а не дело: архивные пути не
    попадали никуда, и источник «падал» ровно у тех должников, у кого дела есть;
*   он же присылает десять дел из скольки угодно — короткий список был
    неотличим от полного;
*   ``pledge_person`` нашёл тринадцать уведомлений ФНП и не сопоставил ни
    одного: ноль записей читался как чистый реестр и приносил плюс к оценке;
*   ``bankrot_person`` возвращает дело по точному ИНН, но с тем ФИО, что
    записано в ЕФРСБ; без даты рождения из ``commmon`` несовпадение фамилии
    выбрасывало найденное банкротство и добавляло +10 за его отсутствие;
*   ``egrul_ip`` не отдаёт статус у строк физлица — «состояние неизвестно»
    печаталось как «прекращено», и действующее ИП выглядело закрытым;
*   он же присылает одну регистрацию ИП дважды, двумя разделами реестра.

Плюс одна проверка не про инверсию, а про приватность: в ответе о банкротстве
приезжают СНИЛС, место рождения и адрес проживания должника, и ни одно из них
не должно оседать в базе и появляться в отчёте.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import (
    BankruptcyStatus,
    BusinessRole,
    BusinessStatus,
    CourtCaseRole,
    MatchLevel,
    ProviderName,
    ProviderStatus,
    SearchType,
)
from app.domain.identity import PersonName, SearchSubject, VehicleDescriptor
from app.domain.models import DebtorReport, ProviderResult
from app.providers.base import BaseProvider
from app.providers.court import NewDBArbitrationProvider
from app.providers.fedresurs import NewDBBankruptcyProvider, _to_bankruptcy
from app.providers.fns import NewDBBusinessProvider
from app.providers.newdb import NewDBFieldMaps
from app.providers.pledge import NewDBPledgeProvider
from app.services.aggregation import Aggregator
from app.services.reporting import render_report
from app.services.scoring import RecoveryScoreEngine

BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"

SHIPPED_MAP = Path("config/field_maps/example_newdb.json")
LIVE_RESPONSES = Path(__file__).parent / "data"

# Субъекты — те же, по которым шли живые запросы, с подставленными ФИО и ИНН.
BANKRUPT = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Пыжова", first_name="Анна", middle_name="Петровна"),
    birth_date=date(1979, 3, 14),
    inn="270311112222",
)
ENTREPRENEUR = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Парфёнов", first_name="Антон", middle_name="Орестович"),
    inn="770600011111",
)
LITIGANT = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Викторович"),
    inn="644600011111",
)
PLEDGOR = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Петров", first_name="Сергей", middle_name="Андреевич"),
    birth_date=date(1985, 5, 10),
)


def live(name: str) -> dict[str, Any]:
    """Захваченный ответ сервиса — читается заново на каждый тест."""
    payload = json.loads((LIVE_RESPONSES / f"newdb_live_{name}.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def rows_of(response: dict[str, Any], method: str) -> list[Any]:
    data = response["results"][method]["result"]["data"]
    assert isinstance(data, list)
    return data


@pytest.fixture
def shipped_maps() -> NewDBFieldMaps:
    """Карта полей из репозитория, а не написанная под тест."""
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


async def report_of(
    provider: BaseProvider, subject: SearchSubject, response: dict[str, Any]
) -> DebtorReport:
    """Полный путь ответа: провайдер -> матчер -> отчёт со скорингом."""
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=response))
    result = await provider.fetch(subject)
    report = Aggregator().build(subject, [result])
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


def result_of(report: DebtorReport, provider: ProviderName) -> ProviderResult:
    result = report.result_for(provider)
    assert result is not None
    return result


def factor_names(report: DebtorReport) -> set[str]:
    score = report.recovery_score
    assert score is not None
    return {factor.name for factor in score.factors}


def confidence_notes(report: DebtorReport) -> Sequence[str]:
    score = report.recovery_score
    assert score is not None
    return score.confidence_notes


# ============================================================ БАНКРОТСТВО


@respx.mock
async def test_live_bankruptcy_reaches_the_report(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Дело из живого ответа доезжает целиком, вместе с датой рождения из commmon."""
    provider = NewDBBankruptcyProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, BANKRUPT, live("bankrot_person"))

    assert result_of(report, ProviderName.FEDRESURS).status is ProviderStatus.SUCCESS
    record = report.bankruptcies[0]
    assert record.case_number == "А73-1111/2017"
    assert record.debtor_name == "Пыжова Анна Петровна"
    assert record.inn == "270311112222"
    assert record.debtor_birth_date == date(1979, 3, 14)
    # Ссылка живьём приходит абсолютной — склейка хоста её не портит.
    assert record.source_url is not None
    assert record.source_url.startswith("https://fedresurs.ru/legalcases/")
    assert record.status is BankruptcyStatus.COMPLETED
    assert record.match_level is MatchLevel.CONFIRMED

    text = render_report(report)
    assert "Не обнаружено" not in text
    assert "А73-1111/2017" in text


@respx.mock
async def test_a_bankruptcy_found_by_inn_survives_a_different_surname(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Девичья фамилия в ЕФРСБ не должна прятать банкротство.

    Метод адресуется одним лишь ``innfiz``: дело найдено по точному
    идентификатору должника. Но ФИО в реестре — то, под которым его записали, и
    оно может отличаться. Пока дата рождения из ``commmon`` не читалась, такая
    запись набирала 0.00 за ФИО + 0.30 за ИНН, объявлялась слабым совпадением,
    выпадала из отчёта — и скоринг начислял плюс за «банкротство не обнаружено».
    Найденное банкротство превращалось в бонус к взыскаемости.
    """
    subject = BANKRUPT.model_copy(
        update={"name": PersonName(last_name="Смирнова", first_name="Анна", middle_name="Петровна")}
    )
    provider = NewDBBankruptcyProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, subject, live("bankrot_person"))

    record = report.bankruptcies[0]
    assert "совпадает дата рождения" in record.match_reasons
    assert "совпадает ИНН" in record.match_reasons
    assert record.is_usable, "запись, найденная по ИНН и подтверждённая датой рождения"

    text = render_report(report)
    assert "Не обнаружено" not in text
    assert "no_bankruptcy" not in factor_names(report)


@respx.mock
async def test_a_conflicting_birth_date_still_disqualifies_the_case(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Обратная сторона: прочитанная дата рождения обязана и отсеивать.

    Если бы дата только помогала записи выжить, это была бы не проверка
    личности, а поблажка. Другой человек с чужой датой рождения из отчёта
    выпадает — но и плюса за «банкротство не обнаружено» при этом не даёт: мы
    не проверили, а отсеяли.
    """
    subject = BANKRUPT.model_copy(update={"birth_date": date(1990, 1, 1)})
    provider = NewDBBankruptcyProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, subject, live("bankrot_person"))

    record = report.bankruptcies[0]
    assert "дата рождения не совпадает" in record.match_reasons
    assert not record.is_usable
    assert "no_bankruptcy" not in factor_names(report)
    assert "сопоставить с должником не удалось" in render_report(report)


def test_the_state_vocabulary_is_taken_from_the_live_answer() -> None:
    """Словарь состояний обязан покрывать то, что источник действительно шлёт.

    Строка берётся из сохранённого тела, а не набирается здесь: тест на
    литерале проверял бы, что мы согласны сами с собой. Живьём значение ровно
    одно — «Производство по делу завершено», — и цена промаха по нему высока:
    непрочитанное состояние стоит должнику столько же, сколько идущая
    процедура (``UNKNOWN_BANKRUPTCY_STATE_PENALTY``). Пополнять словарь можно
    только тем, что видели в ответе.
    """
    row = rows_of(live("bankrot_person"), "bankrot_person")[0]
    statuses = [case["status"] for case in row["bankruptcy"]]
    assert statuses == ["Производство по делу завершено"]

    for status in statuses:
        parsed = _to_bankruptcy({"status": status})
        assert parsed.status is not BankruptcyStatus.UNKNOWN, (
            f"состояние {status!r} источник прислал, а словарь его не знает"
        )


@respx.mock
async def test_live_empty_bankruptcy_is_an_honest_nothing(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Пустой живой ответ — это контейнер с четырьмя пустыми значениями.

    Не пустой ``data[]``, как обещала документация, и не ошибка карты: разница
    важна, потому что «карта не прочла» обязана давать «не проверено», а этот
    ответ обязан давать честное «не обнаружено».
    """
    response = live("bankrot_person_empty")
    assert rows_of(response, "bankrot_person") == [
        {"bankruptcy": [], "commmon": {}, "encumbrances": [], "publications": []}
    ]

    provider = NewDBBankruptcyProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, BANKRUPT, response)

    result = result_of(report, ProviderName.FEDRESURS)
    assert result.status is ProviderStatus.NO_RESULTS
    assert result.error_code is None
    assert not result.is_partial
    assert "Не обнаружено" in render_report(report)
    assert "no_bankruptcy" in factor_names(report)


# ------------------------------------------------------------- приватность


@respx.mock
async def test_the_bankruptcy_answer_leaves_its_snils_behind(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """СНИЛС, место рождения и адрес проживания не оседают и не печатаются.

    Живой ответ привозит их в ``commmon``, README советует прогнать первого
    должника с ``STORE_RAW_RESPONSES=true`` — и до этой правки совет означал
    «запишите чужой СНИЛС в свою базу». Флаг решает, хранить ли тело; он не
    решает, хранить ли вместе с ним номер страхового свидетельства.
    """
    response = live("bankrot_person")
    common = rows_of(response, "bankrot_person")[0]["commmon"]
    assert common["snils"] and common["birth_place"] and common["residential_address"]

    settings = shipped_settings.model_copy(update={"store_raw_responses": True})
    provider = NewDBBankruptcyProvider(settings, shipped_maps)
    # Оператор искал по ИНН и ФИО, даты рождения не вводил — значит всё, что
    # найдётся в тексте, пришло из ответа, а не из шапки запроса.
    report = await report_of(provider, BANKRUPT.model_copy(update={"birth_date": None}), response)

    raw = result_of(report, ProviderName.FEDRESURS).raw_response
    assert raw is not None, "флаг включён — тело сохраняется"
    assert "А73-1111/2017" in raw, "сохраняется именно ответ, а не заглушка"
    for secret in (common["snils"], common["birth_place"], common["residential_address"]):
        assert secret not in raw
    stored = json.loads(raw)
    stored_common = stored["results"]["bankrot_person"]["result"]["data"][0]["commmon"]
    assert "snils" not in stored_common
    # Ключ не обнуляется, а исчезает — и на его месте остаётся след того, что
    # он был: ``"snils": null`` читалось бы как «вендор ничего не прислал».
    assert stored_common["_redacted_fields"] == [
        "birth_place",
        "residential_address",
        "snils",
    ]

    text = render_report(report)
    for secret in (common["snils"], common["birth_place"], common["residential_address"]):
        assert secret not in text
    # Дата рождения из ответа нужна сопоставлению — и остаётся только в нём.
    assert report.bankruptcies[0].debtor_birth_date == date(1979, 3, 14)
    assert "14.03.1979" not in text


@respx.mock
async def test_nothing_is_stored_at_all_without_the_flag(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    provider = NewDBBankruptcyProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, BANKRUPT, live("bankrot_person"))

    assert result_of(report, ProviderName.FEDRESURS).raw_response is None


# ================================================================= ЕГРИП


@respx.mock
async def test_live_egrul_returns_roles_in_legal_entities_too(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Метод отдаёт не только ИП, что бы ни говорило его имя.

    Живой ответ по одному ИНН: регистрация ИП, руководитель ЮЛ и учредитель.
    Роль закодирована кодом раздела, и без словаря в карте все три стали бы
    «иной ролью».
    """
    provider = NewDBBusinessProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, ENTREPRENEUR, live("egrul_ip"))

    assert result_of(report, ProviderName.FNS).status is ProviderStatus.SUCCESS
    roles = {item.role for item in report.business_relations}
    assert roles == {BusinessRole.SOLE_PROPRIETOR, BusinessRole.DIRECTOR, BusinessRole.FOUNDER}

    sole = next(
        item for item in report.business_relations if item.role is BusinessRole.SOLE_PROPRIETOR
    )
    assert sole.ogrn == "320774600311111"
    assert sole.registration_date == date(2020, 9, 15)

    text = render_report(report)
    assert "Связей с ИП и юрлицами не найдено" not in text
    assert "руководитель ЮЛ" in text
    assert "учредитель ЮЛ" in text


@respx.mock
async def test_the_person_behind_a_registry_row_is_matched_by_name(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """В строках физлица ``name_short`` — это ФИО, и оно должно сопоставляться.

    Раньше ФИО из ЕГРИП вообще не доходило до матчера: имя человека принималось
    только с приставкой «ИП », а живьём оно приходит голым. Запись держалась на
    одном ИНН и стояла ровно на пороге отсева.
    """
    provider = NewDBBusinessProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, ENTREPRENEUR, live("egrul_ip"))

    record = report.business_relations[0]
    assert record.person_name == "ПАРФЁНОВ АНТОН ОРЕСТОВИЧ"
    assert "полное совпадение ФИО" in record.match_reasons
    assert record.match_level is MatchLevel.CONFIRMED


@respx.mock
async def test_a_status_the_source_never_gave_is_not_a_termination(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Живой ``status`` у строк физлица — null во всех записях.

    Печатать это как «прекращено» значит закрыть действующее ИП должника одним
    словом, а в скоринге — выдать штраф за «3 прекращённые бизнес-связи»
    человеку с действующим ИП и двумя ролями в ЮЛ. Знак фактора был обратен
    факту.
    """
    response = live("egrul_ip")
    assert all(row["status"] is None for row in rows_of(response, "egrul_ip")[0]["matches"])

    provider = NewDBBusinessProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, ENTREPRENEUR, response)

    assert all(item.status is BusinessStatus.UNKNOWN for item in report.business_relations)

    text = render_report(report)
    assert "прекращено" not in text
    assert "состояние не определено" in text
    assert "terminated_business_relation" not in factor_names(report)


@respx.mock
async def test_one_registration_named_twice_is_one_registration(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """``matches[]`` — объединение разделов, и ОГРНИП приходит в нём дважды.

    Секция ``docip`` («документы на государственную регистрацию ИП») повторяет
    ту же регистрацию, что и секция ``ip``. Без склейки отчёт печатает два
    одинаковых ИП, а скоринг считает их за две связи.
    """
    response = live("egrul_ip_duplicate_registration")
    sections = [row["section"] for row in rows_of(response, "egrul_ip")[0]["matches"]]
    assert sections == ["ip", "ip", "docip"]

    provider = NewDBBusinessProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, BANKRUPT, response)

    ogrns = sorted(item.ogrn or "" for item in report.business_relations)
    assert ogrns == ["307272011400022", "323270000011111"]


@respx.mock
async def test_live_empty_egrul_is_an_honest_nothing(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    provider = NewDBBusinessProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, ENTREPRENEUR, live("egrul_ip_empty"))

    result = result_of(report, ProviderName.FNS)
    assert result.status is ProviderStatus.NO_RESULTS
    assert not result.is_partial
    assert "Связей с ИП и юрлицами не найдено" in render_report(report)


@respx.mock
async def test_a_truncated_registry_answer_says_so(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """``total_items`` больше числа присланных строк — список ролей неполный.

    Живьём они всегда сходились, поэтому расхождение сделано здесь: живой ответ
    с уменьшенным ``matches[]``. Это единственный тест в файле, чьё тело
    отличается от захваченного, и отличие названо вслух.
    """
    response = copy.deepcopy(live("egrul_ip"))
    row = rows_of(response, "egrul_ip")[0]
    row["matches"] = row["matches"][:1]
    assert row["total_items"] == 3

    provider = NewDBBusinessProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, ENTREPRENEUR, response)

    result = result_of(report, ProviderName.FNS)
    assert result.is_partial
    assert "список ролей неполный" in " ".join(result.notes)
    assert "источник прислал не всё" in " ".join(confidence_notes(report))


# ================================================================ АРБИТРАЖ


@respx.mock
async def test_live_arbitration_case_reaches_the_report(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Дело лежит в ``cases[]`` внутри обёртки, а не в самой строке ``data``.

    Архивные пути отсчитывались на два уровня выше, чем надо: ни один из них не
    попадал никуда, запись считалась неразобранной, и источник отвечал
    «недоступен» у КАЖДОГО должника, у которого дела есть. На прогоне в
    восемьсот строк это читалось как флапающий вендор.
    """
    response = live("arbitr_person")
    wrapper = rows_of(response, "arbitr_person")[0]
    assert "cases" in wrapper and "case_number" not in wrapper

    provider = NewDBArbitrationProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, LITIGANT, response)

    assert result_of(report, ProviderName.COURT).status is ProviderStatus.SUCCESS
    case = report.court_cases[0]
    assert case.case_number == "А57-11111/2025"
    assert case.status == "Рассмотрение дела завершено"
    assert case.court_name == "Огнищева Ю. П. | АС Саратовской области"
    assert case.case_type == "банкротство"
    assert case.filed_at == date(2025, 4, 26)
    assert case.source_url is not None and case.source_url.startswith("https://kad.arbitr.ru/Card/")
    assert case.role is CourtCaseRole.DEFENDANT
    assert case.is_closed
    assert case.is_usable

    text = render_report(report)
    assert "Арбитражных дел не найдено" not in text
    assert "А57-11111/2025" in text


@respx.mock
async def test_a_case_without_a_parsed_card_still_has_a_side(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Роль у дела без разобранной карточки — из плоских ``respondent``/``plaintiff``.

    Вендор сам пишет, сколько дел разобрал подробно («Подробно разобрано 1 из
    текущих 1»), то есть карточки есть не у всех. Списки участников лежат
    внутри карточки; без неё роль оставалась «иной», дело не попадало в иски к
    должнику, и скоринг начислял плюс за «действующих исков не найдено»,
    напечатав это самое дело строкой выше.
    """
    response = copy.deepcopy(live("arbitr_person"))
    case_row = rows_of(response, "arbitr_person")[0]["cases"][0]
    case_row.pop("card")
    case_row["has_details"] = False
    assert case_row["respondent"] == "Тестов Андрей Викторович"

    provider = NewDBArbitrationProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, LITIGANT, response)

    case = report.court_cases[0]
    assert case.role is CourtCaseRole.DEFENDANT
    assert case.is_against_debtor
    assert "no_court_claims" not in factor_names(report)


@respx.mock
async def test_ten_cases_out_of_forty_are_never_shown_as_all_of_them(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """``total_count`` больше присланного — ответ обрезан, и отчёт это говорит.

    Живой ``pagination.limit`` равен десяти независимо от числа дел должника, а
    ``total_count`` считает все. У должника с сорока делами придут десять,
    карта разберёт десять, ``unreadable`` будет ноль — и отчёт напечатает
    десять дел как всё, что есть, тем увереннее ошибаясь, чем хуже должник.
    Счётчики взяты из живой обёртки; изменены только их значения.
    """
    response = copy.deepcopy(live("arbitr_person"))
    wrapper = rows_of(response, "arbitr_person")[0]
    wrapper["total_count"] = 40
    wrapper["pagination"]["has_more"] = True

    provider = NewDBArbitrationProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, LITIGANT, response)

    result = result_of(report, ProviderName.COURT)
    assert result.status is ProviderStatus.SUCCESS
    assert result.is_partial
    assert len(report.court_cases) == 1

    text = render_report(report)
    assert "Источник нашёл 40 арбитражных дел, а прислал 1" in text
    assert "ответ неполный" in text
    assert "источник прислал не всё" in " ".join(confidence_notes(report))


@respx.mock
async def test_found_without_a_single_case_is_not_a_clean_answer(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """``found: true`` при пустом ``cases[]`` — «нашёл и не отдал», а не «нет дел»."""
    response = copy.deepcopy(live("arbitr_person"))
    wrapper = rows_of(response, "arbitr_person")[0]
    wrapper["cases"] = []

    provider = NewDBArbitrationProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, LITIGANT, response)

    result = result_of(report, ProviderName.COURT)
    assert result.is_partial
    text = render_report(report)
    assert "Арбитражных дел не найдено" not in text
    assert "no_court_claims" not in factor_names(report)


@respx.mock
async def test_live_empty_arbitration_is_an_honest_nothing(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """У этого метода пустой ответ — буквально ``data: []``, без обёртки.

    Не как у ``bankrot_person`` и ``pledge_person``, где пустой ``data[]``
    означал бы поломку. Тест держит это различие, чтобы никто не «починил»
    арбитраж под общий с ними шаблон.
    """
    response = live("arbitr_person_empty")
    assert rows_of(response, "arbitr_person") == []

    provider = NewDBArbitrationProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, LITIGANT, response)

    result = result_of(report, ProviderName.COURT)
    assert result.status is ProviderStatus.NO_RESULTS
    assert not result.is_partial
    assert "Арбитражных дел не найдено" in render_report(report)
    assert "no_court_claims" in factor_names(report)


# =================================================================== ЗАЛОГИ


@respx.mock
async def test_thirteen_unmatched_notices_are_not_an_empty_register(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Живой ответ ФНП: ``fnp`` пуст, ``fnp_urls`` — тринадцать уведомлений.

    Реестр ищет по ФИО, а дата рождения, по документации метода, «используется
    для фильтрации релевантных записей»: тринадцать нашлось, ни одно не
    прошло фильтр. До этой правки провайдер отвечал NO_RESULTS, отчёт печатал
    «Записей в реестре залогов не найдено», а скоринг начислял +3 за «в реестре
    уведомлений ФНП действующих залогов не найдено». Тринадцать уведомлений
    превращались в плюс к оценке взыскиваемости.
    """
    response = live("pledge_person_unmatched")
    row = rows_of(response, "pledge_person")[0]
    assert row["fnp"] == [] and len(row["fnp_urls"]) == 13

    provider = NewDBPledgeProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, PLEDGOR, response)

    result = result_of(report, ProviderName.PLEDGE)
    assert result.is_partial
    assert "найдено 13 уведомлений" in " ".join(result.notes)

    text = render_report(report)
    assert "Записей в реестре залогов не найдено" not in text
    assert "13 уведомлений" in text
    assert "требуется ручная проверка" in text or "нужна ручная проверка" in text
    # Ссылки — то, ради чего эта ветка вообще существует: взыскателю есть куда
    # пойти руками.
    assert row["fnp_urls"][0] in text
    # Оговорка о границах источника не пропадает: она про другое.
    assert "Проверен только реестр уведомлений ФНП" in text

    assert "no_pledges" not in factor_names(report)
    score = report.recovery_score
    assert score is not None
    assert all("не найдено" not in factor.reason for factor in score.factors)


@respx.mock
async def test_an_empty_register_is_still_allowed_to_be_empty(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Все три массива пусты — вот это и есть «в ФНП чисто».

    Живой ``pledge_vin`` по несуществующему VIN. Без этого теста починка
    предыдущего случая легко превратилась бы в «залоги никогда не бывают
    чистыми», что так же бесполезно, как и ложный плюс.
    """
    response = live("pledge_vin_empty")
    assert rows_of(response, "pledge_vin") == [{"fnp": [], "fedresurs": [], "fnp_urls": []}]

    # Поиск ровно по VIN, как и был живой запрос: субъект без ФИО и без даты
    # рождения не порождает второго вызова, по человеку.
    subject = SearchSubject(
        search_type=SearchType.VIN.value,
        vehicle=VehicleDescriptor(vin="XTA210990S1234567"),
    )
    provider = NewDBPledgeProvider(shipped_settings, shipped_maps)
    report = await report_of(provider, subject, response)

    result = result_of(report, ProviderName.PLEDGE)
    assert result.status is ProviderStatus.NO_RESULTS
    assert not result.is_partial
    assert "Записей в реестре залогов не найдено" in render_report(report)
    assert "no_pledges" in factor_names(report)
