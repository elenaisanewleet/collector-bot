"""Data access.

Repositories own SQL and nothing else; they take and return domain objects or
plain values so services never see a SQLAlchemy construct.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, cast

from sqlalchemy import CursorResult, Select, and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AccessRequest,
    AuditEvent,
    BatchItem,
    BatchRun,
    Debtor,
    DebtorReportRow,
    PhoneLookup,
    QueryCard,
    SearchRequest,
    SearchResult,
    ShareLink,
    VendorCacheEntry,
)
from app.domain.enums import SETTLED_PROVIDER_STATUSES
from app.domain.identity import normalize_phone
from app.domain.models import InternalDebtorRecord, ProviderResult, RecoveryScore
from app.utils.dates import utcnow
from app.utils.hashing import normalize_token, stable_hash

HISTORY_PAGE_SIZE = 10


class VendorCacheRepository:
    """Ответы платных методов, ключ которых — не субъект поиска.

    Нужен цепочке по юрлицам: там вызов идёт по ИНН компании, а не по человеку,
    и без отдельного ключа два должника из одного ООО платят за один ответ
    дважды.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, cache_key: str, *, ttl_hours: int) -> str | None:
        if ttl_hours <= 0:
            return None
        cutoff = utcnow() - timedelta(hours=ttl_hours)
        stmt = select(VendorCacheEntry).where(
            VendorCacheEntry.cache_key == cache_key,
            VendorCacheEntry.created_at >= cutoff,
        )
        entry = await self._session.scalar(stmt)
        return entry.payload_json if entry is not None else None

    async def put(self, cache_key: str, payload_json: str) -> None:
        existing = await self._session.scalar(
            select(VendorCacheEntry).where(VendorCacheEntry.cache_key == cache_key)
        )
        if existing is None:
            self._session.add(VendorCacheEntry(cache_key=cache_key, payload_json=payload_json))
        else:
            existing.payload_json = payload_json
            existing.created_at = utcnow()
        await self._session.flush()


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
            "inn",
            "passport",
            "passport_masked",
            "address",
            "vehicle_plate",
            "vehicle_plates",
            "source_record_ids",
            "impounded_at",
            "released_at",
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

    async def total_debt(self) -> Decimal:
        """Сколько всего должны — одной цифрой.

        Считает база, а не Python: приветствие показывает эту сумму каждому
        ``/start``, и поднимать ради неё две тысячи строк со всеми полями
        значит платить за первый экран дороже, чем за сам отчёт.

        Суммы нет у части должников — там, где выгрузка не дала ни денег, ни
        двух дат для расчёта. ``SUM`` их пропускает, и это верно: приписать им
        ноль означало бы сказать «должны 0 ₽» вместо «неизвестно».
        """
        return await self._session.scalar(select(func.sum(Debtor.debt_amount))) or Decimal(0)

    async def find_by_fio(self, fio: str, birth_date: datetime | None = None) -> list[Debtor]:
        stmt = select(Debtor).where(Debtor.fio_normalized == normalize_token(fio))
        return await self._all(stmt)

    async def find_by_fio_prefix(self, surname_and_name: str) -> list[Debtor]:
        needle = f"{normalize_token(surname_and_name)}%"
        return await self._all(select(Debtor).where(Debtor.fio_normalized.like(needle)))

    async def count_by_surname(self, surname: str) -> int:
        """Сколько должников с такой фамилией. Ноль — это «новый клиент».

        Фамилия, а не ФИО целиком, и не префикс «фамилия имя»: у женщин в
        выгрузке заказчика фамилия бывает девичьей, отчество пропущенным, а имя
        сокращённым до буквы. Вопрос здесь другой, чем при поиске должника, —
        не «этот ли человек», а «людей с такой фамилией мы вообще знаем».
        Считать по нему точным совпадением ФИО значит объявлять новым каждого
        второго.

        ``fio_normalized`` собран из тех же слов через пробел (``normalize_token``),
        поэтому фамилия — это префикс до первого пробела.
        """
        needle = normalize_token(surname)
        if not needle:
            return 0
        stmt = select(func.count()).select_from(Debtor)
        pattern = f"{needle} %"
        return (
            await self._session.scalar(
                stmt.where(
                    or_(Debtor.fio_normalized == needle, Debtor.fio_normalized.like(pattern))
                )
            )
            or 0
        )

    async def all_by_name(self, *, limit: int) -> list[Debtor]:
        """Все должники по алфавиту — для выгрузки списка и для глаз.

        Порядок по ФИО, а не по идентификатору: список читает человек, и
        искать в нём он будет по фамилии.
        """
        return await self._all(
            select(Debtor).order_by(Debtor.fio_normalized.asc(), Debtor.id.asc()).limit(limit)
        )

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
        """Должник по госномеру — по любой его машине, а не только по последней.

        У взыскателя-эвакуатора один человек приезжает в выгрузке несколько раз
        на разных машинах, и все его номера хранятся в ``vehicle_plates``. Пока
        искали только по ``vehicle_plate``, находилась одна машина из
        нескольких: на живой выгрузке 88 должников, чьи прежние номера не
        находились вовсе. А госномер здесь — главный ключ поиска: телефона в
        выгрузке нет, машина у оператора на руках, и именно её он и вводит.

        Совпадение по списку — по границам элемента, а не подстрокой: «А123ВС77»
        не должен находиться внутри «А123ВС777».
        """
        return await self._all(
            select(Debtor).where(
                or_(
                    Debtor.vehicle_plate == plate,
                    Debtor.vehicle_plates == plate,
                    Debtor.vehicle_plates.startswith(f"{plate}, "),
                    Debtor.vehicle_plates.endswith(f", {plate}"),
                    Debtor.vehicle_plates.contains(f", {plate}, "),
                )
            )
        )

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

    async def update_progress(self, run_id: int, *, processed: int, failed: int) -> None:
        """Отметить продвижение незаконченного прогона.

        Страница очереди открывается, пока прогон идёт, и читает эти два числа.
        Без промежуточной записи они оба остаются нулями до самого конца.
        """
        run = await self._session.get(BatchRun, run_id)
        if run is None:
            return
        run.processed = processed
        run.failed = failed
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

    async def active_run(self, *, not_older_than: datetime | None = None) -> BatchRun | None:
        """Идущий прогон — чей угодно, а не только свой.

        Прогон платит за ВСЮ выгрузку, поэтому «уже идёт» — свойство базы, а не
        оператора: второй владелец, нажавший «Запустить» через минуту, оплатил
        бы тех же должников второй раз.

        ``not_older_than`` отсекает строки, оставшиеся в ``running`` после
        падения процесса: живого прогона за ними нет, и держать из-за них
        кнопку заблокированной навсегда нельзя.
        """
        stmt = select(BatchRun).where(BatchRun.status == "running")
        if not_older_than is not None:
            stmt = stmt.where(BatchRun.started_at >= not_older_than)
        stmt = stmt.order_by(BatchRun.started_at.desc(), BatchRun.id.desc()).limit(1)
        found: BatchRun | None = await self._session.scalar(stmt)
        return found

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
                    notes_json=json.dumps(list(result.notes), ensure_ascii=False),
                    raw_response=result.raw_response if store_raw else None,
                    error_code=result.error_code,
                    error_message=result.error_message,
                    duration_ms=result.duration_ms,
                    cache_hit=result.cache_hit,
                    # Не под флагом хранения: это не данные о должнике, а то,
                    # чего в ответе не хватало. Потеряв это, кэш пересоберёт
                    # неполный ответ как исчерпывающий.
                    is_partial=result.is_partial,
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

    async def count_for_user_since(self, telegram_user_id: int, since: datetime) -> int:
        """Сколько проверок этот человек запустил после указанного момента.

        Считаются запросы, а не ответы источников: квота защищает оплаченный
        остаток, а списывается он в момент запроса, чем бы тот ни кончился.
        Взятое из кэша сюда тоже попадает, и это осознанно — иначе счётчик
        зависел бы от того, чем ответил кэш, и предсказать его было бы нельзя.
        """
        stmt = (
            select(func.count())
            .select_from(SearchRequest)
            .where(SearchRequest.telegram_user_id == telegram_user_id)
            .where(SearchRequest.created_at >= since)
        )
        return int(await self._session.scalar(stmt) or 0)

    async def get_request(self, request_id: int) -> SearchRequest | None:
        return await self._session.get(SearchRequest, request_id)

    async def find_cached_request(
        self, normalized_query_hash: str, *, ttl_hours: int
    ) -> SearchRequest | None:
        """Most recent *reusable* search of the same subject still inside the TTL.

        The cache is shared across operators on purpose: two people chasing the
        same debtor should not each spend an external API call.

        Пригодным считается только отчёт, в котором все источники улеглись.
        Молчание источника — не ответ: подать такой отчёт из кэша значит
        закрепить «не проверено» на сутки, и починка на стороне сервиса до
        истечения TTL ничего не изменит. Ровно так и вышло однажды — после
        починки поиска по телефону тот же номер продолжал отдавать
        «недостаточно данных» из кэша, снятого до неё.

        Отбор в запросе, а не у вызывающего, — намеренно. Правило нужно и
        выдаче отчёта, и смете прогона, и разошлись они именно тогда, когда были
        написаны по отдельности: смета обещала «бесплатно» строки, за которые
        прогон потом платил. Одно определение — одно поведение.

        Недоспрошенный отчёт не пропадает: если в пределах TTL остался более
        ранний, но полный, берётся он — это дешевле нового обращения и честнее
        свежего пробела.
        """
        if ttl_hours <= 0:
            return None
        cutoff = utcnow() - timedelta(hours=ttl_hours)
        unsettled = (
            select(SearchResult.id)
            .where(SearchResult.search_request_id == SearchRequest.id)
            .where(SearchResult.provider_status.not_in(SETTLED_PROVIDER_STATUSES))
        )
        stmt = (
            select(SearchRequest)
            .join(DebtorReportRow, DebtorReportRow.search_request_id == SearchRequest.id)
            .where(
                SearchRequest.normalized_query_hash == normalized_query_hash,
                SearchRequest.created_at >= cutoff,
                ~unsettled.exists(),
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
        """Retention hook: drop search history older than the cutoff.

        Результаты провайдеров и отчёт уносит каскадом: ``ON DELETE CASCADE``
        объявлен в схеме и теперь действительно исполняется — внешние ключи
        включаются на каждом соединении, см. :mod:`app.db.session`.
        """
        await self._purge_orphaned_children()
        # execute() is typed as Result; a DELETE always yields a CursorResult,
        # which is the only kind that carries rowcount.
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(SearchRequest).where(SearchRequest.created_at < cutoff)
            ),
        )
        return result.rowcount or 0

    async def _purge_orphaned_children(self) -> int:
        """Подобрать то, что осиротело, пока внешние ключи были выключены.

        У этих строк родителя уже нет, поэтому по ``created_at`` запроса их не
        найти и каскад до них не дойдёт: без отдельного прохода они остались бы
        в базе навсегда — ровно те персональные данные, которые ретеншен уже
        отчитался удалившим. Проход дешёвый (индекс по ``search_request_id``) и
        после первой уборки не находит ничего, потому что новых сирот больше не
        появляется.

        Порядок важен: сначала сироты, потом удаление по cutoff. Иначе проход
        подобрал бы за сломанным каскадом в том же вызове и скрыл бы поломку.
        """
        alive = select(SearchRequest.id).where(SearchRequest.id == SearchResult.search_request_id)
        results = cast(
            "CursorResult[Any]",
            await self._session.execute(delete(SearchResult).where(~alive.exists())),
        )
        alive_reports = select(SearchRequest.id).where(
            SearchRequest.id == DebtorReportRow.search_request_id
        )
        reports = cast(
            "CursorResult[Any]",
            await self._session.execute(delete(DebtorReportRow).where(~alive_reports.exists())),
        )
        return (results.rowcount or 0) + (reports.rowcount or 0)


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

    async def get_active(self, link_id: int) -> ShareLink | None:
        """Живая ссылка по её идентификатору.

        Нужна производным ссылкам на одного человека: в них подписан номер
        ссылки-родителя, а не её токен, — иначе токен всей базы уехал бы в
        адрес каждой отдельной страницы, и переслать одного должника значило
        бы отдать всю выгрузку.
        """
        stmt = select(ShareLink).where(
            ShareLink.id == link_id,
            ShareLink.expires_at > utcnow(),
            ShareLink.revoked_at.is_(None),
        )
        found: ShareLink | None = await self._session.scalar(stmt)
        return found

    async def find_for_target(
        self, kind: str, target_id: int, *, telegram_user_id: int | None = None
    ) -> ShareLink | None:
        """Действующая ссылка на тот же отчёт — чтобы не плодить новые.

        ``telegram_user_id`` обязателен везде, где ссылку собираются
        переиспользовать: кэш отчётов общий на всех операторов, поэтому второй
        сотрудник, проверивший того же должника, получал ссылку, выданную
        первому, — и своей командой ``/revoke`` погасить её не мог. Без
        оператора (``None``) метод отвечает на другой вопрос — «жива ли вообще
        хоть одна ссылка на эту цель».
        """
        conditions = [
            ShareLink.kind == kind,
            ShareLink.target_id == target_id,
            ShareLink.expires_at > utcnow(),
            ShareLink.revoked_at.is_(None),
        ]
        if telegram_user_id is not None:
            conditions.append(ShareLink.telegram_user_id == telegram_user_id)
        stmt = select(ShareLink).where(*conditions).order_by(ShareLink.created_at.desc()).limit(1)
        found: ShareLink | None = await self._session.scalar(stmt)
        return found

    async def mark_opened(self, link_id: int) -> None:
        link = await self._session.get(ShareLink, link_id)
        if link is None:
            return
        link.opened_count += 1
        link.last_opened_at = utcnow()
        await self._session.flush()

    async def revoke(
        self, *, kind: str, target_id: int, telegram_user_id: int | None = None
    ) -> int:
        """Погасить живые ссылки на один отчёт или прогон.

        ``telegram_user_id`` сужает отзыв до ссылок этого оператора. Считаются и
        гасятся только непросроченные: иначе бот отчитывался бы «отозвано 1» за
        адрес, который и так уже не открывался.
        """
        conditions = [
            ShareLink.kind == kind,
            ShareLink.target_id == target_id,
            ShareLink.revoked_at.is_(None),
            ShareLink.expires_at > utcnow(),
        ]
        if telegram_user_id is not None:
            conditions.append(ShareLink.telegram_user_id == telegram_user_id)
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                update(ShareLink).where(*conditions).values(revoked_at=utcnow())
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

    async def live_targets(self, *, telegram_user_id: int) -> list[tuple[str, int]]:
        """Цели, на которые у оператора сейчас есть живые ссылки."""
        stmt = (
            select(ShareLink.kind, ShareLink.target_id)
            .where(
                ShareLink.telegram_user_id == telegram_user_id,
                ShareLink.revoked_at.is_(None),
                ShareLink.expires_at > utcnow(),
            )
            .distinct()
        )
        rows = await self._session.execute(stmt)
        return [(kind, target_id) for kind, target_id in rows.all()]

    async def count_live_of_others(
        self, targets: Sequence[tuple[str, int]], *, telegram_user_id: int
    ) -> int:
        """Сколько живых ссылок на те же цели выдано другим операторам.

        Нужно, чтобы ответ на ``/revoke`` не врал: свои адреса погашены, а отчёт
        всё ещё открывается по ссылке коллеги, и человек об этом обязан узнать
        от бота, а не от того, кому эта ссылка попадёт.
        """
        if not targets:
            return 0
        same_target = or_(
            *(
                and_(ShareLink.kind == kind, ShareLink.target_id == target_id)
                for kind, target_id in targets
            )
        )
        stmt = (
            select(func.count())
            .select_from(ShareLink)
            .where(
                same_target,
                ShareLink.telegram_user_id != telegram_user_id,
                ShareLink.revoked_at.is_(None),
                ShareLink.expires_at > utcnow(),
            )
        )
        return await self._session.scalar(stmt) or 0

    async def purge_expired(self) -> int:
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(delete(ShareLink).where(ShareLink.expires_at <= utcnow())),
        )
        return result.rowcount or 0

    async def purge_orphaned_reports(self, *, kind: str) -> int:
        """Убрать ссылки, за которыми уже нет отчёта.

        Ретеншен удаляет историю проверок, но ссылки на неё оставлял: строка
        живёт до конца TTL и всё это время хранит связку «оператор → какой
        отчёт он смотрел» — ровно ту карту доступа, которую ретеншен отчитался
        удалившей. Открыть по такой ссылке уже нечего, так что гасить её нечем,
        кроме удаления.
        """
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(
                delete(ShareLink).where(
                    ShareLink.kind == kind,
                    ShareLink.target_id.not_in(select(SearchRequest.id)),
                )
            ),
        )
        return result.rowcount or 0


class AccessRepository:
    """Заявки на доступ. Одна строка на человека — см. модель."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, telegram_user_id: int) -> AccessRequest | None:
        stmt = select(AccessRequest).where(AccessRequest.telegram_user_id == telegram_user_id)
        found: AccessRequest | None = await self._session.scalar(stmt)
        return found

    async def upsert_request(
        self,
        *,
        telegram_user_id: int,
        username: str | None,
        full_name: str | None,
        status: str,
    ) -> AccessRequest:
        """Подать заявку: создать строку или вернуть существующую в ``pending``.

        Имя и username переписываются при каждой подаче: человек мог их сменить,
        а владелец решает именно по ним.
        """
        row = await self.get(telegram_user_id)
        now = utcnow()
        if row is None:
            row = AccessRequest(
                telegram_user_id=telegram_user_id,
                username=username,
                full_name=full_name,
                status=status,
                requested_at=now,
            )
            self._session.add(row)
        else:
            row.username = username or row.username
            row.full_name = full_name or row.full_name
            row.status = status
            row.requested_at = now
            row.decided_at = None
            row.decided_by = None
        await self._session.flush()
        return row

    async def set_status(
        self, telegram_user_id: int, *, status: str, decided_by: int
    ) -> AccessRequest | None:
        row = await self.get(telegram_user_id)
        if row is None:
            return None
        row.status = status
        row.decided_at = utcnow()
        row.decided_by = decided_by
        await self._session.flush()
        return row

    async def by_status(self, *statuses: str) -> list[AccessRequest]:
        stmt = (
            select(AccessRequest)
            .where(AccessRequest.status.in_(statuses))
            .order_by(AccessRequest.requested_at.desc(), AccessRequest.id.desc())
        )
        result = await self._session.scalars(stmt)
        return list(result.all())


class QueryCardRepository:
    """Накопительная карточка запроса. Одна строка на пару «оператор + чат»."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, telegram_user_id: int, chat_id: int) -> QueryCard | None:
        stmt = select(QueryCard).where(
            QueryCard.telegram_user_id == telegram_user_id, QueryCard.chat_id == chat_id
        )
        found: QueryCard | None = await self._session.scalar(stmt)
        return found

    async def save(
        self, telegram_user_id: int, chat_id: int, values: Mapping[str, Any]
    ) -> QueryCard:
        """Создать строку или переписать существующую целиком.

        Именно целиком, а не по изменённым полям: карточка редактируется и
        очищается, и частичное обновление оставило бы прошлого должника в тех
        колонках, которые новый не заполнил. ``values`` приходит из
        :class:`app.services.query_card.Card`, где перечислены все колонки.
        """
        row = await self.get(telegram_user_id, chat_id)
        if row is None:
            row = QueryCard(telegram_user_id=telegram_user_id, chat_id=chat_id)
            self._session.add(row)
        for column, value in values.items():
            setattr(row, column, value)
        row.updated_at = utcnow()
        await self._session.flush()
        return row

    async def delete(self, telegram_user_id: int, chat_id: int) -> None:
        await self._session.execute(
            delete(QueryCard).where(
                QueryCard.telegram_user_id == telegram_user_id, QueryCard.chat_id == chat_id
            )
        )

    async def purge_older_than(self, cutoff: datetime) -> int:
        """Ретеншен: карточка — черновик, а не история, и живёт по своему сроку."""
        result = cast(
            "CursorResult[Any]",
            await self._session.execute(delete(QueryCard).where(QueryCard.updated_at < cutoff)),
        )
        return result.rowcount or 0


class PhoneLookupRepository:
    """Находки по номеру телефона — писать и читать списком."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, lookup: PhoneLookup) -> PhoneLookup:
        self._session.add(lookup)
        await self._session.flush()
        return lookup

    async def recent(self, *, limit: int) -> list[PhoneLookup]:
        """Последние находки — новыми вверх.

        Новыми вверх, а не по алфавиту, как справочник должников: там список
        читают поиском по фамилии, здесь — глазами сверху, «кого я пробил
        сегодня». Порядок вторым ключом по идентификатору: две находки в одну
        секунду иначе меняются местами между обновлениями страницы.
        """
        stmt = (
            select(PhoneLookup)
            .order_by(PhoneLookup.created_at.desc(), PhoneLookup.id.desc())
            .limit(limit)
        )
        return list((await self._session.scalars(stmt)).all())

    async def count(self) -> int:
        return await self._session.scalar(select(func.count()).select_from(PhoneLookup)) or 0


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
        inn=debtor.inn,
        passport=debtor.passport,
        passport_masked=debtor.passport_masked,
        contract_number=debtor.contract_number,
        claim_number=debtor.claim_number,
        debt_amount=debtor.debt_amount,
        address=debtor.address,
        vehicle_plate=debtor.vehicle_plate,
        vin=debtor.vin,
        created_at=debtor.created_at,
    )


def phone_hash(phone: str | None) -> str | None:
    """Hash used to look a debtor up by phone without storing the number.

    Номер приводится к ``+7XXXXXXXXXX`` до хеширования, иначе две стороны
    сравнения расходятся. Так и было: при импорте хешировался уже приведённый
    номер из выгрузки, а при поиске — то, что набрал оператор. Совпадение
    случалось лишь при посимвольном равенстве, и «89263248600» не находил
    должника, записанного как «+79263248600».

    Молчала эта поломка тем же способом, что и все опасные здесь: источник
    отвечал «отработал, совпадений нет». Ненайденный по телефону должник
    выглядел как отсутствующий в выгрузке, ИНН из 1С не подтягивался, и
    остальные реестры отказывались искать без него — при том что ТЗ описывает
    ровно этот ввод: «ФИО + номер телефона».

    Нормализация живёт здесь, а не у вызывающих: обе стороны обязаны считать
    хеш одинаково, и договорённость, которую надо помнить в двух местах, уже
    один раз не сработала. Номер, не похожий на российский, хешируется как
    есть — пусть лучше не найдётся, чем совпадёт с чужим.
    """
    if not phone:
        return None
    return stable_hash("phone", normalize_phone(phone) or phone)
