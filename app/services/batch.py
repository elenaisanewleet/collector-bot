"""Массовая проверка выгрузки.

Главный сценарий продукта: у эвакуаторщика восемьсот должников, и вопрос не
«что известно про Иванова», а «на кого из восьмисот тратить госпошлину».
Сервис прогоняет всю внутреннюю базу через тот же самый пайплайн, что и
одиночный поиск, и складывает результат в очередь, отсортированную по вердикту.

Два свойства, ради которых он написан именно так:

*   **Прогон стоит денег.** Каждый должник — это реальные запросы к платным
    источникам. Поэтому сначала считается смета, оператор её подтверждает, а
    кэш переиспользуется: повторный прогон на следующий день не оплачивает
    заново тех, кого проверяли вчера.
*   **Один сбой не рушит прогон.** Должник, на котором что-то упало, получает
    запись с ошибкой и не мешает остальным семистам девяноста девяти.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime
from decimal import Decimal

from app.config import Settings
from app.db.models import BatchItem, BatchRun, Debtor
from app.db.repository import AuditRepository, BatchRepository, DebtorRepository
from app.db.session import Database
from app.domain.enums import ProviderName, SearchType
from app.domain.identity import NameParseError, SearchSubject, parse_fio
from app.domain.verdict import VERDICT_ORDER, Verdict, VerdictDecision
from app.logging_setup import get_logger
from app.providers.base import FetchContext
from app.providers.newdb import individual_inn
from app.services.search import SearchService, build_query_hash
from app.services.verdict import VerdictEngine

logger = get_logger(__name__)

PAGE_SIZE = 200
ProgressCallback = Callable[["BatchProgress"], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class BatchEstimate:
    """Смета прогона, которую оператор подтверждает до списания средств.

    Считаются **вызовы**, а не источники. Прежняя формула «должники × число
    подключённых источников» была неправдой уже без новых методов: ФССП делает
    вызов на регион, залоги — до двух, а источник, которому нечем искать, не
    делает ни одного. С веером по компаниям расхождение стало бы кратным, и
    оператор подтверждал бы одну сумму, а списывалась бы другая.

    Диапазон нужен из-за цепочки: её длина известна только после ответа ФНС,
    поэтому минимум — это прогон без неё, максимум — с колпаком.
    """

    debtors: int
    cached: int
    to_query: int
    providers_per_debtor: int
    capped: bool
    calls_min: int = 0
    calls_max: int = 0
    per_provider: dict[ProviderName, int] = dataclass_field(default_factory=dict)
    # Мост «паспорт → ИНН» — отдельный терм, а не строка per_provider: он стоит
    # один вызов на должника, а не один на источник, и включается своим флагом.
    bridge_enabled: bool = False
    bridge_calls: int = 0
    # Должники, по которым банкротство, статус ИП и арбитраж не будут проверены
    # вовсе: у них нет ИНН физлица, а взять его неоткуда.
    without_inn: int = 0

    @property
    def requests(self) -> int:
        """Верхняя граница — то, к чему оператор должен быть готов."""
        return self.calls_max + self.bridge_calls

    @property
    def is_range(self) -> bool:
        return self.calls_max > self.calls_min


@dataclass(slots=True)
class BatchProgress:
    processed: int
    total: int
    failed: int

    @property
    def percent(self) -> int:
        return round(self.processed * 100 / self.total) if self.total else 0


@dataclass(slots=True)
class BatchSummary:
    run_id: int
    total: int
    processed: int
    failed: int
    counts: dict[str, int]
    totals: dict[str, Decimal]

    def count(self, verdict: Verdict) -> int:
        return self.counts.get(verdict.value, 0)

    def debt(self, verdict: Verdict) -> Decimal:
        return self.totals.get(f"{verdict.value}:debt", Decimal("0"))

    def fee(self, verdict: Verdict) -> Decimal:
        return self.totals.get(f"{verdict.value}:fee", Decimal("0"))

    @property
    def saved_fees(self) -> Decimal:
        """Пошлины, которые не будут уплачены по безнадёжным должникам.

        Считается по ставке, которая применялась бы к иску: это то, что прогон
        уберёг от списания в никуда.
        """
        return self.fee(Verdict.DROP)


@dataclass(slots=True)
class QueueSnapshot:
    """Очередь одного прогона целиком — то, что показывает веб-страница."""

    run_id: int
    started_at: datetime
    finished_at: datetime | None
    total: int
    processed: int
    failed: int
    counts: dict[str, int]
    totals: dict[str, Decimal]
    items: list[BatchItem]

    def count(self, verdict: Verdict) -> int:
        return self.counts.get(verdict.value, 0)

    def debt(self, verdict: Verdict) -> Decimal:
        return self.totals.get(f"{verdict.value}:debt", Decimal("0"))

    def fee(self, verdict: Verdict) -> Decimal:
        return self.totals.get(f"{verdict.value}:fee", Decimal("0"))

    @property
    def saved_fees(self) -> Decimal:
        return self.fee(Verdict.DROP)

    @property
    def actionable(self) -> int:
        return self.count(Verdict.FILE) + self.count(Verdict.ORDER)

    @property
    def actionable_debt(self) -> Decimal:
        return self.debt(Verdict.FILE) + self.debt(Verdict.ORDER)


class BatchService:
    """Прогон всей выгрузки и построение очереди."""

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        search_service: SearchService,
        verdict_engine: VerdictEngine | None = None,
    ) -> None:
        self._settings = settings
        self._database = database
        self._search = search_service
        self._verdict = verdict_engine or VerdictEngine(settings)

    async def queue_snapshot(
        self, run_id: int, *, limit: int | None = None
    ) -> QueueSnapshot | None:
        """Собрать очередь прогона для веб-страницы."""
        async with self._database.session() as session:
            repo = BatchRepository(session)
            run = await session.get(BatchRun, run_id)
            if run is None:
                return None
            items = await repo.queue(run_id, limit=limit or self._settings.batch_max_debtors)
            counts = await repo.verdict_counts(run_id)
            totals = await repo.verdict_totals(run_id)
            return QueueSnapshot(
                run_id=run.id,
                started_at=run.started_at,
                finished_at=run.finished_at,
                total=run.total,
                processed=run.processed,
                failed=run.failed,
                counts=counts,
                totals=totals,
                items=items,
            )

    # ------------------------------------------------------------- estimate

    async def estimate(self) -> BatchEstimate:
        """Сколько должников и сколько вызовов будет стоить прогон."""
        cap = self._settings.batch_max_debtors
        async with self._database.session() as session:
            total = await DebtorRepository(session).count()
        debtors = min(total, cap)

        scan = await self._scan(debtors)
        providers = len(self._search.registry.configured_names)
        bridge = self._search.registry.inn_bridge
        return BatchEstimate(
            debtors=debtors,
            cached=scan.cached,
            to_query=max(debtors - scan.cached, 0),
            providers_per_debtor=providers,
            capped=total > cap,
            calls_min=scan.calls_min,
            calls_max=scan.calls_max,
            per_provider=dict(scan.per_provider),
            bridge_enabled=bridge is not None and bridge.is_configured,
            bridge_calls=scan.bridge_calls,
            without_inn=scan.without_inn,
        )

    async def _scan(self, limit: int) -> _Scan:
        """Один проход по выгрузке: и кэш, и планируемые вызовы.

        Считается в том же цикле, что уже был написан ради кэша: лишних
        запросов к БД смета не стоит, а без неё оператор подтверждает цифру,
        которая с фактическим списанием не совпадает.

        ``bridge_calls`` считается честно — по тому, дошло бы дело до платного
        вызова, — а не подставляется нулём. Сегодня он всё равно выходит
        нулевым: у ``Debtor`` нет паспортной колонки, и брать паспорт в массовом
        прогоне неоткуда. Захардкоженный ноль стал бы враньём в тот день, когда
        колонка появится; посчитанный — просто изменится.
        """
        from app.db.repository import SearchRepository

        scan = _Scan()
        context = FetchContext(batch=True)
        providers = self._search.registry.external
        bridge = self._search.registry.inn_bridge
        cache_enabled = self._settings.cache_enabled
        async with self._database.session() as session:
            debtor_repo = DebtorRepository(session)
            search_repo = SearchRepository(session)
            for page in _pages(limit):
                rows = await debtor_repo.iter_all(limit=page.size, offset=page.offset)
                if not rows:
                    break
                for row in rows:
                    subject = _subject_for(row)
                    if subject is None:
                        continue
                    if individual_inn(subject) is None:
                        scan.without_inn += 1
                    if cache_enabled:
                        found = await search_repo.find_cached_request(
                            build_query_hash(subject),
                            ttl_hours=self._settings.cache_ttl_hours,
                        )
                        if found is not None:
                            # Кэшированный должник не опрашивается — и не стоит.
                            scan.cached += 1
                            continue
                    if bridge is not None and bridge.will_query(subject):
                        scan.bridge_calls += 1
                    for provider in providers:
                        planned = provider.planned_calls(subject, context)
                        ceiling = provider.max_planned_calls(subject, context)
                        scan.calls_min += planned
                        scan.calls_max += ceiling
                        if ceiling:
                            scan.per_provider[provider.name] = (
                                scan.per_provider.get(provider.name, 0) + ceiling
                            )
        return scan

    # ------------------------------------------------------------- run

    async def run(
        self,
        *,
        telegram_user_id: int,
        progress: ProgressCallback | None = None,
    ) -> BatchSummary:
        """Прогнать выгрузку и построить очередь."""
        estimate = await self.estimate()
        total = estimate.debtors

        async with self._database.session() as session:
            run = await BatchRepository(session).create_run(
                telegram_user_id=telegram_user_id, total=total
            )
            run_id = run.id
            await AuditRepository(session).record(
                telegram_user_id=telegram_user_id,
                action="batch.started",
                entity_id=str(run_id),
                detail=f"должников {total}",
            )

        state = BatchProgress(processed=0, total=total, failed=0)
        semaphore = asyncio.Semaphore(self._settings.batch_concurrency)
        lock = asyncio.Lock()

        for page in _pages(total):
            async with self._database.session() as session:
                rows = await DebtorRepository(session).iter_all(limit=page.size, offset=page.offset)
                # Держим только то, что нужно: сессия закроется до сетевых вызовов.
                snapshot = [_snapshot(row, telegram_user_id) for row in rows]
            if not snapshot:
                break

            # Сеть — параллельно, запись — одной транзакцией на страницу.
            # Отдельная транзакция на должника даёт восемьсот транзакций и
            # конкуренцию за запись; на SQLite это ещё и теряет строки.
            queue_rows = await asyncio.gather(
                *(
                    self._process(item, run_id, semaphore, state, lock, progress)
                    for item in snapshot
                )
            )
            async with self._database.session() as session:
                repo = BatchRepository(session)
                for queue_row in queue_rows:
                    await repo.add_item(queue_row)

        async with self._database.session() as session:
            repo = BatchRepository(session)
            await repo.finish_run(run_id, processed=state.processed, failed=state.failed)
            counts = await repo.verdict_counts(run_id)
            totals = await repo.verdict_totals(run_id)
            await AuditRepository(session).record(
                telegram_user_id=telegram_user_id,
                action="batch.finished",
                entity_id=str(run_id),
                detail=f"обработано {state.processed}, ошибок {state.failed}",
            )

        logger.info("batch.finished", run_id=run_id, processed=state.processed, failed=state.failed)
        return BatchSummary(
            run_id=run_id,
            total=total,
            processed=state.processed,
            failed=state.failed,
            counts=counts,
            totals=totals,
        )

    async def _process(
        self,
        item: DebtorSnapshot,
        run_id: int,
        semaphore: asyncio.Semaphore,
        state: BatchProgress,
        lock: asyncio.Lock,
        progress: ProgressCallback | None,
    ) -> BatchItem:
        """Проверить одного должника и вернуть строку очереди для записи."""
        async with semaphore:
            decision, score, error = await self._check(item)

        async with lock:
            state.processed += 1
            if error is not None:
                state.failed += 1
            should_report = progress is not None and (
                state.processed % self._settings.batch_progress_every == 0
                or state.processed == state.total
            )
        if should_report and progress is not None:
            await progress(BatchProgress(state.processed, state.total, state.failed))
        return _to_row(run_id, item, decision, score, error)

    async def _check(
        self, item: DebtorSnapshot
    ) -> tuple[VerdictDecision | None, int | None, str | None]:
        """Проверить одного должника. Ошибка возвращается, а не бросается."""
        if item.subject is None:
            return None, None, "в карточке нет ни ФИО, ни номера договора"
        try:
            report = await self._search.search(
                item.subject, telegram_user_id=item.telegram_user_id, batch=True
            )
        except Exception as exc:
            logger.warning(
                "batch.debtor_failed", debtor_id=item.debtor_id, error=type(exc).__name__
            )
            return None, None, f"{type(exc).__name__}"
        score = report.recovery_score.score if report.recovery_score else None
        return self._verdict.decide(report), score, None


# ---------------------------------------------------------------- snapshots


@dataclass(frozen=True, slots=True)
class DebtorSnapshot:
    """Данные должника, отвязанные от сессии БД."""

    debtor_id: int
    telegram_user_id: int
    subject: SearchSubject | None


@dataclass(slots=True)
class _Scan:
    """Итог одного прохода по выгрузке."""

    cached: int = 0
    calls_min: int = 0
    calls_max: int = 0
    per_provider: dict[ProviderName, int] = dataclass_field(default_factory=dict)
    without_inn: int = 0
    bridge_calls: int = 0


@dataclass(frozen=True, slots=True)
class _Page:
    offset: int
    size: int


def _pages(total: int, size: int = PAGE_SIZE) -> list[_Page]:
    return [_Page(offset, min(size, total - offset)) for offset in range(0, total, size)]


def _snapshot(row: Debtor, telegram_user_id: int) -> DebtorSnapshot:
    return DebtorSnapshot(
        debtor_id=row.id,
        telegram_user_id=telegram_user_id,
        subject=_subject_for(row),
    )


def _subject_for(row: Debtor) -> SearchSubject | None:
    """Собрать субъект поиска из строки выгрузки.

    Без имени и без номера договора искать нечего — такая строка попадает в
    очередь как ошибка, а не как «ничего не найдено».
    """
    name = None
    if row.fio:
        try:
            name = parse_fio(row.fio)
        except NameParseError:
            name = None
    if name is None and not row.contract_number:
        return None
    return SearchSubject(
        search_type=SearchType.PERSON.value if name else SearchType.CONTRACT.value,
        name=name,
        birth_date=row.birth_date,
        phone=row.phone,
        inn=row.inn,
        contract_number=row.contract_number,
        claim_number=row.claim_number,
        debtor_id=row.external_debtor_id,
        address=row.address,
    )


def _to_row(
    run_id: int,
    item: DebtorSnapshot,
    decision: VerdictDecision | None,
    score: int | None,
    error: str | None,
) -> BatchItem:
    if decision is None:
        return BatchItem(
            batch_run_id=run_id,
            debtor_id=item.debtor_id,
            verdict=Verdict.REVIEW.value,
            verdict_order=VERDICT_ORDER[Verdict.REVIEW],
            headline="Проверка не выполнена",
            error=error,
        )
    return BatchItem(
        batch_run_id=run_id,
        debtor_id=item.debtor_id,
        verdict=decision.verdict.value,
        verdict_order=decision.sort_key,
        headline=decision.headline[:512],
        reasons_json=json.dumps(
            [reason.model_dump(mode="json") for reason in decision.reasons],
            ensure_ascii=False,
        ),
        debt_amount=decision.debt_amount,
        debt_kopecks=_to_kopecks(decision.debt_amount),
        state_fee=decision.state_fee,
        fee_basis=decision.fee_basis.value,
        score=score,
        confidence=round(decision.confidence * 100),
        error=error,
    )


def _to_kopecks(amount: Decimal | None) -> int:
    """Целочисленный ключ сортировки: копейки, без потерь на float."""
    return int((amount * 100).to_integral_value()) if amount is not None else 0


def snapshots_for(rows: Sequence[Debtor], telegram_user_id: int = 0) -> list[DebtorSnapshot]:
    """Публичная обёртка для тестов и будущего API."""
    return [_snapshot(row, telegram_user_id) for row in rows]
