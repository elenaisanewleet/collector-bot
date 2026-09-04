"""Data access.

Repositories own SQL and nothing else; they take and return domain objects or
plain values so services never see a SQLAlchemy construct.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, cast

from sqlalchemy import CursorResult, Select, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuditEvent, Debtor, DebtorReportRow, SearchRequest, SearchResult
from app.domain.models import InternalDebtorRecord, ProviderResult, RecoveryScore
from app.utils.dates import utcnow
from app.utils.hashing import normalize_token, stable_hash

HISTORY_PAGE_SIZE = 10


class DebtorRepository:
    """Reads and upserts of the internal debtor table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, debtor: Debtor) -> tuple[Debtor, bool]:
        """Insert or update by ``dedup_key``. Returns ``(row, created)``."""
        existing = await self._session.scalar(
            select(Debtor).where(Debtor.dedup_key == debtor.dedup_key)
        )
        if existing is None:
            self._session.add(debtor)
            await self._session.flush()
            return debtor, True

        for column in (
            "external_debtor_id",
            "fio",
            "fio_normalized",
            "birth_date",
            "phone",
            "phone_masked",
            "phone_hash",
            "contract_number",
            "claim_number",
            "debt_amount",
            "address",
            "vehicle_plate",
            "vin",
        ):
            value = getattr(debtor, column)
            # Never overwrite a known value with a blank from a sparser export.
            if value is not None:
                setattr(existing, column, value)
        existing.updated_at = utcnow()
        await self._session.flush()
        return existing, False

    async def count(self) -> int:
        return await self._session.scalar(select(func.count()).select_from(Debtor)) or 0

    async def find_by_fio(self, fio: str, birth_date: datetime | None = None) -> list[Debtor]:
        stmt = select(Debtor).where(Debtor.fio_normalized == normalize_token(fio))
        return await self._all(stmt)

    async def find_by_fio_prefix(self, surname_and_name: str) -> list[Debtor]:
        needle = f"{normalize_token(surname_and_name)}%"
        return await self._all(select(Debtor).where(Debtor.fio_normalized.like(needle)))

    async def find_by_phone_hash(self, phone_hash: str) -> list[Debtor]:
        return await self._all(select(Debtor).where(Debtor.phone_hash == phone_hash))

    async def find_by_contract(self, contract_number: str) -> list[Debtor]:
        return await self._all(
            select(Debtor).where(
                func.lower(Debtor.contract_number) == contract_number.strip().lower()
            )
        )

    async def find_by_claim(self, claim_number: str) -> list[Debtor]:
        return await self._all(
            select(Debtor).where(func.lower(Debtor.claim_number) == claim_number.strip().lower())
        )

    async def find_by_external_id(self, external_id: str) -> list[Debtor]:
        return await self._all(
            select(Debtor).where(
                func.lower(Debtor.external_debtor_id) == external_id.strip().lower()
            )
        )

    async def find_by_plate(self, plate: str) -> list[Debtor]:
        return await self._all(select(Debtor).where(Debtor.vehicle_plate == plate))

    async def find_by_vin(self, vin: str) -> list[Debtor]:
        return await self._all(select(Debtor).where(Debtor.vin == vin))

    async def find_by_address(self, address: str) -> list[Debtor]:
        needle = f"%{address.strip().lower()}%"
        return await self._all(
            select(Debtor).where(func.lower(Debtor.address).like(needle)).limit(25)
        )

    async def search_any(self, term: str) -> list[Debtor]:
        """Loose lookup used by the contract handler, which accepts a contract
        number, a claim number or an internal id in one field."""
        value = term.strip().lower()
        return await self._all(
            select(Debtor)
            .where(
                or_(
                    func.lower(Debtor.contract_number) == value,
                    func.lower(Debtor.claim_number) == value,
                    func.lower(Debtor.external_debtor_id) == value,
                )
            )
            .limit(25)
        )

    async def _all(self, stmt: Select[tuple[Debtor]]) -> list[Debtor]:
        result = await self._session.scalars(stmt)
        return list(result.all())


class SearchRepository:
    """Search requests, per-provider results, scores and the audit trail."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_request(
        self,
        *,
        telegram_user_id: int,
        search_type: str,
        normalized_query_hash: str,
        masked_query: str,
        subject_json: str = "{}",
    ) -> SearchRequest:
        request = SearchRequest(
            telegram_user_id=telegram_user_id,
            search_type=search_type,
            normalized_query_hash=normalized_query_hash,
            masked_query=masked_query,
            subject_json=subject_json,
        )
        self._session.add(request)
        await self._session.flush()
        return request

    async def save_provider_results(
        self,
        request_id: int,
        results: Sequence[ProviderResult],
        *,
        store_raw: bool = False,
    ) -> None:
        for result in results:
            self._session.add(
                SearchResult(
                    search_request_id=request_id,
                    provider=result.provider.value,
                    provider_status=result.status.value,
                    fetched_at=result.fetched_at,
                    normalized_json=json.dumps(
                        [record.model_dump(mode="json") for record in result.records],
                        ensure_ascii=False,
                    ),
                    raw_response=result.raw_response if store_raw else None,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    duration_ms=result.duration_ms,
                    cache_hit=result.cache_hit,
                )
            )
        await self._session.flush()

    async def save_score(self, request_id: int, score: RecoveryScore) -> None:
        self._session.add(
            DebtorReportRow(
                search_request_id=request_id,
                score=score.score,
                score_confidence=round(score.confidence * 100),
                category=score.category,
                factors_json=json.dumps(
                    [factor.model_dump(mode="json") for factor in score.factors],
                    ensure_ascii=False,
                ),
            )
        )
        await self._session.flush()

    async def recent_for_user(
        self, telegram_user_id: int, limit: int = HISTORY_PAGE_SIZE
    ) -> list[SearchRequest]:
        stmt = (
            select(SearchRequest)
            .where(SearchRequest.telegram_user_id == telegram_user_id)
            .order_by(SearchRequest.created_at.desc(), SearchRequest.id.desc())
            .limit(limit)
        )
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def get_request(self, request_id: int) -> SearchRequest | None:
        return await self._session.get(SearchRequest, request_id)

    async def find_cached_request(
        self, normalized_query_hash: str, *, ttl_hours: int
    ) -> SearchRequest | None:
        """Most recent search of the same subject still inside the TTL.

        The cache is shared across operators on purpose: two people chasing the
        same debtor should not each spend an external API call.
        """
        if ttl_hours <= 0:
            return None
        cutoff = utcnow() - timedelta(hours=ttl_hours)
        stmt = (
            select(SearchRequest)
            .join(DebtorReportRow, DebtorReportRow.search_request_id == SearchRequest.id)
            .where(
                SearchRequest.normalized_query_hash == normalized_query_hash,
                SearchRequest.created_at >= cutoff,
            )
            .order_by(SearchRequest.created_at.desc(), SearchRequest.id.desc())
            .limit(1)
        )
        found: SearchRequest | None = await self._session.scalar(stmt)
        return found

    async def results_for_request(self, request_id: int) -> list[SearchResult]:
        stmt = select(SearchResult).where(SearchResult.search_request_id == request_id)
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def report_for_request(self, request_id: int) -> DebtorReportRow | None:
        stmt = select(DebtorReportRow).where(DebtorReportRow.search_request_id == request_id)
        found: DebtorReportRow | None = await self._session.scalar(stmt)
        return found

    async def purge_older_than(self, cutoff: datetime) -> int:
        """Retention hook: drop search history older than the cutoff."""
        # execute() is typed as Result; a DELETE always yields a CursorResult,
        # which is the only kind that carries rowcount.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(SearchRequest).where(SearchRequest.created_at < cutoff)
            ),
        )
        return result.rowcount or 0


class AuditRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        telegram_user_id: int | None,
        action: str,
        entity_id: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._session.add(
            AuditEvent(
                telegram_user_id=telegram_user_id,
                action=action,
                entity_id=entity_id,
                detail=detail,
            )
        )
        await self._session.flush()

    async def recent(self, limit: int = 50) -> list[AuditEvent]:
        stmt = select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(limit)
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def count(self) -> int:
        return await self._session.scalar(select(func.count()).select_from(AuditEvent)) or 0


def debtor_to_record(debtor: Debtor) -> InternalDebtorRecord:
    """Map a stored row onto the domain fact used by the report."""
    return InternalDebtorRecord(
        debtor_id=debtor.external_debtor_id or str(debtor.id),
        full_name=debtor.fio,
        birth_date=debtor.birth_date,
        phone=debtor.phone,
        phone_masked=debtor.phone_masked,
        contract_number=debtor.contract_number,
        claim_number=debtor.claim_number,
        debt_amount=debtor.debt_amount,
        address=debtor.address,
        vehicle_plate=debtor.vehicle_plate,
        vin=debtor.vin,
        created_at=debtor.created_at,
    )


def phone_hash(phone: str | None) -> str | None:
    """Hash used to look a debtor up by phone without storing the number."""
    return stable_hash("phone", phone) if phone else None
