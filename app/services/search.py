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
from collections.abc import Sequence
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
from app.providers.internal.base import InternalDebtorProvider
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
            subject, provider_results, internal_records=internal_records
        )
        report.recovery_score = self._score_engine.evaluate(report)

        await self._persist(
            report,
            telegram_user_id=telegram_user_id,
            query_hash=query_hash,
        )
        return report

    async def lookup_internal(self, subject: SearchSubject) -> list[InternalDebtorRecord]:
        """Query our own records using whichever identifiers we have.

        Exact-identifier hits get a confidence floor; a name-based hit goes
        through the ordinary matcher like any external record.
        """
        provider = self._registry.internal
        records, exact = await self._internal_candidates(provider, subject)
        if not records:
            return []
        unique = _dedupe_internal(records)
        self._matcher.annotate(
            subject,
            list(unique),
            confidence_floor=IDENTIFIER_MATCH_FLOOR if exact else None,
        )
        return unique

    # ------------------------------------------------------------- internals

    async def _internal_candidates(
        self, provider: InternalDebtorProvider, subject: SearchSubject
    ) -> tuple[list[InternalDebtorRecord], bool]:
        """Returns candidates plus whether they came from an exact identifier."""
        if subject.debtor_id:
            found = await provider.find_by_debtor_id(subject.debtor_id)
            if found:
                return found, True
        if subject.contract_number:
            found = await provider.find_by_contract(subject.contract_number)
            if found:
                return found, True
        if subject.claim_number:
            found = await provider.find_by_claim(subject.claim_number)
            if found:
                return found, True
        if subject.vehicle and subject.vehicle.vin:
            found = await provider.find_by_vin(subject.vehicle.vin)
            if found:
                return found, True
        if subject.vehicle and subject.vehicle.plate:
            found = await provider.find_by_plate(subject.vehicle.plate)
            if found:
                return found, True
        if subject.phone:
            found = await provider.find_by_phone(subject.phone)
            if found:
                return found, True

        candidates: list[InternalDebtorRecord] = []
        if subject.name:
            candidates.extend(
                await provider.find_by_fio(subject.name.full, birth_date=subject.birth_date)
            )
        if not candidates and subject.address:
            candidates.extend(await provider.find_by_address(subject.address))
        return candidates, False

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
                    is_partial=row.is_partial,
                    notes=_deserialize_notes(row.notes_json),
                )
            )

        internal_records = await self.lookup_internal(subject)
        report = self._aggregator.build(subject, results, internal_records=internal_records)
        # The score is recomputed rather than read back: the rules may have
        # changed since the cached run, and recomputation is free.
        report.recovery_score = self._score_engine.evaluate(report)
        report.from_cache = True
        report.cached_at = created_at
        return report


def _deserialize_notes(payload: str) -> tuple[str, ...]:
    """Оговорки о неполноте ответа, сохранённые вместе с ним.

    Битое хранилище не должно превращать неполный ответ в полный, поэтому
    ``is_partial`` читается отдельной колонкой и остаётся верным даже здесь: без
    текста оговорка станет менее внятной, но не исчезнет.
    """
    try:
        raw: Any = json.loads(payload)
    except json.JSONDecodeError:
        return ()
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw)


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
    "redact_subject",
    "subject_from_json",
]
