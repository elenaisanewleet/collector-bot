"""Методы NewDB поверх общего конверта.

Каждый тест здесь проверяет один и тот же инвариант с новой стороны: метод,
схему строк которого деплой не описал, не опрашивается и не выдаёт «ничего не
найдено», а любой сбой на описанном методе становится честным статусом, а не
пустым результатом.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import FedresursBackend, FNSBackend, Settings
from app.domain.enums import (
    BankruptcyStatus,
    BusinessRole,
    CourtCaseRole,
    PledgeStatus,
    ProviderStatus,
)
from app.domain.identity import SearchSubject, VehicleDescriptor
from app.domain.models import BankruptcyRecord, BusinessRelation, CourtCase, PledgeRecord
from app.providers.court import NewDBArbitrationProvider
from app.providers.fedresurs import NewDBBankruptcyProvider
from app.providers.fns import NewDBBusinessProvider
from app.providers.mapping import FieldMapError
from app.providers.newdb import NewDBFieldMaps
from app.providers.pledge import NewDBPledgeProvider

BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"

# Placeholder row keys, matching config/field_maps/example_newdb.json. They are
# what a deployment writes down after reading its own contract; the point of the
# tests is that the adapter reads whatever the map says, not that these names
# are the real ones.
FIELD_MAP: dict[str, Any] = {
    "_comment": ["ignored"],
    "bankrot_person": {
        "fields": {
            "debtor_name": "Debtor",
            "inn": "INN",
            "case_number": "CaseNumber",
            "procedure": "Procedure",
            "status": "ProcedureStatus",
            "started_at": "ProcedureStartDate",
            "completed_at": "ProcedureEndDate",
        }
    },
    "egrul_ip": {
        "fields": {
            "inn": "INN",
            "ogrn": "OGRNIP",
            "name": "Name",
            "role": "Role",
            "status": "Status",
        },
        "value_maps": {"role": {"индивидуальный предприниматель": "ип"}},
    },
    "arbitr_person": {
        "fields": {
            "case_number": "CaseNumber",
            "court_name": "Court",
            "amount": "ClaimAmount",
            "filed_at": "FilingDate",
            "participant_name": "Participant",
            "inn": "INN",
            "role": "ParticipantRole",
            "status": "CaseStatus",
        }
    },
    "pledge_person": {
        "fields": {
            "registration_number": "NotificationNumber",
            "registered_at": "RegistrationDate",
            "terminated_at": "ExclusionDate",
            "pledgor_name": "Pledgor",
            "pledgor_birth_date": "PledgorBirthDate",
            "pledgee_name": "Pledgee",
            "subject": "PledgeSubject",
            "vin": "VIN",
        }
    },
    "pledge_vin": {
        "fields": {
            "registration_number": "NotificationNumber",
            "registered_at": "RegistrationDate",
            "pledgor_name": "Pledgor",
            "pledgee_name": "Pledgee",
            "subject": "PledgeSubject",
            "vin": "VIN",
        }
    },
}


def envelope(
    method: str,
    *,
    state: str = "complete",
    data: Sequence[Mapping[str, Any]] | None = None,
    include_results: bool = True,
    errors_info: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """The NewDB async envelope, in the shape the ФССП adapter already reads."""
    payload: dict[str, Any] = {
        "params": {"method": method, "country": "ru"},
        "requestId": "00000000-0000-4000-8000-000000000001",
        "state": state,
    }
    if errors_info is not None:
        payload["errors_info"] = list(errors_info)
    if include_results and state == "complete":
        payload["results"] = {
            method: {"result": {"status": 200, "data": list(data) if data is not None else []}}
        }
    return payload


@pytest.fixture
def field_map_path(tmp_path: Path) -> Path:
    path = tmp_path / "newdb.json"
    path.write_text(json.dumps(FIELD_MAP, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def newdb_settings(live_settings: Settings, field_map_path: Path) -> Settings:
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 2,
            "newdb_poll_interval_seconds": 0.01,
            "newdb_field_map": field_map_path,
            "provider_max_retries": 0,
            "provider_retry_backoff_seconds": 0.0,
            "fedresurs_backend": FedresursBackend.NEWDB,
            "fns_provider": FNSBackend.NEWDB,
        }
    )


@pytest.fixture
def maps(field_map_path: Path) -> NewDBFieldMaps:
    return NewDBFieldMaps.load(field_map_path)


@pytest.fixture
def inn_subject(person_subject: SearchSubject) -> SearchSubject:
    return person_subject.model_copy(update={"inn": "770912345601"})


# ---------------------------------------------------------------- field maps


def test_field_map_reads_every_described_method(maps: NewDBFieldMaps) -> None:
    assert maps.methods == {
        "bankrot_person",
        "egrul_ip",
        "arbitr_person",
        "pledge_person",
        "pledge_vin",
    }
    # Comment keys are documentation, not methods.
    assert "_comment" not in maps.methods


def test_no_field_map_means_no_methods() -> None:
    assert NewDBFieldMaps.load(None).methods == frozenset()


def test_field_map_rejects_an_entry_without_fields(tmp_path: Path) -> None:
    """An empty map would turn every row into an all-None record — a source that
    always answers "ничего не известно" while looking connected."""
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"bankrot_person": {"fields": {}}}), encoding="utf-8")

    with pytest.raises(FieldMapError):
        NewDBFieldMaps.load(path)


def test_field_map_rejects_a_non_object_entry(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text(json.dumps({"bankrot_person": "fssp_person"}), encoding="utf-8")

    with pytest.raises(FieldMapError):
        NewDBFieldMaps.load(path)


def test_field_map_rejects_unreadable_json(tmp_path: Path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(FieldMapError):
        NewDBFieldMaps.load(path)


# ---------------------------------------------------------------- not configured


async def test_unmapped_method_is_not_configured(
    live_settings: Settings, person_subject: SearchSubject
) -> None:
    """A key without a row map is not an integration.

    This is the case the whole design turns on: NewDB would happily answer, and
    the adapter would have nothing to read the answer with.
    """
    settings = live_settings.model_copy(
        update={"newdb_api_key": "test-key", "newdb_base_url": BASE_URL}
    )
    provider = NewDBBankruptcyProvider(settings, NewDBFieldMaps())

    result = await provider.fetch(person_subject)

    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert not result.status.is_answered
    assert result.records == []


async def test_mapped_method_without_a_key_is_not_configured(
    live_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    result = await NewDBBankruptcyProvider(live_settings, maps).fetch(person_subject)
    assert result.status is ProviderStatus.NOT_CONFIGURED


async def test_pledge_provider_needs_at_least_one_mapped_method(
    newdb_settings: Settings, person_subject: SearchSubject
) -> None:
    provider = NewDBPledgeProvider(newdb_settings, NewDBFieldMaps())
    assert provider.mapped_methods == ()
    result = await provider.fetch(person_subject)
    assert result.status is ProviderStatus.NOT_CONFIGURED


# ---------------------------------------------------------------- банкротство


@respx.mock
async def test_bankruptcy_maps_rows_through_the_field_map(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    row = {
        "Debtor": "Тестов Андрей Сергеевич",
        "INN": "770912345601",
        "CaseNumber": "А40-118472/2026",
        "Procedure": "Реализация имущества гражданина",
        "ProcedureStatus": "Введена",
        "ProcedureStartDate": "2026-02-17",
    }
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[row]))
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.SUCCESS
    record = result.records[0]
    assert isinstance(record, BankruptcyRecord)
    assert record.case_number == "А40-118472/2026"
    assert record.status is BankruptcyStatus.ACTIVE
    assert record.started_at == date(2026, 2, 17)

    body = json.loads(route.calls[0].request.content)
    assert body["params"]["method"] == "bankrot_person"
    # ``innfiz``, not ``inn``: the latter is the ten-digit legal-entity field.
    assert body["params"]["innfiz"] == "770912345601"
    assert "inn" not in body["params"]
    assert "lastname" not in body["params"]


@respx.mock
async def test_bankruptcy_reports_an_empty_register_as_no_results(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[]))
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(inn_subject)

    # The source did answer, so this one genuinely means "checked, nothing there".
    assert result.status is ProviderStatus.NO_RESULTS
    assert result.status.is_answered


@respx.mock
async def test_bankruptcy_failed_state_is_never_an_empty_register(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    """A rejected key arrives as HTTP 200 + state=failed on every method."""
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200,
            json=envelope(
                "bankrot_person",
                state="failed",
                errors_info=[{"error": "Проверьте баланс и токен доступа (X-API-KEY)"}],
            ),
        )
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "unauthorized"
    assert not result.status.is_answered


@respx.mock
async def test_bankruptcy_missing_result_section_is_a_schema_error(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", include_results=False))
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"


@respx.mock
async def test_bankruptcy_without_inn_is_not_queried(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    """ФИО с датой рождения метод не принимает — спрашивать нечем.

    Живой эндпоинт отвечает на person-блок ``Отсутствует обязательный параметр:
    innfiz``. Отправить запрос всё равно значило бы получить отказ и показать
    его как чистый реестр — ровно та подмена, против которой написан проект.
    """
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[]))
    )

    result = await NewDBBankruptcyProvider(newdb_settings, maps).fetch(person_subject)

    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered
    assert not route.calls  # платный вызов не потрачен на заведомый отказ


# ---------------------------------------------------------------- ИП


@respx.mock
async def test_sole_proprietor_status_is_mapped(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    row = {
        "INN": "770912345601",
        "OGRNIP": "316774600000000",
        "Name": "ИП Тестов Андрей Сергеевич",
        "Role": "Индивидуальный предприниматель",
        "Status": "Действует",
    }
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("egrul_ip", data=[row]))
    )

    result = await NewDBBusinessProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.SUCCESS
    record = result.records[0]
    assert isinstance(record, BusinessRelation)
    assert record.role is BusinessRole.SOLE_PROPRIETOR
    assert record.is_active_sole_proprietor
    assert json.loads(route.calls[0].request.content)["params"]["innfiz"] == "770912345601"


async def test_sole_proprietor_needs_an_identifier(
    newdb_settings: Settings, maps: NewDBFieldMaps
) -> None:
    subject = SearchSubject(search_type="contract", contract_number="EV-1")

    result = await NewDBBusinessProvider(newdb_settings, maps).fetch(subject)

    assert result.error_code == "insufficient_query"


@respx.mock
async def test_sole_proprietor_is_not_searched_by_name(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    """ФИО с датой рождения здесь было запасным путём — метод его не принимает."""
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("egrul_ip", data=[]))
    )

    result = await NewDBBusinessProvider(newdb_settings, maps).fetch(person_subject)

    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered
    assert not route.calls


# ---------------------------------------------------------------- арбитраж


async def test_arbitration_without_inn_is_not_searched_by_name(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    """Поиск по одному ФИО вернул бы чужие дела — и мы бы сняли должника с иска."""
    result = await NewDBArbitrationProvider(newdb_settings, maps).fetch(person_subject)

    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered


@respx.mock
async def test_arbitration_reads_role_and_amount(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    row = {
        "CaseNumber": "А40-227414/2026",
        "Court": "Арбитражный суд города Москвы",
        "ClaimAmount": "1 180 400,00",
        "FilingDate": "2026-05-20",
        "Participant": "Тестов Андрей Сергеевич",
        "INN": "770912345601",
        "ParticipantRole": "Ответчик",
        "CaseStatus": "Рассмотрение по существу",
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("arbitr_person", data=[row]))
    )

    result = await NewDBArbitrationProvider(newdb_settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, CourtCase)
    assert record.role is CourtCaseRole.DEFENDANT
    assert record.amount == Decimal("1180400.00")
    assert record.is_active
    assert record.is_against_debtor


@respx.mock
async def test_arbitration_skips_a_row_without_a_case_number(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200, json=envelope("arbitr_person", data=[{"Court": "Арбитражный суд"}])
        )
    )

    result = await NewDBArbitrationProvider(newdb_settings, maps).fetch(inn_subject)

    assert result.status is ProviderStatus.NO_RESULTS


@respx.mock
async def test_arbitration_marks_a_decided_case_closed(
    newdb_settings: Settings, maps: NewDBFieldMaps, inn_subject: SearchSubject
) -> None:
    row = {
        "CaseNumber": "А40-1/2025",
        "Participant": "Тестов Андрей Сергеевич",
        "ParticipantRole": "Ответчик",
        "CaseStatus": "Дело рассмотрено",
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("arbitr_person", data=[row]))
    )

    result = await NewDBArbitrationProvider(newdb_settings, maps).fetch(inn_subject)

    record = result.records[0]
    assert isinstance(record, CourtCase)
    assert not record.is_active
    assert not record.is_against_debtor


# ---------------------------------------------------------------- залоги


@respx.mock
async def test_pledge_by_person_is_active_until_excluded(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    row = {
        "NotificationNumber": "2022-006-123456-789",
        "RegistrationDate": "2022-04-11",
        "Pledgor": "Тестов Андрей Сергеевич",
        "PledgorBirthDate": "1985-03-12",
        "Pledgee": 'АО "Демонстрационный банк"',
        "PledgeSubject": "Автомобиль LADA VESTA, 2021",
        "VIN": "xta1234567890abcd",
    }
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("pledge_person", data=[row]))
    )

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(person_subject)

    assert result.status is ProviderStatus.SUCCESS
    record = result.records[0]
    assert isinstance(record, PledgeRecord)
    assert record.status is PledgeStatus.ACTIVE
    assert record.is_active
    assert record.vin == "XTA1234567890ABCD"
    assert record.pledgor_birth_date == date(1985, 3, 12)

    body = json.loads(route.calls[0].request.content)
    assert body["params"]["method"] == "pledge_person"


@respx.mock
async def test_excluded_pledge_is_not_active(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    row = {
        "NotificationNumber": "2022-006-123456-789",
        "RegistrationDate": "2022-04-11",
        "ExclusionDate": "2025-06-01",
        "Pledgor": "Тестов Андрей Сергеевич",
        "PledgeSubject": "Автомобиль LADA VESTA, 2021",
    }
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("pledge_person", data=[row]))
    )

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(person_subject)

    record = result.records[0]
    assert isinstance(record, PledgeRecord)
    assert record.status is PledgeStatus.TERMINATED
    assert not record.is_active


@respx.mock
async def test_pledge_by_vin_uses_the_vin_method(
    newdb_settings: Settings, maps: NewDBFieldMaps
) -> None:
    subject = SearchSubject(
        search_type="vin",
        vehicle=VehicleDescriptor(vin="XTA1234567890ABCD"),
    )
    row = {
        "NotificationNumber": "2022-006-123456-789",
        "RegistrationDate": "2022-04-11",
        "Pledgee": 'АО "Демонстрационный банк"',
        "PledgeSubject": "Автомобиль LADA VESTA, 2021",
        "VIN": "XTA1234567890ABCD",
    }
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("pledge_vin", data=[row]))
    )

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(subject)

    assert result.status is ProviderStatus.SUCCESS
    body = json.loads(route.calls[0].request.content)
    assert body["params"]["method"] == "pledge_vin"
    assert body["params"]["vin"] == "XTA1234567890ABCD"


@respx.mock
async def test_pledge_deduplicates_across_both_methods(
    newdb_settings: Settings, maps: NewDBFieldMaps, person_subject: SearchSubject
) -> None:
    """Both searches legitimately return the same notice; it is one pledge."""
    subject = person_subject.model_copy(
        update={"vehicle": VehicleDescriptor(vin="XTA1234567890ABCD")}
    )
    row = {
        "NotificationNumber": "2022-006-123456-789",
        "RegistrationDate": "2022-04-11",
        "Pledgor": "Тестов Андрей Сергеевич",
        "PledgeSubject": "Автомобиль LADA VESTA, 2021",
        "VIN": "XTA1234567890ABCD",
    }
    respx.post(NEWDB_URL).mock(
        side_effect=[
            httpx.Response(200, json=envelope("pledge_vin", data=[row])),
            httpx.Response(200, json=envelope("pledge_person", data=[row])),
        ]
    )

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(subject)

    assert len(result.records) == 1


async def test_pledge_with_an_unmapped_method_is_a_config_gap_not_a_data_gap(
    newdb_settings: Settings, person_subject: SearchSubject, tmp_path: Path
) -> None:
    """Искать есть по чему, но метод не описан — это про настройку, не про данные."""
    path = tmp_path / "vin-only.json"
    path.write_text(json.dumps({"pledge_vin": {"fields": {"vin": "VIN"}}}), encoding="utf-8")
    provider = NewDBPledgeProvider(newdb_settings, NewDBFieldMaps.load(path))

    # Человека искать есть по чему, но описан только метод по VIN.
    result = await provider.fetch(person_subject)

    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert result.error_code != "insufficient_query"


async def test_pledge_without_vin_or_identity_is_not_queried(
    newdb_settings: Settings, maps: NewDBFieldMaps
) -> None:
    subject = SearchSubject(search_type="contract", contract_number="EV-1")

    result = await NewDBPledgeProvider(newdb_settings, maps).fetch(subject)

    assert result.error_code == "insufficient_query"


# ---------------------------------------------------------------- extra params


@respx.mock
async def test_extra_params_override_what_the_adapter_would_send(
    live_settings: Settings, tmp_path: Path, person_subject: SearchSubject
) -> None:
    """The escape hatch for a contract that names a parameter differently."""
    path = tmp_path / "newdb.json"
    path.write_text(
        json.dumps(
            {
                "bankrot_person": {
                    "fields": {"case_number": "CaseNumber"},
                    "extra_params": {"country": "kz", "source": "efrsb"},
                }
            }
        ),
        encoding="utf-8",
    )
    settings = live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_field_map": path,
            "provider_max_retries": 0,
        }
    )
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=envelope("bankrot_person", data=[]))
    )

    await NewDBBankruptcyProvider(settings, NewDBFieldMaps.load(path)).fetch(person_subject)

    body = json.loads(route.calls[0].request.content)
    assert body["params"]["country"] == "kz"
    assert body["params"]["source"] == "efrsb"
    # Everything the adapter derives from the subject still travels.
    assert body["params"]["lastname"] == "Тестов"


# ---------------------------------------------------------------- сборка


def test_registry_replaces_the_stubs_with_the_mapped_sources(
    newdb_settings: Settings,
) -> None:
    """Заглушка обязана уйти, а не встать рядом.

    Отчёт адресуется по имени источника, поэтому две записи под одним именем
    означали бы, что одну из них никто никогда не прочитает.
    """
    from app.domain.enums import ProviderName
    from app.providers.court import NewDBArbitrationProvider as Arbitration
    from app.providers.fedresurs import NewDBBankruptcyProvider as Bankruptcy
    from app.providers.fns import NewDBBusinessProvider as Business
    from app.providers.pledge import NewDBPledgeProvider as Pledge
    from app.providers.registry import build_external_providers

    providers = build_external_providers(newdb_settings)
    by_name = {provider.name: provider for provider in providers}

    assert len(by_name) == len(providers), "источник зарегистрирован дважды"
    assert isinstance(by_name[ProviderName.FEDRESURS], Bankruptcy)
    assert isinstance(by_name[ProviderName.FNS], Business)
    assert isinstance(by_name[ProviderName.PLEDGE], Pledge)
    assert isinstance(by_name[ProviderName.COURT], Arbitration)
    assert {ProviderName.PROPERTY, ProviderName.INHERITANCE} <= set(by_name)


def test_registry_reports_the_mapped_methods_as_configured(
    newdb_settings: Settings,
) -> None:
    from app.domain.enums import ProviderName
    from app.providers.registry import build_external_providers

    configured = {
        provider.name
        for provider in build_external_providers(newdb_settings)
        if provider.is_configured
    }

    assert {
        ProviderName.FSSP,
        ProviderName.FEDRESURS,
        ProviderName.FNS,
        ProviderName.PLEDGE,
        ProviderName.COURT,
    } <= configured


def test_registry_without_a_field_map_leaves_the_new_sources_unconnected(
    live_settings: Settings,
) -> None:
    from app.domain.enums import ProviderName
    from app.providers.registry import build_external_providers

    settings = live_settings.model_copy(
        update={"newdb_api_key": "test-key", "newdb_base_url": BASE_URL}
    )
    by_name = {provider.name: provider for provider in build_external_providers(settings)}

    # ФССП не зависит от карты: его строки разобраны по проверенной схеме.
    assert by_name[ProviderName.FSSP].is_configured
    assert not by_name[ProviderName.PLEDGE].is_configured
    assert not by_name[ProviderName.COURT].is_configured


def test_shipped_example_map_describes_every_documented_method() -> None:
    """Пример в репозитории и код не должны разъезжаться."""
    from app.providers.court import NEWDB_METHOD as ARBITRATION_METHOD
    from app.providers.fedresurs import NEWDB_METHOD as BANKRUPTCY_METHOD
    from app.providers.fns import NEWDB_METHOD as BUSINESS_METHOD
    from app.providers.pledge import PERSON_METHOD, VIN_METHOD

    example = NewDBFieldMaps.load(Path("config/field_maps/example_newdb.json"))

    assert example.methods == {
        BANKRUPTCY_METHOD,
        BUSINESS_METHOD,
        ARBITRATION_METHOD,
        PERSON_METHOD,
        VIN_METHOD,
    }
