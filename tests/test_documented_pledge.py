"""Ответы NewDB из документации — через настоящую карту полей и до текста отчёта.

Каждый другой тест про залоги подаёт строки, придуманные под карту, которую тот
же тест и написал. Так проверяется адаптер, но не проверяется главное: доедет ли
до оператора запись, которую источник действительно прислал.

Здесь связка собрана целиком и без подмен:

    дословный ответ из архивной документации
        -> config/field_maps/example_newdb.json (тот самый файл, что в репозитории)
        -> NewDBPledgeProvider
        -> IdentityMatcher / Aggregator
        -> render_report + RecoveryScoreEngine

Тела ответов лежат в ``tests/data/newdb_pledge_*_response.json`` и скопированы из
снимка документации от 07.02.2026 байт в байт: раздел «Пример ответа» страниц
``fiz_06-pledge_person`` и ``property_03-pledge_vin``. Их не следует
«причёсывать» — ценность файлов именно в том, что их писали не мы.

Ловится этим два подлога, и оба — «НАЙДЕНО, показанное как НЕ НАЙДЕНО»:

*   ФНП печатает залогодателя как «ИМЯ ОТЧЕСТВО ФАМИЛИЯ», и позиционное
    сравнение ФИО читало «СЕРГЕЙ АНДРЕЕВИЧ ПЕТРОВ» как чужого человека;
*   поиск по VIN не возвращает ни ИНН, ни даты рождения, и уведомление о залоге
    оставалось слабым совпадением, хотя спрашивали про конкретную машину.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import MatchLevel, ProviderName, ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject, VehicleDescriptor
from app.domain.models import DebtorReport, PledgeRecord
from app.providers.newdb import NewDBFieldMaps
from app.providers.pledge import NewDBPledgeProvider
from app.services.aggregation import Aggregator
from app.services.reporting import render_report
from app.services.scoring import RecoveryScoreEngine

BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"

SHIPPED_MAP = Path("config/field_maps/example_newdb.json")
DOC_RESPONSES = Path(__file__).parent / "data"

# Субъект ровно из «Примера запроса» страницы pledge_person.
DOCUMENTED_PERSON = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Петров", first_name="Сергей", middle_name="Андреевич"),
    birth_date=date(1985, 5, 10),
)
# VIN ровно из «Примера запроса» страницы pledge_vin.
DOCUMENTED_VIN = SearchSubject(
    search_type=SearchType.VIN.value,
    vehicle=VehicleDescriptor(vin="XWEHD21A800000017"),
)


def documented_response(name: str) -> dict[str, Any]:
    payload = json.loads(
        (DOC_RESPONSES / f"newdb_{name}_response.json").read_text(encoding="utf-8")
    )
    assert isinstance(payload, dict)
    return payload


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


async def pledge_report(
    settings: Settings,
    maps: NewDBFieldMaps,
    subject: SearchSubject,
    response: dict[str, Any],
) -> DebtorReport:
    """Полный путь ответа: провайдер -> матчер -> отчёт со скорингом."""
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=response))
    result = await NewDBPledgeProvider(settings, maps).fetch(subject)
    report = Aggregator().build(subject, [result])
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


def pledges_of(report: DebtorReport) -> list[PledgeRecord]:
    return report.pledges


# ------------------------------------------------------- поиск по человеку


@respx.mock
async def test_documented_pledge_person_response_reaches_the_report(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Залог из документации виден в отчёте, а не «не найден».

    Порядок слов у ФНП — «ИМЯ ОТЧЕСТВО ФАМИЛИЯ». Пока сравнение было
    позиционным, эта запись получала 0.0 за ФИО, с бонусом за дату рождения
    доходила до 0.30, объявлялась слабым совпадением и выпадала из отчёта, —
    после чего скоринг начислял плюс за незаложенное имущество. Найденный залог
    превращался в бонус к взыскаемости.
    """
    report = await pledge_report(
        shipped_settings, shipped_maps, DOCUMENTED_PERSON, documented_response("pledge_person")
    )

    assert report.result_for(ProviderName.PLEDGE).status is ProviderStatus.SUCCESS  # type: ignore[union-attr]
    record = pledges_of(report)[0]
    assert record.pledgor_name == "СЕРГЕЙ АНДРЕЕВИЧ ПЕТРОВ"
    assert record.pledgor_birth_date == date(1985, 5, 10)
    assert record.is_active
    # Дата рождения совпала, ФИО — тоже, порядок слов теперь ни при чём.
    assert record.match_level is MatchLevel.CONFIRMED
    assert "полное совпадение ФИО" in record.match_reasons

    text = render_report(report)
    assert "Записей в реестре залогов не найдено" not in text
    assert "2025-012-232030-634" in text
    assert 'АКЦИОНЕРНОЕ ОБЩЕСТВО "АЛЬФА-БАНК"' in text
    assert "Зарегистрирован: 24.11.2025" in text


@respx.mock
async def test_two_notices_the_answer_never_parsed_are_not_silently_dropped(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """У этого же ответа ``fnp_urls`` длиннее ``fnp``, и разница молчала.

    В примере из документации три ссылки на уведомления и одно разобранное
    уведомление. Показать одно и промолчать про два — короткий список,
    неотличимый от полного; ровно то, против чего написан весь этот модуль.
    Заметили это на живом ответе, где ``fnp`` оказался пустым при тринадцати
    ссылках, — а лежало оно и здесь, только мягче.
    """
    response = documented_response("pledge_person")
    row = response["results"]["pledge_person"]["result"]["data"][0]
    assert len(row["fnp"]) == 1 and len(row["fnp_urls"]) == 3

    report = await pledge_report(shipped_settings, shipped_maps, DOCUMENTED_PERSON, response)

    result = report.result_for(ProviderName.PLEDGE)
    assert result is not None
    assert result.is_partial

    text = render_report(report)
    # Разобранное уведомление по-прежнему на месте — неполнота не отменяет находку.
    assert "2025-012-232030-634" in text
    assert "Ещё 2 уведомления" in text
    for url in row["fnp_urls"][1:]:
        assert url in text


@respx.mock
async def test_documented_pledge_person_lowers_the_score_instead_of_raising_it(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Обратная сторона того же дефекта: спрятанный залог приносил плюс."""
    report = await pledge_report(
        shipped_settings, shipped_maps, DOCUMENTED_PERSON, documented_response("pledge_person")
    )
    score = report.recovery_score
    assert score is not None
    names = {factor.name for factor in score.factors}

    assert "active_pledge" in names
    assert "no_pledges" not in names
    assert all("не обременено" not in factor.reason for factor in score.factors)


# ------------------------------------------------------------ поиск по VIN


@respx.mock
async def test_documented_pledge_vin_response_reaches_the_report(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Точный VIN в запросе обязан давать видимую запись.

    Ответ ``pledge_vin`` не содержит ни ИНН, ни даты рождения, а залогодателя
    печатает как «Игорь Юрьевич Семенов» — субъекта с таким именем мы вообще не
    задавали. По обычным правилам запись осталась бы слабым совпадением и
    исчезла. Идентификатор здесь принесён запросом: спрашивали про конкретные
    17 символов, и уведомление отвечает про них же.
    """
    report = await pledge_report(
        shipped_settings, shipped_maps, DOCUMENTED_VIN, documented_response("pledge_vin")
    )

    record = pledges_of(report)[0]
    assert record.vin == "XWEHD21A800000017"
    assert record.pledgor_name == "Игорь Юрьевич Семенов"
    assert record.is_usable
    assert "совпадает VIN, по которому шёл поиск" in record.match_reasons

    text = render_report(report)
    assert "Записей в реестре залогов не найдено" not in text
    assert "2015-000-291842-833" in text
    assert "ИНТЕРПРОГРЕССБАНК" in text


@respx.mock
async def test_a_vin_inside_a_list_of_subject_numbers_still_carries_the_record(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Предмет залога описан перечнем номеров, а не одним VIN.

    Живая форма поля: «XUS22270280002514, 15218-1, 15274-1» — VIN и заводские
    номера навесного оборудования одной строкой. Целиком это не VIN ни по
    длине, ни по алфавиту, ``normalize_vin`` возвращала ``None``, и запись
    теряла единственный идентификатор: уведомление про ТУ САМУЮ машину, про
    которую спрашивали, становилось слабым совпадением и уходило из отчёта.
    """
    response = copy.deepcopy(documented_response("pledge_vin"))
    rows = response["results"]["pledge_vin"]["result"]["data"]
    rows[0]["fnp"][0]["pledge_subject_ids_raw"] = "XWEHD21A800000017, 15218-1, 15274-1"

    report = await pledge_report(shipped_settings, shipped_maps, DOCUMENTED_VIN, response)

    record = pledges_of(report)[0]
    assert record.vin == "XWEHD21A800000017"
    assert record.is_usable
    assert "совпадает VIN, по которому шёл поиск" in record.match_reasons


@respx.mock
async def test_subject_numbers_that_hold_no_vin_are_kept_as_they_came(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Перечень без VIN остаётся описанием предмета, а не исчезает.

    Отождествление по VIN на нём не сработает — и не должно; но выбрасывать
    единственное описание заложенной вещи нельзя.
    """
    response = copy.deepcopy(documented_response("pledge_vin"))
    rows = response["results"]["pledge_vin"]["result"]["data"]
    rows[0]["fnp"][0]["pledge_subject_ids_raw"] = "15218-1, 15274-1"

    report = await pledge_report(shipped_settings, shipped_maps, DOCUMENTED_VIN, response)

    record = pledges_of(report)[0]
    assert record.vin == "15218-1, 15274-1"
    assert "совпадает VIN, по которому шёл поиск" not in record.match_reasons


@respx.mock
async def test_a_pledge_on_another_vin_is_not_carried_by_the_query(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Пол уверенности даётся запросом, а не записью — иначе это дыра.

    Уведомление про другую машину не становится сильным совпадением оттого, что
    рядом искали по VIN: совпасть должен именно тот VIN, который спросили.
    """
    response = copy.deepcopy(documented_response("pledge_vin"))
    rows = response["results"]["pledge_vin"]["result"]["data"]
    rows[0]["fnp"][0]["pledge_subject_ids_raw"] = "XW8ZZZCKZMG012344"

    report = await pledge_report(shipped_settings, shipped_maps, DOCUMENTED_VIN, response)

    record = pledges_of(report)[0]
    assert record.match_level is MatchLevel.WEAK
    assert "совпадает VIN, по которому шёл поиск" not in record.match_reasons


# ------------------------------------------------- непрочитанная ветка Федресурса


@respx.mock
async def test_a_leasing_only_debtor_is_never_called_unencumbered(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    """Пустой ФНП при непустом Федресурсе — «в ФНП не найдено», и только.

    Ответ ``pledge_person`` несёт две ветки, а одна запись карты описывает один
    набор строк, поэтому читается только ``fnp``. Должник, у которого есть
    договор лизинга и нет уведомлений ФНП, честно вернётся как NO_RESULTS — но
    ни отчёт, ни скоринг не имеют права превратить это в «имущество не
    обременено»: в ответе, который мы только что получили, лизинг лежит рядом.
    """
    response = copy.deepcopy(documented_response("pledge_person"))
    rows = response["results"]["pledge_person"]["result"]["data"]
    rows[0]["fnp"] = []
    assert rows[0]["fedresurs"], "выборка теряет смысл без непрочитанной ветки"

    report = await pledge_report(shipped_settings, shipped_maps, DOCUMENTED_PERSON, response)

    assert report.result_for(ProviderName.PLEDGE).status is ProviderStatus.NO_RESULTS  # type: ignore[union-attr]
    assert pledges_of(report) == []

    text = render_report(report)
    assert "не обременено" not in text
    assert "Проверен только реестр уведомлений ФНП" in text

    score = report.recovery_score
    assert score is not None
    assert all("не обременено" not in factor.reason for factor in score.factors)
