"""ЕГРН — объект по адресу.

Все тесты здесь стоят вокруг одного различия: источник отвечает про **объект**,
а не про имущество должника. Он не называет правообладателя, по ФИО не ищет, а
на адрес до дома отвечает ошибкой — за уже списанный вызов. Каждое из этих
свойств здесь закреплено, потому что нарушение любого превращает справку об
объекте в утверждение «у должника есть квартира».
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import ProviderStatus, SearchType
from app.domain.identity import SearchSubject
from app.domain.models import PropertyRecord
from app.providers.base import FetchContext
from app.providers.newdb import NewDBFieldMaps
from app.providers.property import NewDBPropertyProvider

BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"
LIVE_FIXTURES = Path(__file__).parent / "fixtures" / "newdb"

FLAT = "Саратовская обл., г. Ртищево, ул. Красная, д.22, кв.10"
HOUSE = "Саратовская обл., г. Ртищево, ул. Красная, д.22"
CADASTRAL = "64:47:040605:229"


def live_fixture(name: str) -> Any:
    return json.loads((LIVE_FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def property_settings(live_settings: Settings) -> Settings:
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 2,
            "newdb_poll_interval_seconds": 0.01,
            "provider_max_retries": 0,
            "provider_retry_backoff_seconds": 0.0,
            "rosreestr_enabled": True,
        }
    )


@pytest.fixture
def provider(property_settings: Settings) -> NewDBPropertyProvider:
    return NewDBPropertyProvider(property_settings, NewDBFieldMaps())


def address_subject(address: str | None) -> SearchSubject:
    return SearchSubject(search_type=SearchType.ADDRESS.value, address=address)


# ---------------------------------------------------------------- гейт входа


@respx.mock
async def test_house_level_address_is_not_queried(provider: NewDBPropertyProvider) -> None:
    """Адрес до дома живьём отвечает 500 с пустой data — и всё равно оплачен.

    Поэтому он не отправляется вовсе: «не хватило данных» стоит ноль, а
    оплаченный отказ стоит вызов и ничего не сообщает.
    """
    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json={}))

    result = await provider.fetch(address_subject(HOUSE))

    assert result.error_code == "insufficient_query"
    assert result.status is not ProviderStatus.NO_RESULTS
    assert route.call_count == 0


@respx.mock
async def test_cadastral_number_is_accepted(provider: NewDBPropertyProvider) -> None:
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("rosreestr.json"))
    )

    result = await provider.fetch(address_subject(CADASTRAL))

    assert result.status is ProviderStatus.SUCCESS
    sent = json.loads(route.calls[0].request.content)["params"]
    assert sent["cadastral_number"] == CADASTRAL


async def test_no_address_is_not_an_empty_register(provider: NewDBPropertyProvider) -> None:
    result = await provider.fetch(address_subject(None))

    assert result.error_code == "insufficient_query"
    assert result.status is not ProviderStatus.NO_RESULTS


# ---------------------------------------------------------------- живой ответ


@respx.mock
async def test_live_response_is_parsed(provider: NewDBPropertyProvider) -> None:
    """Дословный ответ живого сервиса, все пути сняты с него."""
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("rosreestr.json"))
    )

    result = await provider.fetch(address_subject(FLAT))

    assert result.status is ProviderStatus.SUCCESS
    record = result.records[0]
    assert isinstance(record, PropertyRecord)
    assert record.cadastral_number == CADASTRAL
    assert record.cadastral_cost == Decimal("1444067.96")
    assert record.area == "49.50"
    assert record.property_type == "Помещение, Жилое"
    assert record.rights_count == 4
    assert record.shares == ("1/5", "2/5", "1/5", "1/5")
    assert record.encumbrances == ()
    assert record.encumbrances_checked is True
    # Несущее: правообладателя источник не называет и назвать не может.
    assert record.owner_confirmed is False


@respx.mock
async def test_upstream_500_with_empty_data_is_not_no_results(
    provider: NewDBPropertyProvider,
) -> None:
    """Дословный ответ на адрес до дома: 500, пустая data, вечный restart.

    Прочитанный наивно, он означал бы «по адресу объекта нет».
    """
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("rosreestr_upstream_error.json"))
    )

    result = await provider.fetch(address_subject(FLAT))

    assert result.status is not ProviderStatus.NO_RESULTS
    assert result.status is ProviderStatus.UNAVAILABLE


@respx.mock
async def test_empty_data_with_status_200_is_no_results(
    provider: NewDBPropertyProvider,
) -> None:
    payload = live_fixture("rosreestr.json")
    payload["results"]["rosreestr"]["result"]["data"] = []
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=payload))

    result = await provider.fetch(address_subject(FLAT))

    assert result.status is ProviderStatus.NO_RESULTS


# ---------------------------------------------------------------- настройка


async def test_disabled_source_is_not_configured(live_settings: Settings) -> None:
    settings = live_settings.model_copy(
        update={"newdb_api_key": "k", "newdb_base_url": BASE_URL, "rosreestr_enabled": False}
    )
    provider = NewDBPropertyProvider(settings, NewDBFieldMaps())

    assert not provider.is_configured
    result = await provider.fetch(address_subject(FLAT))
    assert result.status is ProviderStatus.NOT_CONFIGURED


@respx.mock
async def test_batch_needs_its_own_flag(property_settings: Settings) -> None:
    """Прогон на восемьсот карточек с адресом — восемьсот платных вызовов."""
    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json={}))
    provider = NewDBPropertyProvider(property_settings, NewDBFieldMaps())
    subject = SearchSubject(search_type=SearchType.PERSON.value, address=FLAT)

    result = await provider.fetch(subject, FetchContext(batch=True))

    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert "ROSREESTR_IN_BATCH" in (result.error_message or "")
    assert route.call_count == 0
    assert provider.planned_calls(subject, FetchContext(batch=True)) == 0


def test_planned_calls_count_only_addressable_debtors(
    property_settings: Settings,
) -> None:
    settings = property_settings.model_copy(update={"rosreestr_in_batch": True})
    provider = NewDBPropertyProvider(settings, NewDBFieldMaps())
    batch = FetchContext(batch=True)

    assert provider.planned_calls(address_subject(FLAT), batch) == 1
    assert provider.planned_calls(address_subject(HOUSE), batch) == 0
    assert provider.planned_calls(address_subject(None), batch) == 0


def test_registry_replaces_the_property_stub(live_settings: Settings) -> None:
    from app.domain.enums import ProviderName
    from app.providers.registry import build_external_providers

    settings = live_settings.model_copy(
        update={"newdb_api_key": "k", "newdb_base_url": BASE_URL, "rosreestr_enabled": True}
    )
    by_name = {provider.name: provider for provider in build_external_providers(settings)}

    assert isinstance(by_name[ProviderName.PROPERTY], NewDBPropertyProvider)
    assert by_name[ProviderName.PROPERTY].is_configured
