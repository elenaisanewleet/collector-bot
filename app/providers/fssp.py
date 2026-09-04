"""ФССП — банк данных исполнительных производств.

This adapter follows the public ФССП API's asynchronous request model: a search
call returns a task identifier, and the caller polls until the result is ready.
Endpoint paths, the polling budget and the parameter names are configuration
(``FSSP_*`` in ``.env``) rather than constants, and the response parser walks the
payload structurally instead of assuming a fixed nesting. If the response does
not contain anything recognizable, the provider reports ``ERROR`` with
``unexpected_schema`` — it never invents proceedings.

Before enabling this against production, verify the paths and parameter names in
the API documentation supplied with your token; they are the one part of this
integration that cannot be verified without the token itself.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

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
from app.providers.base import (
    BaseProvider,
    ProviderNotConfiguredError,
    ProviderUnavailableError,
)
from app.providers.http import RetryPolicy, build_client, request_json
from app.providers.mapping import as_text, dig, first_present
from app.utils.dates import parse_date, utcnow
from app.utils.money import parse_amount

logger = get_logger(__name__)

# Keys that identify a record as an enforcement proceeding, across the naming
# variants seen in the API and its mirrors.
_PROCEEDING_NUMBER_KEYS = (
    "exe_production",
    "proceeding_number",
    "ip_number",
    "number",
    "ip",
)
_NAME_KEYS = ("name", "debtor", "debtor_name", "fio")
_SUBJECT_KEYS = ("subject", "details", "description", "exe_subject")
_DEPARTMENT_KEYS = ("department", "osp", "subdivision", "division")
_AMOUNT_KEYS = ("amount", "sum", "debt", "balance", "subject_amount")
_END_KEYS = ("ip_end", "exe_end", "end_date", "completed", "ip_end_date")
_BIRTHDATE_KEYS = ("birthdate", "birth_date", "debtor_birthdate", "dob")
_TASK_KEYS = ("task", "task_id", "id")
_STATUS_KEYS = ("status", "state", "code")

MAX_PROCEEDINGS = 100
READY_STATUS_VALUES = frozenset({"0", "ok", "done", "ready", "success", "complete"})
PENDING_STATUS_VALUES = frozenset({"1", "2", "pending", "in_progress", "processing", "wait"})


class FSSPProvider(BaseProvider):
    """Enforcement proceedings for an individual."""

    name = ProviderName.FSSP
    title = "ФССП"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def is_configured(self) -> bool:
        return self._settings.fssp_configured

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if subject.name is None:
            # Name is mandatory for this source; there is no lawful lookup by
            # phone or address here.
            return self.insufficient_query("Для поиска в ФССП нужно ФИО")

        regions = _region_codes(subject.regions)
        retry = RetryPolicy(
            max_retries=self._settings.provider_max_retries,
            backoff_seconds=self._settings.provider_retry_backoff_seconds,
        )
        records: list[EnforcementProceeding] = []
        raw_payloads: list[str] = []

        async with build_client(
            base_url=self._settings.fssp_base_url,
            timeout_seconds=self._settings.request_timeout_seconds,
        ) as client:
            for region_code in regions:
                payload, raw = await self._search_region(client, subject, region_code, retry)
                raw_payloads.append(raw)
                records.extend(_parse_proceedings(payload))

        unique = _dedupe(records)[:MAX_PROCEEDINGS]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if unique else ProviderStatus.NO_RESULTS,
            records=list(unique),
            raw_response="\n".join(raw_payloads) if self._settings.store_raw_responses else None,
        )

    async def _search_region(
        self,
        client: Any,
        subject: SearchSubject,
        region_code: int | None,
        retry: RetryPolicy,
    ) -> tuple[Any, str]:
        params = self._build_params(subject, region_code)
        payload, raw = await request_json(
            client,
            "GET",
            self._settings.fssp_search_path,
            params=params,
            retry=retry,
            provider=self.name.value,
        )
        task_id = _extract_task_id(payload)
        if task_id is None:
            # Some deployments answer synchronously; accept that too, but only
            # if the body actually contains recognizable records.
            if _has_records(payload):
                return payload, raw
            raise ProviderUnavailableError(
                "unexpected_schema",
                "Ответ ФССП не содержит идентификатора задачи или записей",
            )
        return await self._poll(client, task_id, retry)

    def _build_params(
        self, subject: SearchSubject, region_code: int | None
    ) -> dict[str, str | int]:
        assert subject.name is not None  # guarded by the caller
        params: dict[str, str | int] = {
            "token": self._settings.fssp_api_token,
            "last_name": subject.name.last_name,
            "first_name": subject.name.first_name,
        }
        if subject.name.middle_name:
            params["second_name"] = subject.name.middle_name
        if subject.birth_date:
            params["birthdate"] = subject.birth_date.strftime("%Y-%m-%d")
        if region_code is not None:
            params["region"] = region_code
        return params

    async def _poll(self, client: Any, task_id: str, retry: RetryPolicy) -> tuple[Any, str]:
        """Poll the result endpoint until the task completes or the budget runs out."""
        last_payload: Any = None
        last_raw = ""
        for attempt in range(self._settings.fssp_poll_attempts):
            await asyncio.sleep(self._settings.fssp_poll_interval_seconds if attempt else 0.0)
            payload, raw = await request_json(
                client,
                "GET",
                self._settings.fssp_result_path,
                params={"token": self._settings.fssp_api_token, "task": task_id},
                retry=retry,
                provider=self.name.value,
            )
            last_payload, last_raw = payload, raw
            state = _task_state(payload)
            if state == "ready" or _has_records(payload):
                return payload, raw
            if state == "failed":
                raise ProviderUnavailableError(
                    "task_failed", "ФССП вернула ошибку выполнения задачи"
                )
        logger.info("fssp.poll_timeout", attempts=self._settings.fssp_poll_attempts)
        if _has_records(last_payload):
            return last_payload, last_raw
        raise ProviderUnavailableError("poll_timeout", "ФССП не успела подготовить результат")


def _region_codes(regions: tuple[str, ...]) -> list[int | None]:
    """Translate domain regions into API region codes.

    ``None`` means "no region filter", which is what an unlisted region gets: we
    would rather query broadly than silently query the wrong region.
    """
    codes: list[int | None] = []
    for value in regions:
        try:
            region = Region(value)
        except ValueError:
            continue
        code = REGION_FSSP_CODES.get(region)
        if code is not None and code not in codes:
            codes.append(code)
    return codes or [None]


def _extract_task_id(payload: Any) -> str | None:
    for path in ("response.task", "response.task_id", "task", "result.task"):
        value = as_text(dig(payload, path))
        if value:
            return value
    if isinstance(payload, Mapping):
        for key in _TASK_KEYS:
            value = as_text(payload.get(key))
            # A bare integer status is not a task id.
            if value and not value.isdigit():
                return value
    return None


def _task_state(payload: Any) -> str:
    """Classify a polling response as ready / pending / failed / unknown."""
    for path in ("status", "response.status", "result.status"):
        raw = dig(payload, path)
        if raw is None:
            continue
        token = str(raw).strip().lower()
        if token in READY_STATUS_VALUES:
            return "ready"
        if token in PENDING_STATUS_VALUES:
            return "pending"
    for key in _STATUS_KEYS:
        if isinstance(payload, Mapping) and str(payload.get(key, "")).lower() == "error":
            return "failed"
    return "unknown"


def _iter_dicts(node: Any, depth: int = 0) -> Iterator[Mapping[str, Any]]:
    """Walk a payload of unknown shape, yielding every mapping it contains."""
    if depth > 8:
        return
    if isinstance(node, Mapping):
        yield node
        for value in node.values():
            yield from _iter_dicts(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _iter_dicts(item, depth + 1)


def _looks_like_proceeding(record: Mapping[str, Any]) -> bool:
    number = first_present(record, _PROCEEDING_NUMBER_KEYS)
    if number is None:
        return False
    text = str(number).strip()
    # Proceeding numbers always carry a separator; a bare integer is a counter.
    return bool(text) and any(char in text for char in "/-")


def _has_records(payload: Any) -> bool:
    return any(_looks_like_proceeding(item) for item in _iter_dicts(payload))


def _parse_proceedings(payload: Any) -> list[EnforcementProceeding]:
    """Extract proceedings, skipping anything that does not parse.

    Records are built field by field with tolerant lookups so a renamed or
    missing key costs one attribute, not the whole result.
    """
    proceedings: list[EnforcementProceeding] = []
    fetched_at = utcnow()
    for record in _iter_dicts(payload):
        if not _looks_like_proceeding(record):
            continue
        number = as_text(first_present(record, _PROCEEDING_NUMBER_KEYS))
        if number is None:
            continue
        subject_text = as_text(first_present(record, _SUBJECT_KEYS))
        proceedings.append(
            EnforcementProceeding(
                proceeding_number=number,
                debtor_name=as_text(first_present(record, _NAME_KEYS)),
                debtor_birth_date=_parse_birth_date(record),
                amount=_parse_proceeding_amount(record, subject_text),
                status=_parse_status(record),
                status_text=as_text(first_present(record, _END_KEYS)),
                subject=subject_text,
                department=as_text(first_present(record, _DEPARTMENT_KEYS)),
                fetched_at=fetched_at,
            )
        )
    return proceedings


def _parse_birth_date(record: Mapping[str, Any]) -> date | None:
    raw = as_text(first_present(record, _BIRTHDATE_KEYS))
    if not raw:
        return None
    # The name field sometimes carries the date; take only a leading date token.
    return parse_date(raw.split()[0]) if raw else None


def _parse_proceeding_amount(record: Mapping[str, Any], subject_text: str | None) -> Decimal | None:
    explicit = parse_amount(as_text(first_present(record, _AMOUNT_KEYS)))
    if explicit is not None:
        return explicit
    if not subject_text:
        return None
    # Amounts are frequently embedded in the subject line, e.g.
    # "Иные взыскания имущественного характера: 91400 руб."
    tail = subject_text.rsplit(":", maxsplit=1)[-1]
    return parse_amount(tail)


def _parse_status(record: Mapping[str, Any]) -> ProceedingStatus:
    """A proceeding is closed only when the source says so explicitly."""
    end_marker = as_text(first_present(record, _END_KEYS))
    if end_marker:
        return ProceedingStatus.CLOSED
    return ProceedingStatus.ACTIVE


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
        logger.info("fssp.not_configured", reason="FSSP_API_TOKEN/FSSP_BASE_URL missing")
    return provider


__all__ = ["FSSPProvider", "ProviderNotConfiguredError", "build_fssp_provider"]
