"""Розыск МВД.

Источник отвечает на самый дорогой вопрос отчёта — есть ли смысл подавать, — и
у него есть способ соврать молча: страница МВД иногда просит капчу, и ответ
приходит пустым при ``captcha_error: true``. Для читающего только строки это
неотличимо от «в розыске не значится». Ради этой одной ветки источник и разбирает
признаки уровня результата, а не одну ``data``.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import ProviderName, ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import WantedRecord
from app.providers.newdb import NewDBFieldMaps
from app.providers.wanted import NEWDB_METHOD, NewDBWantedProvider, _params_for

BASE_URL = "https://newdb.example.test"
NEWDB_URL = f"{BASE_URL}/v2"

SUBJECT = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
    birth_date=date(1985, 3, 12),
)

ROW = {
    "full_name": "ТЕСТОВ АНДРЕЙ СЕРГЕЕВИЧ",
    "birth_date": "12.03.1985",
    "birth_date_match": True,
    "match": "exact",
    "wanted_region": "УМВД РОССИИ ПО ПРИМЕРНОМУ РЕГИОНУ",
    "wanted_reason": "разыскивается по статье УК",
    "details": "пол: МУЖ, дата рождения: 12.03.1985",
    "source": "https://xn--b1aew.xn--p1ai/wanted",
}


def envelope(
    *,
    data: list[dict[str, Any]] | None = None,
    captcha_error: bool = False,
    total_found: int | None = None,
) -> dict[str, Any]:
    """Конверт в той форме, в какой его описывает спецификация поставщика."""
    result: dict[str, Any] = {
        "status": 200,
        "found": bool(data),
        "captcha_error": captcha_error,
        "data": list(data or []),
    }
    if total_found is not None:
        result["total_found"] = total_found
    return {
        "state": "complete",
        "requestId": "00000000-0000-4000-8000-000000000001",
        "results": {NEWDB_METHOD: {"result": result}},
    }


@pytest.fixture
def settings(live_settings: Settings) -> Settings:
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 2,
            "newdb_poll_interval_seconds": 0.01,
            "provider_max_retries": 0,
            "newdb_wanted_email": "owner@example.test",
        }
    )


@pytest.fixture
def maps() -> NewDBFieldMaps:
    return NewDBFieldMaps.load(Path("config/field_maps/example_newdb.json"))


@respx.mock
async def test_a_captcha_is_not_an_empty_registry(settings: Settings, maps: NewDBFieldMaps) -> None:
    """Худшая инверсия, какая возможна в этом продукте, и она закрыта здесь.

    Источник ходит на публичную страницу МВД, та просит капчу, и ответ приходит
    с пустой ``data`` при ``captcha_error: true``. Кто читает только строки,
    увидит «в розыске не значится» — то есть «можно подавать» — про человека, в
    реестр которого никто не заглядывал.
    """
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=envelope(captcha_error=True)))

    result = await NewDBWantedProvider(settings, maps).fetch(SUBJECT)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "captcha"
    assert not result.status.is_answered, "капча прочиталась как ответ источника"
    assert result.records == []


@respx.mock
async def test_an_empty_registry_is_an_honest_nothing(
    settings: Settings, maps: NewDBFieldMaps
) -> None:
    """А настоящий пустой ответ остаётся пустым ответом.

    Разница с веткой выше — одно поле, и обе ветки нужны: без этой проверки
    «закрыть капчу» можно было бы, объявив недоступным любой пустой ответ.
    """
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=envelope()))

    result = await NewDBWantedProvider(settings, maps).fetch(SUBJECT)

    assert result.status is ProviderStatus.NO_RESULTS
    assert result.status.is_answered
    assert result.records == []


@respx.mock
async def test_a_match_is_read_with_the_reason_and_the_region(
    settings: Settings, maps: NewDBFieldMaps
) -> None:
    """Найденная запись несёт то, ради чего её читают: кто, за что и где."""
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope(data=[ROW], total_found=1))
    )

    result = await NewDBWantedProvider(settings, maps).fetch(SUBJECT)

    assert result.status is ProviderStatus.SUCCESS
    assert len(result.records) == 1
    record = result.records[0]
    assert isinstance(record, WantedRecord)
    assert record.provider is ProviderName.WANTED
    assert record.full_name == "ТЕСТОВ АНДРЕЙ СЕРГЕЕВИЧ"
    assert record.birth_date == date(1985, 3, 12)
    assert record.birth_date_match is True
    assert record.reason == "разыскивается по статье УК"
    assert "ПРИМЕРНОМУ РЕГИОНУ" in (record.region or "")


@respx.mock
async def test_the_source_saying_it_found_more_makes_the_answer_partial(
    settings: Settings, maps: NewDBFieldMaps
) -> None:
    """«Нашли троих, показали одного» обязано быть видно.

    Источник сам сообщает ``total_found``. Молча показать меньше, чем он нашёл,
    в этом разделе значит спрятать вторую запись про того же человека — а
    именно она может оказаться подтверждённой.
    """
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope(data=[ROW], total_found=3))
    )

    result = await NewDBWantedProvider(settings, maps).fetch(SUBJECT)

    assert result.is_partial, "неполный ответ выдан за полный"
    assert any("3" in note for note in result.notes)


async def test_without_a_birth_date_the_source_is_not_queried(
    settings: Settings, maps: NewDBFieldMaps
) -> None:
    """Без даты рождения запрос не уходит вовсе.

    МВД ищет по строке имени: без даты рождения ответ будет списком тёзок, а
    отличить должника от них будет нечем. Платить за такой ответ незачем, и
    отчёт обязан сказать, чего не хватило, а не «в розыске не значится».
    """
    subject = SUBJECT.model_copy(update={"birth_date": None})

    result = await NewDBWantedProvider(settings, maps).fetch(subject)

    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered


def test_the_shipped_map_describes_the_fields_the_code_reads() -> None:
    """Карта полей и код читают одни и те же ключи.

    Ключи взяты из примера ответа в OpenAPI-документе поставщика и живьём ещё
    не сверены — тем важнее, чтобы файл и код не разъехались между собой.
    """
    mapping = NewDBFieldMaps.load(Path("config/field_maps/example_newdb.json")).require(
        NEWDB_METHOD
    )

    assert set(mapping.field_map.fields) >= {
        "full_name",
        "birth_date",
        "birth_date_match",
        "region",
        "reason",
        "source",
    }


# ------------------------------------------------- контракт запроса


def test_the_request_carries_the_email_the_contract_demands() -> None:
    """Без ``email`` поставщик отвечает HTTP 400.

    Снято с прода: источник падал с ``http_error`` на каждом должнике, потому
    что параметр обязателен — публичная форма МВД без адреса почты не
    отправляется. Тест держит именно контракт, а не наши представления о нём.

    ``country`` не отправляется: в схеме этого метода его нет вовсе, в отличие
    от соседних. Единого набора параметров у поставщика не существует, и
    общий помощник здесь был как раз причиной отказа.
    """
    params = _params_for(SUBJECT, "owner@example.test")

    assert params["email"] == "owner@example.test"
    assert params["lastname"] == "Тестов"
    assert params["firstname"] == "Андрей"
    assert params["secondname"] == "Сергеевич"
    # Только ISO: живой сервис отвергает DD.MM.YYYY, хотя спецификация обещает
    # оба формата. Проверяется формат, а не наличие ключа, — именно на нём
    # источник падал с HTTP 400 на каждом должнике.
    assert params["dob"] == "1985-03-12"
    assert "country" not in params


def test_a_missing_patronymic_is_omitted_not_sent_empty() -> None:
    """Пустое отчество поставщик отбивает — ключа быть не должно вовсе."""
    nameless = SUBJECT.model_copy(
        update={"name": PersonName(last_name="Тестов", first_name="Андрей")}
    )

    assert "secondname" not in _params_for(nameless, "owner@example.test")


async def test_without_an_email_the_source_says_it_is_not_connected(
    settings: Settings, maps: NewDBFieldMaps
) -> None:
    """Пустая настройка — «не подключено», а не ошибка HTTP.

    Разница не косметическая: «не подключено» оператор чинит сам и видит, чем
    именно, а ``http_error`` выглядит как наша поломка, приходит к нам и при
    этом списывает деньги за каждый отбитый вызов.
    """
    provider = NewDBWantedProvider(settings.model_copy(update={"newdb_wanted_email": ""}), maps)

    assert not provider.is_configured

    result = await provider.fetch(SUBJECT)
    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert "NEWDB_WANTED_EMAIL" in (result.error_message or "")
