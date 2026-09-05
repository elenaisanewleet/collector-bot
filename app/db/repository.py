"""Data access.

Repositories own SQL and nothing else; they take and return domain objects or
plain values so services never see a SQLAlchemy construct.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, Select, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AuditEvent,
    BatchItem,
    BatchRun,
    Debtor,
    DebtorReportRow,
    SearchRequest,
    SearchResult,
    ShareLink,
)
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

    async def iter_all(self, *, limit: int, offset: int = 0) -> list[Debtor]:
        """Страница выгрузки для массового прогона, в стабильном порядке."""
        return await self._all(select(Debtor).order_by(Debtor.id.asc()).limit(limit).offset(offset))

    async def find_by_address(self, address: str) -> list[Debtor]:
        needle = f"%{address.strip().lower()}%"
        return await self._all(
            select(Debtor).where(func.lower(Debtor.address).like(needle)).limit(25)
        )

    async def _all(self, stmt: Select[tuple[Debtor]]) -> list[Debtor]:
        result = await self._session.scalars(stmt)
        return list(result.all())


class BatchRepository:
    """Прогоны массовой проверки и их результаты."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_run(self, *, telegram_user_id: int, total: int) -> BatchRun:
        run = BatchRun(telegram_user_id=telegram_user_id, total=total, status="running")
        self._session.add(run)
        await self._session.flush()
        return run

    async def add_item(self, item: BatchItem) -> None:
        self._session.add(item)
        await self._session.flush()

    async def finish_run(
        self, run_id: int, *, processed: int, failed: int, status: str = "finished"
    ) -> None:
        run = await self._session.get(BatchRun, run_id)
        if run is None:
            return
        run.processed = processed
        run.failed = failed
        run.status = status
        run.finished_at = utcnow()
        await self._session.flush()

    async def latest_run(self, telegram_user_id: int) -> BatchRun | None:
        stmt = (
            select(BatchRun)
            .where(BatchRun.telegram_user_id == telegram_user_id)
            .order_by(BatchRun.started_at.desc(), BatchRun.id.desc())
            .limit(1)
        )
        found: BatchRun | None = await self._session.scalar(stmt)
        return found

    async def queue(
        self, run_id: int, *, verdict: str | None = None, limit: int = 50, offset: int = 0
    ) -> list[BatchItem]:
        """Очередь прогона: сначала то, что можно нести в суд сегодня."""
        stmt = select(BatchItem).where(BatchItem.batch_run_id == run_id)
        if verdict:
            stmt = stmt.where(BatchItem.verdict == verdict)
        stmt = (
            stmt.order_by(
                BatchItem.verdict_order.asc(),
                BatchItem.debt_kopecks.desc(),
                BatchItem.id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.scalars(stmt)
        return list(result.all())

    async def verdict_counts(self, run_id: int) -> dict[str, int]:
        stmt = (
            select(BatchItem.verdict, func.count())
            .where(BatchItem.batch_run_id == run_id)
            .group_by(BatchItem.verdict)
        )
        rows = await self._session.execute(stmt)
        return dict(rows.all())  # type: ignore[arg-type]

    async def verdict_totals(self, run_id: int) -> dict[str, Decimal]:
        """Сумма долга и пошлины в разрезе вердикта."""
        stmt = select(BatchItem.verdict, BatchItem.debt_amount, BatchItem.state_fee).where(
            BatchItem.batch_run_id == run_id
        )
        rows = await self._session.execute(stmt)
        totals: dict[str, Decimal] = {}
        for verdict, debt, fee in rows.all():
            totals[f"{verdict}:debt"] = totals.get(f"{verdict}:debt", Decimal("0")) + (
                debt or Decimal("0")
            )
            totals[f"{verdict}:fee"] = totals.get(f"{verdict}:fee", Decimal("0")) + (
                fee or Decimal("0")
            )
        return totals


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


class ShareLinkRepository:
    """Выдача и проверка ссылок на веб-отчёты."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        token: str,
        kind: str,
        target_id: int,
        telegram_user_id: int,
        ttl_hours: int,
    ) -> ShareLink:
        link = ShareLink(
            token=token,
            kind=kind,
            target_id=target_id,
            telegram_user_id=telegram_user_id,
            expires_at=utcnow() + timedelta(hours=ttl_hours),
        )
        self._session.add(link)
        await self._session.flush()
        return link

    async def find_active(self, token: str) -> ShareLink | None:
        """Живая ссылка по токену.

        Просроченная не возвращается вовсе: страница должна отдать 404, а не
        содержимое с пометкой «устарело».
        """
        stmt = select(ShareLink).where(
            ShareLink.token == token,
            ShareLink.expires_at > utcnow(),
            ShareLink.revoked_at.is_(None),
        )
        found: ShareLink | None = await self._session.scalar(stmt)
        return found

    async def find_for_target(self, kind: str, target_id: int) -> ShareLink | None:
        """Действующая ссылка на тот же отчёт — чтобы не плодить новые."""
        stmt = (
            select(ShareLink)
            .where(
                ShareLink.kind == kind,
                ShareLink.target_id == target_id,
                ShareLink.expires_at > utcnow(),
                ShareLink.revoked_at.is_(None),
            )
            .order_by(ShareLink.created_at.desc())
            .limit(1)
        )
        found: ShareLink | None = await self._session.scalar(stmt)
        return found

    async def mark_opened(self, link_id: int) -> None:
        link = await self._session.get(ShareLink, link_id)
        if link is None:
            return
        link.opened_count += 1
        link.last_opened_at = utcnow()
        await self._session.flush()

    async def revoke(self, *, kind: str, target_id: int) -> int:
        """Погасить все живые ссылки на один отчёт или прогон."""
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(ShareLink)
                .where(
                    ShareLink.kind == kind,
                    ShareLink.target_id == target_id,
                    ShareLink.revoked_at.is_(None),
                )
                .values(revoked_at=utcnow())
            ),
        )
        return result.rowcount or 0

    async def revoke_all(self, *, telegram_user_id: int) -> int:
        """Погасить все живые ссылки одного оператора — на случай «всё сразу»."""
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(ShareLink)
                .where(
                    ShareLink.telegram_user_id == telegram_user_id,
                    ShareLink.revoked_at.is_(None),
                    ShareLink.expires_at > utcnow(),
                )
                .values(revoked_at=utcnow())
            ),
        )
        return result.rowcount or 0

    async def purge_expired(self) -> int:
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(delete(ShareLink).where(ShareLink.expires_at <= utcnow())),
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
