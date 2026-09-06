"""Provider failure modes.

Every one of these asserts the same contract from a different angle: whatever
the network does, a provider returns a :class:`ProviderResult` with an honest
status and never raises.
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

from app.config import AuthStyle, FedresursBackend, FNSBackend, Settings
from app.domain.enums import ProceedingStatus, ProviderStatus, Region
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import BankruptcyRecord, EnforcementProceeding, ProviderResult
from app.providers.base import StubProvider
from app.providers.fedresurs import FedresursProvider
from app.providers.fns import FNSProvider
from app.providers.fssp import MAX_PROCEEDINGS, FSSPProvider
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
NEWDB_URL = f"{BASE_URL}/v2"

# Fixtures below mirror the NEWDB contract exactly as published at
# https://newdb.net/docs/fiz/01-fssp_person/ and in the OpenAPI document at
# https://newdb.net/swagger/openapi.json — the async envelope, the
# queued/in_progress/restart/complete/failed lifecycle, and the row keys the
# fssp_person method returns.

PROCEEDING_ROW: dict[str, Any] = {
    "Debtor": "ИВАНОВ ИВАН ИВАНОВИЧ 01.01.1990 Г. МОСКВА",
    "EnforcementProceeding": "88442/25/66049-ИП от 09.09.2025",
    "WritDetails": "Исполнительный лист от 01.01.2026 № 00RS0000#2-1/2026#1 ПРИМЕРНЫЙ СУД",
    "CompletionDateOrReason": "",
    "Service": "",
    "SubjectAndDebtAmount": (
        "Иные взыскания имущественного характера в пользу физических и "
        "юридических лиц Сумма долга: 30000.00 руб. "
        "Остаток долга по исполнительному документу: 12500.00 руб."
    ),
    "BailiffDepartment": "Примерный РОСП 000000, Россия, г. Москва, ул. Примерная, д. 1",
    "Phone": "+7(000)000-00-00",
    "BailiffOfficer": "ИВАНОВ И. И.",
}


def newdb_envelope(
    state: str = "complete",
    *,
    data: Sequence[Mapping[str, Any]] | None = None,
    include_results: bool = True,
    errors_info: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a NEWDB async envelope in the documented shape."""
    envelope: dict[str, Any] = {
        "params": {
            "firstname": "Андрей",
            "lastname": "Тестов",
            "secondname": "Сергеевич",
            "dob": "1985-03-12",
            "country": "ru",
            "method": "fssp_person",
            "regioncode": 77,
        },
        "requestId": "00000000-0000-4000-8000-000000000001",
        "datecreated": "2026-08-31 14:50:55",
        "state": state,
        "balance": 100,
        "tasks": 1,
        "is_repeat": False,
    }
    if errors_info is not None:
        envelope["errors_info"] = list(errors_info)
    if include_results and state == "complete":
        envelope["results"] = {
            "fssp_person": {
                "taskId": "00000000-0000-4000-8000-000000000002",
                "dateupdated": "2026-08-31 14:51:26",
                "result": {"status": 200, "data": list(data) if data is not None else []},
            },
            "management": {},
        }
    return envelope


# The real service answers a rejected or missing key with HTTP 200 and
# state="failed" — never a 401 — so this envelope is the one that must not be
# read as "checked, nothing found".
AUTH_FAILURE_ENVELOPE = newdb_envelope(
    "failed",
    errors_info=[
        {
            "error": (
                "Проверьте баланс и токен доступа (в HTTP заголовке X-API-KEY ), "
                "обратитесь по адресу access@newdb.net"
            )
        }
    ],
)


@pytest.fixture
def fssp_settings(live_settings: Settings) -> Settings:
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 2,
            "newdb_poll_interval_seconds": 0.01,
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


# ---------------------------------------------------------------- ФССП happy path


@respx.mock
async def test_fssp_parses_a_successful_response(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=newdb_envelope(data=[PROCEEDING_ROW]))
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.SUCCESS
    record = only_record(result, EnforcementProceeding)
    # The trailing "от <дата>" is stripped so the number is a stable key.
    assert record.proceeding_number == "88442/25/66049-ИП"
    assert record.debtor_name == "ИВАНОВ ИВАН ИВАНОВИЧ"
    assert record.debtor_birth_date == date(1990, 1, 1)
    # The outstanding balance wins over the original sum.
    assert record.amount == Decimal("12500.00")
    assert record.subject is not None
    assert record.subject.startswith("Иные взыскания имущественного характера")
    assert "Сумма долга" not in record.subject
    assert record.department is not None
    assert record.department.startswith("Примерный РОСП")
    assert record.status is ProceedingStatus.ACTIVE

    request = route.calls[0].request
    assert request.headers["X-API-KEY"] == "test-key"
    body = json.loads(request.content)
    # method travels inside params, and every mandatory field is present.
    assert body["params"]["method"] == "fssp_person"
    assert body["params"]["country"] == "ru"
    assert body["params"]["lastname"] == "Тестов"
    assert body["params"]["firstname"] == "Андрей"
    assert body["params"]["secondname"] == "Сергеевич"
    assert body["params"]["dob"] == "1985-03-12"
    assert body["params"]["regioncode"] == 77
    assert body["requestId"]


@respx.mock
async def test_fssp_falls_back_to_the_total_when_no_remainder_is_stated(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    row = {
        **PROCEEDING_ROW,
        "SubjectAndDebtAmount": "Взыскание налогов и сборов Сумма долга: 4200.55 руб.",
    }
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=newdb_envelope(data=[row])))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    assert only_record(result, EnforcementProceeding).amount == Decimal("4200.55")


@respx.mock
async def test_fssp_marks_a_completed_proceeding_as_closed(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    row = {**PROCEEDING_ROW, "CompletionDateOrReason": "Окончено 01.03.2026, ст. 46 ч.1 п.3"}
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=newdb_envelope(data=[row])))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    record = only_record(result, EnforcementProceeding)
    assert record.status is ProceedingStatus.CLOSED
    assert record.status_text is not None


@respx.mock
async def test_fssp_reports_no_results_when_the_source_is_empty(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """An empty ``data`` array is a real answer, so NO_RESULTS is correct here."""
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=newdb_envelope(data=[])))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    assert result.status is ProviderStatus.NO_RESULTS
    assert result.records == []


@respx.mock
@pytest.mark.parametrize(
    "row",
    [
        # Строка ответа не объектом — раньше просто пропускалась.
        "88442/25/66049-ИП",
        # Объект без номера производства: показать его нечем, и раньше он
        # исчезал так же тихо.
        {"Debtor": "ИВАНОВ ИВАН ИВАНОВИЧ", "SubjectAndDebtAmount": "Сумма долга: 30000.00 руб."},
        {"EnforcementProceeding": ""},
    ],
)
async def test_fssp_rows_it_cannot_read_are_never_an_empty_register(
    fssp_settings: Settings, person_subject: SearchSubject, row: Any
) -> None:
    """Общее правило счёта потерь распространяется и на ФССП.

    Это самый вероятно включённый источник и единственный, чьи ключи забиты в
    код. Пока непрочитанная строка молча выбрасывалась, ответ из одной такой
    строки давал ноль производств — то есть «активных исполнительных
    производств не найдено» в отчёте и плюс к взыскиваемости в оценке.
    """
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=newdb_envelope(data=[row])))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"
    assert not result.status.is_answered


@respx.mock
async def test_one_unreadable_fssp_row_fails_the_call_even_if_another_parsed(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """Показать одно производство из двух — значит показать неполный список."""
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200, json=newdb_envelope(data=[PROCEEDING_ROW, {"Debtor": "ИВАНОВ ИВАН ИВАНОВИЧ"}])
        )
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.error_code == "unexpected_schema"
    assert result.records == []
    assert "1 из 2" in (result.error_message or "")


@respx.mock
async def test_fssp_says_so_when_it_shows_only_the_first_hundred(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """Обрезка списка — неполнота, и о ней обязан сказать сам ответ."""
    rows = [
        {**PROCEEDING_ROW, "EnforcementProceeding": f"{index}/25/66049-ИП от 09.09.2025"}
        for index in range(MAX_PROCEEDINGS + 5)
    ]
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=newdb_envelope(data=rows)))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.SUCCESS
    assert len(result.records) == MAX_PROCEEDINGS
    assert result.is_partial
    assert any(str(MAX_PROCEEDINGS + 5) in note for note in result.notes)


@respx.mock
async def test_fssp_queries_each_region(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=newdb_envelope(data=[PROCEEDING_ROW]))
    )

    subject = person_subject.model_copy(
        update={"regions": (Region.MOSCOW.value, Region.MOSCOW_OBLAST.value)}
    )
    result = await FSSPProvider(fssp_settings).fetch(subject)

    assert route.call_count == 2
    assert [json.loads(call.request.content)["params"]["regioncode"] for call in route.calls] == [
        77,
        50,
    ]
    # The same proceeding returned for both regions is reported once.
    assert len(result.records) == 1


@respx.mock
async def test_fssp_falls_back_to_all_regions(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """A region we hold no code for widens the search instead of guessing one."""
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=newdb_envelope(data=[]))
    )

    subject = person_subject.model_copy(update={"regions": (Region.OTHER.value,)})
    await FSSPProvider(fssp_settings).fetch(subject)

    assert json.loads(route.calls[0].request.content)["params"]["regioncode"] == 100


@respx.mock
async def test_fssp_omits_the_patronymic_key_when_absent(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """Upstream rejects ``secondname`` sent empty — the key has to be left out.

    Checked against the live endpoint, which validates parameters before the
    key: an empty value answers ``secondname must be non-empty``, an absent one
    passes. Sending the empty string would have failed ФССП — the decisive
    source — for every debtor without a patronymic.
    """
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=newdb_envelope(data=[]))
    )

    name = PersonName(last_name="Тестов", first_name="Андрей")
    await FSSPProvider(fssp_settings).fetch(person_subject.model_copy(update={"name": name}))

    assert "secondname" not in json.loads(route.calls[0].request.content)["params"]


# ---------------------------------------------------------------- ФССП polling


@respx.mock
async def test_fssp_polls_until_the_task_completes(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """queued -> in_progress -> complete, polled by re-POSTing the same request."""
    route = respx.post(NEWDB_URL).mock(
        side_effect=[
            httpx.Response(200, json=newdb_envelope("queued")),
            httpx.Response(200, json=newdb_envelope("in_progress")),
            httpx.Response(200, json=newdb_envelope(data=[PROCEEDING_ROW])),
        ]
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.SUCCESS
    assert route.call_count == 3
    # Polling addresses the same task, so the requestId never changes.
    ids = {json.loads(call.request.content)["requestId"] for call in route.calls}
    assert len(ids) == 1


@respx.mock
async def test_fssp_treats_restart_as_still_running(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        side_effect=[
            httpx.Response(200, json=newdb_envelope("restart")),
            httpx.Response(200, json=newdb_envelope(data=[PROCEEDING_ROW])),
        ]
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    assert result.status is ProviderStatus.SUCCESS


@respx.mock
async def test_fssp_poll_budget_exhaustion_is_unavailable(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=newdb_envelope("in_progress")))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "poll_timeout"
    assert result.records == []


# ---------------------------------------------------------------- ФССП failures


@respx.mock
async def test_rejected_token_is_an_error_not_an_empty_result(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """The load-bearing case for this integration.

    NEWDB answers a bad or missing key with HTTP 200 and ``state: "failed"``.
    Read naively that is an empty result — a clean ФССП section for a debtor
    nobody actually checked.
    """
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=AUTH_FAILURE_ENVELOPE))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "unauthorized"
    assert not result.status.is_answered
    assert result.records == []


@respx.mock
async def test_insufficient_balance_is_reported(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            200,
            json=newdb_envelope(
                "failed",
                errors_info=[
                    {
                        "error": "Недостаточно средств",
                        "error_code": 402,
                        "docs_url": "https://newdb.net/docs/",
                    }
                ],
            ),
        )
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "payment_required"


@respx.mock
async def test_missing_parameter_failure_is_reported(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "params": {"method": "fssp_person", "country": "ru"},
                "requestId": "00000000-0000-4000-8000-000000000003",
                "state": "failed",
                "errors_info": [
                    {
                        "error": "Отсутствует обязательный параметр: dob",
                        "error_code": 400,
                        "docs_url": "https://newdb.net/docs/fiz/01-fssp_person/",
                    }
                ],
            },
        )
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    assert result.status is ProviderStatus.ERROR
    assert result.records == []


@respx.mock
async def test_timeout_is_unavailable_not_error(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "timeout"
    assert result.records == []


@respx.mock
async def test_http_unauthorized_is_reported_and_not_retried(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(401))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "unauthorized"
    # Retrying a rejected credential just burns quota.
    assert route.call_count == 1


@respx.mock
async def test_http_forbidden_is_reported_and_not_retried(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(403))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "unauthorized"
    assert route.call_count == 1


@respx.mock
async def test_http_payment_required_is_reported(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(402))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "payment_required"
    assert route.call_count == 1


@respx.mock
async def test_rate_limit_is_retried_then_reported(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(429))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "rate_limited"
    assert route.call_count == 2  # initial attempt + one retry


@respx.mock
async def test_rate_limit_recovers_on_retry(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        side_effect=[
            httpx.Response(429),
            httpx.Response(200, json=newdb_envelope(data=[PROCEEDING_ROW])),
        ]
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    assert result.status is ProviderStatus.SUCCESS


@respx.mock
async def test_server_error_is_unavailable(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(500))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "server_error"
    assert route.call_count == 2


@respx.mock
async def test_malformed_json_is_an_error_not_a_crash(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, text="<html>not json</html>"))

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "malformed_json"


@respx.mock
async def test_unknown_state_does_not_invent_records(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json={"requestId": "x", "totally": "unexpected"})
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"
    assert result.records == []


@respx.mock
async def test_complete_without_a_result_section_is_not_no_results(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """A complete envelope missing ``results`` is a schema problem, not a clean
    ФССП record."""
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=newdb_envelope("complete", include_results=False))
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"
    # Not an answer, so the report must not print "проверено, записей нет".
    assert not result.status.is_answered


@respx.mock
async def test_raw_response_is_not_kept_by_default(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=newdb_envelope(data=[PROCEEDING_ROW]))
    )

    result = await FSSPProvider(fssp_settings).fetch(person_subject)
    assert result.raw_response is None


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


async def test_missing_birth_date_is_not_no_results(
    fssp_settings: Settings, person_subject: SearchSubject
) -> None:
    """dob is mandatory upstream; without it we say so rather than report a
    clean ФССП section."""
    subject = person_subject.model_copy(update={"birth_date": None})

    result = await FSSPProvider(fssp_settings).fetch(subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered


async def test_insufficient_query_names_the_missing_field(
    fssp_settings: Settings, person_subject: SearchSubject, nameless_subject: SearchSubject
) -> None:
    """Чего не хватило — машинно, а не только словами.

    Карточка группирует источники с одной причиной в одну строку, и делать это
    разбором собственного текста нельзя: формулировку правят, группировка
    ломается молча. Ответ живёт рядом с сообщением и приходит от провайдера,
    поэтому разойтись им негде.
    """
    provider = FSSPProvider(fssp_settings)

    no_name = await provider.fetch(nameless_subject)
    no_date = await provider.fetch(person_subject.model_copy(update={"birth_date": None}))

    assert no_name.missing_input == ("name",)
    assert no_date.missing_input == ("birth_date",)


async def test_an_answered_provider_reports_no_missing_field(
    person_subject: SearchSubject,
) -> None:
    """Поле — про нехватку данных, а не про любой отказ."""
    result = await UnconfiguredVehicleProvider().fetch(person_subject)

    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert result.missing_input == ()


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
async def test_generic_backend_items_that_are_not_records_are_not_an_empty_register(
    live_settings: Settings, person_subject: SearchSubject, field_map_file: Path
) -> None:
    """Массив на месте, а внутри — не записи. Это «не разобрано», не «чисто».

    Фильтр «оставить только объекты» живёт внутри карты полей, и до сих пор он
    молча съедал такие элементы: ответ из двух строк вместо двух дел доезжал до
    провайдера пустым списком и печатался как проверенный чистый реестр.
    """
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
        return_value=httpx.Response(200, json={"items": ["А40-1/2026", "А40-2/2026"]})
    )

    result = await FedresursProvider(settings).fetch(person_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"
    assert not result.status.is_answered


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


# Переадресация — это указание источника, куда отправить наш запрос ещё раз,
# вместе с телом и заголовками. Проверяется на POST с ФИО в теле и с
# ``X-CSRFToken`` в заголовках: это в точности то, что уходило на чужой хост.
FOREIGN_URL = "https://evil.example.net"
NAME_BODY = {"name": "Иванов Иван Иванович"}


@respx.mock
async def test_a_redirect_to_another_host_is_refused() -> None:
    """Чужой хост не должен получить ни ФИО должника, ни csrf-токен."""
    respx.post(f"{BASE_URL}/api").mock(
        return_value=httpx.Response(307, headers={"Location": f"{FOREIGN_URL}/api"})
    )
    foreign = respx.post(f"{FOREIGN_URL}/api").mock(
        return_value=httpx.Response(200, json={"count": 0, "records": []})
    )

    async with build_client(
        base_url=BASE_URL, timeout_seconds=1, headers={"X-CSRFToken": "secret"}
    ) as client:
        with pytest.raises(Exception) as exc_info:
            await request_json(
                client,
                "POST",
                "/api",
                json_body=NAME_BODY,
                retry=RetryPolicy(max_retries=0),
                provider="test",
            )

    assert foreign.call_count == 0
    assert getattr(exc_info.value, "code", None) == "redirect_blocked"


@respx.mock
async def test_a_redirect_that_drops_https_is_refused() -> None:
    """Тот же хост, но открытым текстом, — это тоже утечка тела запроса."""
    plain = "http://api.example.test"
    respx.post(f"{BASE_URL}/api").mock(
        return_value=httpx.Response(307, headers={"Location": f"{plain}/api"})
    )
    downgraded = respx.post(f"{plain}/api").mock(
        return_value=httpx.Response(200, json={"count": 0, "records": []})
    )

    async with build_client(base_url=BASE_URL, timeout_seconds=1) as client:
        with pytest.raises(Exception) as exc_info:
            await request_json(
                client,
                "POST",
                "/api",
                json_body=NAME_BODY,
                retry=RetryPolicy(max_retries=0),
                provider="test",
            )

    assert downgraded.call_count == 0
    assert getattr(exc_info.value, "code", None) == "redirect_blocked"


@respx.mock
async def test_a_redirect_inside_the_same_host_still_works() -> None:
    """Запрет не должен ломать обычный переезд пути внутри того же сайта."""
    respx.post(f"{BASE_URL}/api").mock(
        return_value=httpx.Response(307, headers={"Location": f"{BASE_URL}/api/"})
    )
    moved = respx.post(f"{BASE_URL}/api/").mock(
        return_value=httpx.Response(200, json={"count": 1, "records": [{"id": 1}]})
    )

    async with build_client(base_url=BASE_URL, timeout_seconds=1) as client:
        payload, _raw = await request_json(
            client,
            "POST",
            "/api",
            json_body=NAME_BODY,
            retry=RetryPolicy(max_retries=0),
            provider="test",
        )

    assert moved.call_count == 1
    assert payload == {"count": 1, "records": [{"id": 1}]}


@respx.mock
async def test_a_redirect_loop_ends_with_an_error() -> None:
    """Кольцо переадресаций обрывается ошибкой, а не бесконечным хождением."""
    respx.get(f"{BASE_URL}/loop").mock(
        return_value=httpx.Response(302, headers={"Location": f"{BASE_URL}/loop"})
    )

    async with build_client(base_url=BASE_URL, timeout_seconds=1) as client:
        with pytest.raises(Exception) as exc_info:
            await request_json(
                client, "GET", "/loop", retry=RetryPolicy(max_retries=0), provider="test"
            )

    assert getattr(exc_info.value, "code", None) == "too_many_redirects"


@respx.mock
async def test_demo_providers_are_deterministic(person_subject: SearchSubject) -> None:
    from app.providers.mock import DemoFSSPProvider

    provider = DemoFSSPProvider()
    first = await provider.fetch(person_subject)
    second = await provider.fetch(person_subject)
    assert [r.model_dump(exclude={"fetched_at"}) for r in first.records] == [
        r.model_dump(exclude={"fetched_at"}) for r in second.records
    ]
