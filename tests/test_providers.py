"""Provider failure modes.

Every one of these asserts the same contract from a different angle: whatever
the network does, a provider returns a :class:`ProviderResult` with an honest
status and never raises.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
import respx

from app.config import AuthStyle, FedresursBackend, FNSBackend, Settings
from app.domain.enums import ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import BankruptcyRecord, EnforcementProceeding, ProviderResult
from app.providers.base import StubProvider
from app.providers.fedresurs import FedresursProvider
from app.providers.fns import FNSProvider
from app.providers.fssp import FSSPProvider
from app.providers.http import RetryPolicy, build_client, request_json
from app.providers.vehicle import UnconfiguredVehicleProvider


def only_record[T](result: object, expected: type[T]) -> T:
    """Narrow the single record of a result to its concrete type.

    ``ProviderResult.records`` is a discriminated union; asserting the type here
    both documents what the provider should have produced and gives the type
    checker something to work with.
    """
    records = getattr(result, "records", [])
    assert len(records) == 1, f"expected exactly one record, got {len(records)}"
    record = records[0]
    assert isinstance(record, expected), (
        f"expected {expected.__name__}, got {type(record).__name__}"
    )
    return record


BASE_URL = "https://api.example.test"
SEARCH_URL = f"{BASE_URL}/api/v1.0/search/physical"
RESULT_URL = f"{BASE_URL}/api/v1.0/result"

TASK_RESPONSE = {"status": 0, "response": {"task": "task-123"}}
RESULT_RESPONSE = {
    "status": 0,
    "response": {
        "result": [
            {
                "query": {"name": "Тестов Андрей Сергеевич"},
                "result": [
                    {
                        "name": "Тестов Андрей Сергеевич, 12.03.1985",
                        "exe_production": "12345/26/77001-ИП от 01.02.2026",
                        "subject": "Взыскание задолженности: 91400 руб.",
                        "department": "Демо ОСП",
                        "ip_end": "",
                    }
                ],
            }
        ]
    },
}


@pytest.fixture
def fssp_settings(live_settings: Settings) -> Settings:
    return live_settings.model_copy(
        update={
            "fssp_api_token": "test-token",
            "fssp_base_url": BASE_URL,
            "fssp_poll_attempts": 2,
            "fssp_poll_interval_seconds": 0.01,
            "provider_max_retries": 1,
            "provider_retry_backoff_seconds": 0.0,
        }
    )


# ---------------------------------------------------------------- not configured


async def test_fssp_without_credentials_is_not_configured(
    live_settings: Settings, person_subject: SearchSubject
) -> None:
    """Missing credentials are reported as such, never as an empty result."""
    result = await FSSPProvider(live_settings).fetch(person_subject)
    assert result.status is ProviderStatus.NOT_CONFIGURED
    # The source was never consulted, so it must not count as having answered.
    assert not result.status.is_answered
    assert result.records == []


async def test_fedresurs_without_backend_is_not_configured(
    live_settings: Settings, person_subject: SearchSubject
) -> None:
    result = await FedresursProvider(live_settings).fetch(person_subject)
    assert result.status is ProviderStatus.NOT_CONFIGURED


async def test_fns_without_backend_is_not_configured(
    live_settings: Settings, person_subject: SearchSubject
) -> None:
    result = await FNSProvider(live_settings).fetch(person_subject)
    assert result.status is ProviderStatus.NOT_CONFIGURED


async def test_vehicle_provider_is_not_configured(person_subject: SearchSubject) -> None:
    result = await UnconfiguredVehicleProvider().fetch(person_subject)
    assert result.status is ProviderStatus.NOT_CONFIGURED


async def test_future_stub_providers_are_not_configured(
    person_subject: SearchSubject,
) -> None:
    from app.providers.future import build_future_providers

    for provider in build_future_providers():
        result = await provider.fetch(person_subject)
        assert result.status is ProviderStatus.NOT_CONFIGURED


async def test_fedresurs_missing_field_map_stays_unconfigured(
    live_settings: Settings, person_subject: SearchSubject
) -> None:
    """A vendor endpoint without a field map cannot be parsed, so it is not
    treated as configured."""
    settings = live_settings.model_copy(
        update={
            "fedresurs_backend": FedresursBackend.GENERIC_JSON,
            "fedresurs_base_url": BASE_URL,
            "fedresurs_search_path": "/search",
            "fedresurs_api_key": "key",
        }
    )
    result = await FedresursProvider(settings).fetch(person_subject)
    assert result.status is ProviderStatus.NOT_CONFIGURED


# ---------------------------------------------------------------- happy path


@respx.mock
async def test_fssp_parses_a_successful_response(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=TASK_RESPONSE))
    respx.get(RESULT_URL).mock(return_value=httpx.Response(200, json=RESULT_RESPONSE))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.SUCCESS
    record = only_record(result, EnforcementProceeding)
    assert record.proceeding_number.startswith("12345/26/77001-ИП")
    # The amount is embedded in the subject line, not a dedicated field.
    assert record.amount == 91400


@respx.mock
async def test_fssp_reports_no_results_when_the_source_is_empty(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=TASK_RESPONSE))
    respx.get(RESULT_URL).mock(
        return_value=httpx.Response(200, json={"status": 0, "response": {"result": []}})
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    assert result.status is ProviderStatus.NO_RESULTS


@respx.mock
async def test_fssp_queries_each_region(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    from app.domain.enums import Region

    search = respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=TASK_RESPONSE))
    respx.get(RESULT_URL).mock(return_value=httpx.Response(200, json=RESULT_RESPONSE))

    subject = person_subject.model_copy(
        update={"regions": (Region.MOSCOW.value, Region.MOSCOW_OBLAST.value)}
    )
    result = await FSSPProvider(fssp_settings).fetch(subject)

    assert search.call_count == 2
    # The same proceeding returned for both regions is reported once.
    assert len(result.records) == 1


# ---------------------------------------------------------------- failures


@respx.mock
async def test_timeout_is_unavailable_not_error(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.get(SEARCH_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "timeout"
    assert result.records == []


@respx.mock
async def test_unauthorized_is_reported_and_not_retried(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(401))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "unauthorized"
    # Retrying a rejected credential just burns quota.
    assert route.call_count == 1


@respx.mock
async def test_rate_limit_is_retried_then_reported(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(429))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "rate_limited"
    assert route.call_count == 2  # initial attempt + one retry


@respx.mock
async def test_rate_limit_recovers_on_retry(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.get(SEARCH_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=TASK_RESPONSE),
        ]
    )
    respx.get(RESULT_URL).mock(return_value=httpx.Response(200, json=RESULT_RESPONSE))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    assert result.status is ProviderStatus.SUCCESS


@respx.mock
async def test_server_error_is_unavailable(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.get(SEARCH_URL).mock(return_value=httpx.Response(500))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "server_error"
    assert route.call_count == 2


@respx.mock
async def test_malformed_json_is_an_error_not_a_crash(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, text="<html>not json</html>"))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "malformed_json"


@respx.mock
async def test_unexpected_schema_does_not_invent_records(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """A response we cannot understand is an error, never an empty clean result."""
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json={"totally": "unexpected"}))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"
    assert result.records == []


@respx.mock
async def test_poll_timeout_is_reported(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.get(SEARCH_URL).mock(return_value=httpx.Response(200, json=TASK_RESPONSE))
    respx.get(RESULT_URL).mock(
        return_value=httpx.Response(200, json={"status": 1, "response": None})
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "poll_timeout"


async def test_provider_bug_is_contained(person_subject: SearchSubject) -> None:
    """An unhandled exception inside a provider becomes an ERROR result."""

    class BrokenProvider(StubProvider):
        @property
        def is_configured(self) -> bool:
            return True

        async def _fetch(self, subject: SearchSubject) -> ProviderResult:
            raise RuntimeError("boom")

    from app.domain.enums import ProviderName

    provider = BrokenProvider(ProviderName.COURT, "Суды", "test")
    result = await provider.fetch(person_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "unhandled_exception"


async def test_insufficient_query_is_not_no_results(
    fssp_settings: Settings, nameless_subject: SearchSubject
) -> None:
    result = await FSSPProvider(fssp_settings).fetch(nameless_subject)
    assert result.status is not ProviderStatus.NO_RESULTS
    assert result.error_code == "insufficient_query"


# ---------------------------------------------------------------- vendor adapters


@pytest.fixture
def field_map_file(tmp_path: Path) -> Path:
    """A minimal vendor field map, standing in for a real vendor's schema."""
    import json

    path = tmp_path / "map.json"
    path.write_text(
        json.dumps(
            {
                "records_path": "items",
                "fields": {
                    "debtor_name": "debtor.name",
                    "case_number": "case",
                    "procedure": "procedure",
                    "status": "state",
                    "started_at": "started",
                    "inn": "debtor.inn",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@respx.mock
async def test_fedresurs_generic_backend_parses_records(
    live_settings: Settings, person_subject: SearchSubject, field_map_file: Path
) -> None:
    settings = live_settings.model_copy(
        update={
            "fedresurs_backend": FedresursBackend.GENERIC_JSON,
            "fedresurs_base_url": BASE_URL,
            "fedresurs_search_path": "/bankruptcy",
            "fedresurs_api_key": "key",
            "fedresurs_auth_style": AuthStyle.BEARER,
            "fedresurs_field_map": field_map_file,
        }
    )
    respx.get(f"{BASE_URL}/bankruptcy").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "debtor": {"name": "Тестов Андрей Сергеевич", "inn": "770912345601"},
                        "case": "А40-1/2026",
                        "procedure": "Реализация имущества",
                        "state": "открыто",
                        "started": "2026-02-17",
                    }
                ]
            },
        )
    )

    result = await FedresursProvider(settings).fetch(person_subject)

    assert result.status is ProviderStatus.SUCCESS
    record = only_record(result, BankruptcyRecord)
    assert record.case_number == "А40-1/2026"
    assert record.is_active


@respx.mock
async def test_fns_generic_backend_reports_no_results(
    live_settings: Settings, person_subject: SearchSubject, tmp_path: Path
) -> None:
    import json

    field_map = tmp_path / "fns.json"
    field_map.write_text(
        json.dumps({"records_path": "items", "fields": {"inn": "inn"}}), encoding="utf-8"
    )
    settings = live_settings.model_copy(
        update={
            "fns_provider": FNSBackend.GENERIC_JSON,
            "fns_base_url": BASE_URL,
            "fns_search_path": "/egrul",
            "fns_api_key": "key",
            "fns_field_map": field_map,
        }
    )
    respx.get(f"{BASE_URL}/egrul").mock(return_value=httpx.Response(200, json={"items": []}))

    result = await FNSProvider(settings).fetch(person_subject)
    assert result.status is ProviderStatus.NO_RESULTS


@respx.mock
async def test_invalid_field_map_is_an_error(
    live_settings: Settings, person_subject: SearchSubject, tmp_path: Path
) -> None:
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    settings = live_settings.model_copy(
        update={
            "fns_provider": FNSBackend.GENERIC_JSON,
            "fns_base_url": BASE_URL,
            "fns_search_path": "/egrul",
            "fns_api_key": "key",
            "fns_field_map": broken,
        }
    )
    result = await FNSProvider(settings).fetch(person_subject)
    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "invalid_field_map"


# ---------------------------------------------------------------- http core


@respx.mock
async def test_request_json_honours_retry_budget() -> None:
    route = respx.get(f"{BASE_URL}/x").mock(return_value=httpx.Response(503))
    policy = RetryPolicy(max_retries=2, backoff_seconds=0.0)

    async with build_client(base_url=BASE_URL, timeout_seconds=1) as client:
        with pytest.raises(Exception) as exc_info:
            await request_json(client, "GET", "/x", retry=policy, provider="test")

    assert route.call_count == 3
    assert getattr(exc_info.value, "code", None) == "server_error"


@respx.mock
async def test_demo_providers_are_deterministic(person_subject: SearchSubject) -> None:
    from app.providers.mock import DemoFSSPProvider

    provider = DemoFSSPProvider()
    first = await provider.fetch(person_subject)
    second = await provider.fetch(person_subject)
    assert [r.model_dump(exclude={"fetched_at"}) for r in first.records] == [
        r.model_dump(exclude={"fetched_at"}) for r in second.records
    ]
