"""ФССП — исполнительные производства через агрегатор NewDB.

The former direct integration with ``api-ip.fssp.gov.ru`` is gone: that service
is retired and answers ``HTTP 410 Gone``. Enforcement proceedings now come from
the NewDB API, whose contract is documented at https://newdb.net/docs/ and
published as OpenAPI at https://newdb.net/swagger/openapi.json.

The contract this adapter implements, verified against that specification:

*   ``POST {NEWDB_BASE_URL}/v2`` with ``X-API-KEY``; the body is
    ``{"params": {...}, "requestId": "..."}`` and ``method`` lives *inside*
    ``params``. All seven params — firstname, lastname, secondname, dob,
    regioncode, country, method — are required.
*   The call is asynchronous. The envelope carries ``state``: ``queued``,
    ``in_progress`` and ``restart`` mean keep waiting; ``complete`` and
    ``failed`` are terminal. Polling is a repeat POST to the same endpoint with
    the same ``requestId``, which is what the specification prescribes and what
    keeps the token out of query strings and therefore out of access logs.
*   Results arrive at ``results.fssp_person.result.data`` as a list of rows
    keyed ``Debtor``, ``EnforcementProceeding``, ``SubjectAndDebtAmount``,
    ``BailiffDepartment`` and ``CompletionDateOrReason``.

One behaviour deserves emphasis, because getting it wrong would break this
project's central invariant: **a rejected or missing token comes back as
HTTP 200 with ``state: "failed"``**, not as 401/403. Handled naively that would
parse as "no proceedings found" — a clean report for a debtor nobody checked.
Every terminal ``failed`` state is therefore mapped to an error status, and a
``complete`` envelope without the expected result path is reported as
``unexpected_schema`` rather than as an empty result.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.config import Settings
from app.domain.enums import (
    REGION_FSSP_CODES,
    ProceedingStatus,
    ProviderName,
    ProviderStatus,
    Region,
)
from app.domain.identity import SearchSubject
from app.domain.models import EnforcementProceeding, ProviderResult
from app.logging_setup import get_logger
from app.providers.base import BaseProvider, ProviderError, ProviderUnavailableError
from app.providers.http import RetryPolicy, build_client, request_json
from app.providers.mapping import as_text, dig
from app.utils.dates import parse_date, utcnow
from app.utils.money import parse_amount

logger = get_logger(__name__)

NEWDB_METHOD = "fssp_person"
COUNTRY_RU = "ru"

# NewDB's own dictionary: 100 searches every ФССП region at once. Used when the
# operator picked a region we hold no code for, so the search widens instead of
# silently targeting the wrong region.
ALL_REGIONS_CODE = 100

STATE_COMPLETE = "complete"
STATE_FAILED = "failed"
PENDING_STATES = frozenset({"queued", "in_progress", "restart"})
KNOWN_STATES = PENDING_STATES | {STATE_COMPLETE, STATE_FAILED}

RESULT_DATA_PATH = f"results.{NEWDB_METHOD}.result.data"
MAX_PROCEEDINGS = 100

# Row keys, exactly as the specification names them.
FIELD_DEBTOR = "Debtor"
FIELD_PROCEEDING = "EnforcementProceeding"
FIELD_SUBJECT = "SubjectAndDebtAmount"
FIELD_DEPARTMENT = "BailiffDepartment"
FIELD_COMPLETION = "CompletionDateOrReason"

# The service reports a bad key and an empty balance through one message; both
# are actionable by the operator and neither is retryable.
AUTH_ERROR_MARKERS = ("токен", "token", "баланс", "balance", "x-api-key")
ERROR_CODE_PAYMENT_REQUIRED = 402
ERROR_CODE_BAD_REQUEST = 400

# "ИВАНОВ ИВАН ИВАНОВИЧ 01.01.1990 Г. МОСКВА" -> name, date, place.
_BIRTH_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
# "... Сумма долга: 30000.00 руб. Остаток долга по исполнительному документу: 30000.00 руб."
_REMAINING_DEBT_RE = re.compile(r"Остаток долга[^:]*:\s*([\d\s.,]+)")
_TOTAL_DEBT_RE = re.compile(r"Сумма долга\s*:\s*([\d\s.,]+)")
_DEBT_HEAD_RE = re.compile(r"\s*Сумма долга\s*:")
# "88442/25/66049-ИП от 09.09.2025" -> "88442/25/66049-ИП"
_PROCEEDING_TAIL_RE = re.compile(r"\s+от\s+\d{2}\.\d{2}\.\d{4}.*$")


class FSSPProvider(BaseProvider):
    """Enforcement proceedings for an individual, via NewDB."""

    name = ProviderName.FSSP
    title = "ФССП"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def is_configured(self) -> bool:
        return self._settings.fssp_configured

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if subject.name is None:
            return self.insufficient_query("Для поиска в ФССП нужно ФИО")
        if subject.birth_date is None:
            # NewDB requires dob. Saying so is honest; querying without it and
            # reporting the rejection as "ничего не найдено" would not be.
            return self.insufficient_query(
                "Для поиска в ФССП нужна дата рождения — источник требует её обязательно"
            )

        retry = RetryPolicy(
            max_retries=self._settings.provider_max_retries,
            backoff_seconds=self._settings.provider_retry_backoff_seconds,
        )
        records: list[EnforcementProceeding] = []
        raw_payloads: list[str] = []

        async with build_client(
            base_url=self._settings.newdb_base_url,
            timeout_seconds=self._settings.request_timeout_seconds,
            headers={"X-API-KEY": self._settings.newdb_api_key},
        ) as client:
            for region_code in _region_codes(subject.regions):
                envelope, raw = await self._run_method(client, subject, region_code, retry)
                raw_payloads.append(raw)
                records.extend(_parse_proceedings(envelope))

        unique = _dedupe(records)[:MAX_PROCEEDINGS]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if unique else ProviderStatus.NO_RESULTS,
            records=list(unique),
            raw_response="\n".join(raw_payloads) if self._settings.store_raw_responses else None,
        )

    async def _run_method(
        self,
        client: httpx.AsyncClient,
        subject: SearchSubject,
        region_code: int,
        retry: RetryPolicy,
    ) -> tuple[Any, str]:
        payload = self._build_payload(subject, region_code)
        envelope, raw = await self._post(client, payload, retry)

        state = _state_of(envelope)
        if state == STATE_COMPLETE:
            return envelope, raw
        if state == STATE_FAILED:
            raise _failure_error(envelope)
        if state not in PENDING_STATES:
            # An envelope with no recognizable state is not an empty result.
            raise ProviderUnavailableError(
                "unexpected_schema", "Ответ NewDB не содержит поля state"
            )
        return await self._poll(client, payload, retry)

    def _build_payload(self, subject: SearchSubject, region_code: int) -> dict[str, Any]:
        assert subject.name is not None  # guarded by the caller
        assert subject.birth_date is not None
        return {
            "params": {
                "method": NEWDB_METHOD,
                "country": COUNTRY_RU,
                "lastname": subject.name.last_name,
                "firstname": subject.name.first_name,
                # The field is mandatory; an empty value states "no patronymic"
                # rather than omitting the key and being rejected outright.
                "secondname": subject.name.middle_name or "",
                "dob": subject.birth_date.strftime("%Y-%m-%d"),
                "regioncode": region_code,
            },
            # Carried through polling: the same id addresses the same task.
            "requestId": str(uuid.uuid4()),
        }

    async def _post(
        self, client: httpx.AsyncClient, payload: Mapping[str, Any], retry: RetryPolicy
    ) -> tuple[Any, str]:
        return await request_json(
            client,
            "POST",
            self._settings.newdb_method_path,
            json_body=payload,
            retry=retry,
            provider=self.name.value,
        )

    async def _poll(
        self, client: httpx.AsyncClient, payload: Mapping[str, Any], retry: RetryPolicy
    ) -> tuple[Any, str]:
        """Re-POST the same requestId until the task settles or the budget ends."""
        for _attempt in range(self._settings.newdb_poll_attempts):
            await asyncio.sleep(self._settings.newdb_poll_interval_seconds)
            envelope, raw = await self._post(client, payload, retry)
            state = _state_of(envelope)
            if state == STATE_COMPLETE:
                return envelope, raw
            if state == STATE_FAILED:
                raise _failure_error(envelope)

        logger.info("fssp.poll_timeout", attempts=self._settings.newdb_poll_attempts)
        raise ProviderUnavailableError("poll_timeout", "NewDB не успела подготовить результат")


# ---------------------------------------------------------------- envelope


def _state_of(envelope: Any) -> str:
    state = as_text(dig(envelope, "state"))
    return state.lower() if state else ""


def _errors_info(envelope: Any) -> list[Mapping[str, Any]]:
    node = dig(envelope, "errors_info")
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, Mapping)]


def _failure_error(envelope: Any) -> ProviderError:
    """Translate a terminal ``failed`` envelope into a typed provider error.

    Never a ``NO_RESULTS``: the task did not run, so there is nothing to report
    as absent.
    """
    errors = _errors_info(envelope)
    messages = [text for item in errors if (text := as_text(item.get("error")))]
    message = "; ".join(messages) or as_text(dig(envelope, "error")) or "NewDB отклонила запрос"
    return ProviderError(_failure_code(errors, message), message)


def _failure_code(errors: list[Mapping[str, Any]], message: str) -> str:
    codes = {item.get("error_code") for item in errors}
    if ERROR_CODE_PAYMENT_REQUIRED in codes:
        return "payment_required"
    lowered = message.lower()
    if any(marker in lowered for marker in AUTH_ERROR_MARKERS):
        # A rejected key and an exhausted balance share one message here, and
        # both mean the same thing operationally: the source was not queried.
        return "unauthorized"
    if ERROR_CODE_BAD_REQUEST in codes:
        return "bad_request"
    return "request_failed"


def _parse_proceedings(envelope: Any) -> list[EnforcementProceeding]:
    """Map a completed envelope onto domain records.

    A missing result path on a ``complete`` envelope is a schema problem, not an
    empty result, and is reported as such.
    """
    rows = dig(envelope, RESULT_DATA_PATH)
    if rows is None:
        raise ProviderUnavailableError(
            "unexpected_schema", f"В ответе NewDB нет раздела {RESULT_DATA_PATH}"
        )
    if not isinstance(rows, list):
        raise ProviderUnavailableError(
            "unexpected_schema", f"{RESULT_DATA_PATH} не является списком"
        )

    fetched_at = utcnow()
    proceedings: list[EnforcementProceeding] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        record = _to_proceeding(row, fetched_at)
        if record is not None:
            proceedings.append(record)
    return proceedings


# ---------------------------------------------------------------- rows


def _to_proceeding(row: Mapping[str, Any], fetched_at: datetime) -> EnforcementProceeding | None:
    number = _proceeding_number(as_text(row.get(FIELD_PROCEEDING)))
    if number is None:
        return None

    debtor_name, debtor_birth_date = _split_debtor(as_text(row.get(FIELD_DEBTOR)))
    subject_text = as_text(row.get(FIELD_SUBJECT))
    completion = as_text(row.get(FIELD_COMPLETION))

    return EnforcementProceeding(
        proceeding_number=number,
        debtor_name=debtor_name,
        debtor_birth_date=debtor_birth_date,
        amount=_debt_amount(subject_text),
        # The source states a completion date or reason only for closed
        # proceedings, so its presence is the signal.
        status=ProceedingStatus.CLOSED if completion else ProceedingStatus.ACTIVE,
        status_text=completion,
        subject=_subject_text(subject_text),
        department=as_text(row.get(FIELD_DEPARTMENT)),
        fetched_at=fetched_at,
    )


def _proceeding_number(raw: str | None) -> str | None:
    """``88442/25/66049-ИП от 09.09.2025`` -> ``88442/25/66049-ИП``.

    The trailing date is dropped so the same proceeding returned for two regions
    deduplicates on one key.
    """
    if not raw:
        return None
    number = _PROCEEDING_TAIL_RE.sub("", raw).strip()
    return number or None


def _split_debtor(raw: str | None) -> tuple[str | None, date | None]:
    """``ИВАНОВ ИВАН ИВАНОВИЧ 01.01.1990 Г. МОСКВА`` -> name, date of birth.

    The place of birth is discarded: it is free text with no domain field, and
    the date is the part that drives identity matching.
    """
    if not raw:
        return None, None
    match = _BIRTH_DATE_RE.search(raw)
    if match is None:
        return raw.strip() or None, None
    name = raw[: match.start()].strip()
    return name or None, parse_date(match.group(1))


def _debt_amount(raw: str | None) -> Decimal | None:
    """Extract what the debtor still owes on this proceeding.

    The source states both the original sum and the outstanding balance. The
    balance is what matters for recovery — it is the claim still competing with
    ours — so it wins, with the original sum as the fallback when the source
    omits a remainder.
    """
    if not raw:
        return None
    for pattern in (_REMAINING_DEBT_RE, _TOTAL_DEBT_RE):
        match = pattern.search(raw)
        if match is None:
            continue
        amount = parse_amount(match.group(1))
        if amount is not None:
            return amount
    return None


def _subject_text(raw: str | None) -> str | None:
    """The purpose of the proceeding, without the amounts appended to it."""
    if not raw:
        return None
    head = _DEBT_HEAD_RE.split(raw, maxsplit=1)[0].strip()
    return head or raw.strip() or None


# ---------------------------------------------------------------- regions


def _region_codes(regions: tuple[str, ...]) -> list[int]:
    """Translate domain regions into NewDB region codes.

    A region we hold no code for falls back to "all regions" rather than to an
    arbitrary one: a broader search is a defensible default, a wrong region is
    not.
    """
    codes: list[int] = []
    for value in regions:
        try:
            region = Region(value)
        except ValueError:
            continue
        code = REGION_FSSP_CODES.get(region)
        if code is not None and code not in codes:
            codes.append(code)
    return codes or [ALL_REGIONS_CODE]


def _dedupe(records: list[EnforcementProceeding]) -> list[EnforcementProceeding]:
    """Multi-region searches legitimately return the same proceeding twice."""
    seen: set[str] = set()
    unique: list[EnforcementProceeding] = []
    for record in records:
        key = record.proceeding_number.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def build_fssp_provider(settings: Settings) -> FSSPProvider:
    provider = FSSPProvider(settings)
    if not provider.is_configured:
        logger.info("fssp.not_configured", reason="NEWDB_API_KEY/NEWDB_BASE_URL missing")
    return provider


__all__ = ["FSSPProvider", "build_fssp_provider"]
