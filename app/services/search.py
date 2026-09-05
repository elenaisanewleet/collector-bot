"""Search orchestration.

The pipeline, end to end::

    SearchSubject
        -> internal lookup (CSV + database)
        -> external providers, in parallel and individually isolated
        -> IdentityMatcher
        -> Aggregator
        -> RecoveryScoreEngine
        -> persisted DebtorReport

Handlers call :meth:`SearchService.search` and nothing else. Whether a source is
a CSV file, an HTTP API or a future 1С instance is not visible from here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from pydantic import TypeAdapter, ValidationError

from app.config import Settings
from app.db.repository import AuditRepository, SearchRepository
from app.db.session import Database
from app.domain.enums import ProviderName, ProviderStatus, SearchType
from app.domain.identity import SearchSubject
from app.domain.models import (
    DebtorReport,
    FactRecord,
    InternalDebtorRecord,
    ProviderResult,
    RecoveryScore,
)
from app.logging_setup import get_logger
from app.providers.base import BaseProvider
from app.providers.internal.base import (
    InternalDebtorProvider,
    InternalRecords,
    InternalSourceFailure,
)
from app.providers.registry import ProviderRegistry
from app.services.aggregation import Aggregator
from app.services.identity import IdentityMatcher
from app.services.scoring import RecoveryScoreEngine
from app.utils.dates import iso_or_none
from app.utils.hashing import stable_hash
from app.utils.masking import mask_name, mask_passport, mask_phone

logger = get_logger(__name__)

_RECORDS_ADAPTER: TypeAdapter[list[FactRecord]] = TypeAdapter(list[FactRecord])

# Records reached through an exact identifier are trusted at this level even
# without a date of birth: the identifier is our own and unique.
IDENTIFIER_MATCH_FLOOR = 0.95
INTERNAL_TIMEOUT_MULTIPLIER = 2.0

# Which failure gets reported when several internal sources fall over at once.
# The worst one wins, because it is the one the operator has to act on.
_FAILURE_SEVERITY: dict[ProviderStatus, int] = {
    ProviderStatus.NOT_CONFIGURED: 1,
    ProviderStatus.UNAVAILABLE: 2,
    ProviderStatus.ERROR: 3,
}


class SearchService:
    """Runs a search and persists everything needed for history and caching."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        registry: ProviderRegistry,
        matcher: IdentityMatcher | None = None,
        aggregator: Aggregator | None = None,
        score_engine: RecoveryScoreEngine | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._registry = registry
        self._matcher = matcher or IdentityMatcher()
        self._aggregator = aggregator or Aggregator(self._matcher)
        self._score_engine = score_engine or RecoveryScoreEngine()
        self._semaphore = asyncio.Semaphore(settings.provider_concurrency)

    @property
    def registry(self) -> ProviderRegistry:
        """Источники, с которыми работает сервис.

        Нужен вызывающему, чтобы оценить стоимость массового прогона до его
        запуска; менять состав через это свойство нельзя.
        """
        return self._registry

    # ------------------------------------------------------------------ api

    async def search(
        self,
        subject: SearchSubject,
        *,
        telegram_user_id: int,
        force_refresh: bool = False,
    ) -> DebtorReport:
        """Run a full check and store it. Returns a cached report when fresh."""
        query_hash = build_query_hash(subject)

        if not force_refresh and self._settings.cache_enabled:
            cached = await self._load_cached(subject, query_hash)
            if cached is not None:
                logger.info(
                    "search.cache_hit",
                    user_id=telegram_user_id,
                    search_type=subject.search_type,
                )
                return cached

        internal_records = await self.lookup_internal(subject)
        provider_results = await self._run_external(subject)

        report = self._aggregator.build(
            subject,
            [*provider_results, internal_status_result(internal_records)],
            internal_records=internal_records,
        )
        report.recovery_score = self._score_engine.evaluate(report)

        await self._persist(
            report,
            telegram_user_id=telegram_user_id,
            query_hash=query_hash,
        )
        return report

    async def lookup_internal(self, subject: SearchSubject) -> InternalRecords:
        """Query our own records using whichever identifiers we have.

        Exact-identifier hits get a confidence floor; a name-based hit goes
        through the ordinary matcher like any external record. The returned
        list also carries whichever internal sources failed to answer — it is a
        ``list`` subclass, so every existing caller keeps working unchanged.
        """
        provider = self._registry.internal
        records, exact, failures = await self._internal_candidates(provider, subject)
        if not records:
            return InternalRecords((), failures=failures)
        unique = _dedupe_internal(records)
        self._matcher.annotate(
            subject,
            list(unique),
            confidence_floor=IDENTIFIER_MATCH_FLOOR if exact else None,
        )
        return InternalRecords(unique, failures=failures)

    # ------------------------------------------------------------- internals

    async def _internal_candidates(
        self, provider: InternalDebtorProvider, subject: SearchSubject
    ) -> tuple[list[InternalDebtorRecord], bool, tuple[InternalSourceFailure, ...]]:
        """Candidates, whether they came from an exact identifier, and refusals.

        Failures accumulate across the *whole* cascade. The short circuit on the
        first hit is what makes this necessary: a 1С that failed on
        ``by_debtor_id`` and a database that answered on ``by_fio`` otherwise
        produce a report claiming the internal contour was fully checked.
        """
        failures: list[InternalSourceFailure] = []
        vehicle = subject.vehicle

        exact_lookups: list[tuple[str | None, _Lookup]] = [
            (subject.debtor_id, provider.find_by_debtor_id),
            (subject.contract_number, provider.find_by_contract),
            (subject.claim_number, provider.find_by_claim),
            (vehicle.vin if vehicle else None, provider.find_by_vin),
            (vehicle.plate if vehicle else None, provider.find_by_plate),
            (subject.phone, provider.find_by_phone),
        ]
        for value, lookup in exact_lookups:
            if not value:
                continue
            found = await lookup(value)
            failures.extend(_failures_of(found))
            if found:
                return list(found), True, _dedupe_failures(failures)

        candidates: list[InternalDebtorRecord] = []
        if subject.name:
            found = await provider.find_by_fio(subject.name.full, birth_date=subject.birth_date)
            failures.extend(_failures_of(found))
            candidates.extend(found)
        if not candidates and subject.address:
            found = await provider.find_by_address(subject.address)
            failures.extend(_failures_of(found))
            candidates.extend(found)
        return candidates, False, _dedupe_failures(failures)

    async def _run_external(self, subject: SearchSubject) -> list[ProviderResult]:
        """Query every external provider concurrently.

        Concurrency is capped, each provider is wrapped in its own timeout, and
        every outcome — including a timeout — becomes a ``ProviderResult``. One
        slow source cannot delay or void the rest of the report.
        """
        providers = self._registry.external
        if not providers:
            return []
        results = await asyncio.gather(
            *(self._guarded_fetch(provider, subject) for provider in providers)
        )
        return list(results)

    async def _guarded_fetch(
        self, provider: BaseProvider, subject: SearchSubject
    ) -> ProviderResult:
        # A provider-level timeout on top of the HTTP timeout, so a provider that
        # polls or retries still has a hard ceiling.
        budget = self._settings.request_timeout_seconds * INTERNAL_TIMEOUT_MULTIPLIER
        async with self._semaphore:
            try:
                return await asyncio.wait_for(provider.fetch(subject), timeout=budget)
            except TimeoutError:
                logger.warning("provider.budget_exceeded", provider=provider.name.value)
                return ProviderResult(
                    provider=provider.name,
                    status=ProviderStatus.UNAVAILABLE,
                    error_code="timeout",
                    error_message="Источник не ответил вовремя",
                    duration_ms=int(budget * 1000),
                )

    # ------------------------------------------------------------ persistence

    async def _persist(
        self,
        report: DebtorReport,
        *,
        telegram_user_id: int,
        query_hash: str,
    ) -> None:
        score = report.recovery_score
        if score is None:  # pragma: no cover - the caller always sets it
            return
        async with self._database.session() as session:
            search_repo = SearchRepository(session)
            request = await search_repo.create_request(
                telegram_user_id=telegram_user_id,
                search_type=report.subject.search_type,
                normalized_query_hash=query_hash,
                masked_query=describe_subject(report.subject),
                subject_json=json.dumps(
                    redact_subject(
                        report.subject,
                        store_sensitive=self._settings.store_sensitive_identifiers,
                    ),
                    ensure_ascii=False,
                ),
            )
            await search_repo.save_provider_results(
                request.id,
                report.provider_results,
                store_raw=self._settings.store_raw_responses,
            )
            await search_repo.save_score(request.id, score)
            await AuditRepository(session).record(
                telegram_user_id=telegram_user_id,
                action="search.completed",
                entity_id=str(request.id),
                detail=f"{report.subject.search_type}: {score.score}/100",
            )

    async def _load_cached(self, subject: SearchSubject, query_hash: str) -> DebtorReport | None:
        """Rebuild a recent report of the same subject from storage."""
        async with self._database.session() as session:
            repo = SearchRepository(session)
            request = await repo.find_cached_request(
                query_hash, ttl_hours=self._settings.cache_ttl_hours
            )
            if request is None:
                return None
            stored_results = await repo.results_for_request(request.id)
            stored_report = await repo.report_for_request(request.id)
            if stored_report is None:
                return None
            created_at = request.created_at

        results: list[ProviderResult] = []
        for row in stored_results:
            if ProviderName(row.provider) is ProviderName.INTERNAL:
                # The internal contour is re-queried on every cache hit, so the
                # stored status describes a different run than the records
                # about to be attached. Replaced below, never restored.
                continue
            records = _deserialize_records(row.normalized_json)
            results.append(
                ProviderResult(
                    provider=ProviderName(row.provider),
                    status=ProviderStatus(row.provider_status),
                    fetched_at=row.fetched_at,
                    records=records,
                    error_code=row.error_code,
                    error_message=row.error_message,
                    duration_ms=row.duration_ms,
                    cache_hit=True,
                )
            )

        internal_records = await self.lookup_internal(subject)
        results.append(internal_status_result(internal_records))
        report = self._aggregator.build(subject, results, internal_records=internal_records)
        # The score is recomputed rather than read back: the rules may have
        # changed since the cached run, and recomputation is free.
        report.recovery_score = self._score_engine.evaluate(report)
        report.from_cache = True
        report.cached_at = created_at
        return report


_Lookup = Callable[[str], Awaitable[list[InternalDebtorRecord]]]


def _failures_of(records: Sequence[InternalDebtorRecord]) -> tuple[InternalSourceFailure, ...]:
    failures = getattr(records, "failures", ())
    return tuple(failures)


def _dedupe_failures(
    failures: Iterable[InternalSourceFailure],
) -> tuple[InternalSourceFailure, ...]:
    """One line per source and reason, however many cascade steps hit it."""
    seen: set[tuple[str, str]] = set()
    unique: list[InternalSourceFailure] = []
    for failure in failures:
        key = (failure.source, failure.error_code)
        if key in seen:
            continue
        seen.add(key)
        unique.append(failure)
    return tuple(unique)


def internal_status_result(records: InternalRecords) -> ProviderResult:
    """One ``ProviderResult`` describing the internal contour as a whole.

    ``records=[]`` on purpose: the records are handed to the aggregator
    separately, and :func:`Aggregator._dispatch` would append them to
    ``report.internal_records`` a second time, doubling the card.

    This is what makes "не проверено" visible at all. Without it an unreachable
    1С renders identically to a clean internal base — which is the one thing
    this project exists not to do.
    """
    failures = records.failures
    if failures:
        worst = max(failures, key=lambda item: _FAILURE_SEVERITY.get(item.status, 0))
        return ProviderResult(
            provider=ProviderName.INTERNAL,
            status=worst.status,
            records=[],
            error_code=worst.error_code,
            error_message=f"{worst.source}: {worst.error_message}",
        )
    return ProviderResult(
        provider=ProviderName.INTERNAL,
        status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
        records=[],
    )


def _deserialize_records(payload: str) -> list[FactRecord]:
    """Rebuild typed records from stored JSON, tolerating schema drift."""
    try:
        raw: Any = json.loads(payload)
    except json.JSONDecodeError:
        return []
    if not isinstance(raw, list):
        return []
    try:
        return _RECORDS_ADAPTER.validate_python(raw)
    except ValidationError:
        logger.warning("cache.record_schema_mismatch")
        return []


def _dedupe_internal(records: Sequence[InternalDebtorRecord]) -> list[InternalDebtorRecord]:
    seen: set[str] = set()
    unique: list[InternalDebtorRecord] = []
    for record in records:
        key = stable_hash(
            record.debtor_id or "",
            record.full_name,
            iso_or_none(record.birth_date),
            record.contract_number,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def build_query_hash(subject: SearchSubject) -> str:
    """Stable identity of a query, used as the cache key.

    One-way: the stored hash cannot be turned back into the query.
    """
    vehicle = subject.vehicle
    return stable_hash(
        subject.search_type,
        subject.name.normalized if subject.name else None,
        iso_or_none(subject.birth_date),
        subject.phone,
        subject.inn,
        subject.address,
        subject.contract_number,
        subject.claim_number,
        subject.debtor_id,
        vehicle.plate if vehicle else None,
        vehicle.vin if vehicle else None,
        ",".join(sorted(subject.regions)),
    )


def redact_subject(subject: SearchSubject, *, store_sensitive: bool) -> dict[str, Any]:
    """Serialize a subject for storage, dropping what must not be retained.

    With ``STORE_SENSITIVE_IDENTIFIERS`` off — the default — the passport and the
    full phone number are removed before the query is written. A re-run of such a
    search therefore proceeds without them, which is the intended trade.
    """
    payload = subject.model_dump(mode="json")
    if not store_sensitive:
        payload.pop("passport", None)
        payload.pop("phone", None)
    return payload


def subject_from_json(payload: str) -> SearchSubject | None:
    """Rebuild a stored subject; returns ``None`` if the record is unusable."""
    try:
        raw = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict) or not raw.get("search_type"):
        return None
    try:
        return SearchSubject.model_validate(raw)
    except ValidationError:
        logger.warning("history.subject_schema_mismatch")
        return None


def describe_subject(subject: SearchSubject) -> str:
    """A masked, human-readable label for the history list.

    Never contains a full phone number or passport.
    """
    search_type = subject.search_type
    if search_type == SearchType.CONTRACT.value:
        return subject.contract_number or subject.claim_number or subject.debtor_id or "—"
    if search_type == SearchType.PASSPORT.value:
        return mask_passport(subject.passport) or "—"
    vehicle_types = {
        SearchType.VEHICLE_PLATE.value,
        SearchType.VIN.value,
        SearchType.VEHICLE.value,
    }
    if search_type in vehicle_types:
        return subject.vehicle.title if subject.vehicle else "—"
    if search_type == SearchType.ADDRESS.value:
        return subject.address or "—"

    parts = [mask_name(subject.name.full) if subject.name else None]
    if subject.birth_date:
        parts.append(str(subject.birth_date.year))
    if subject.phone:
        parts.append(mask_phone(subject.phone))
    label = ", ".join(part for part in parts if part)
    return label or "—"


__all__ = [
    "RecoveryScore",
    "SearchService",
    "build_query_hash",
    "describe_subject",
    "internal_status_result",
    "redact_subject",
    "subject_from_json",
]
