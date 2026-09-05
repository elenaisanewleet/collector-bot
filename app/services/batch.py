"""Массовая проверка выгрузки.

Главный сценарий продукта: у эвакуаторщика восемьсот должников, и вопрос не
«что известно про Иванова», а «на кого из восьмисот тратить госпошлину».
Сервис прогоняет всю внутреннюю базу через тот же самый пайплайн, что и
одиночный поиск, и складывает результат в очередь, отсортированную по вердикту.

Четыре свойства, ради которых он написан именно так:

*   **Прогон стоит денег.** Каждый должник — это реальные запросы к платным
    источникам. Поэтому сначала считается смета, оператор её подтверждает, а
    кэш переиспользуется: повторный прогон на следующий день не оплачивает
    заново тех, кого проверяли вчера.
*   **Один сбой не рушит прогон.** Должник, на котором что-то упало, получает
    запись с ошибкой и не мешает остальным семистам девяноста девяти.
*   **Кончившийся баланс рушит.** Один упавший источник — это дыра в строке, и
    её видно. Источник, который отказывает по деньгам, — это дыра во всех
    оставшихся строках сразу, и прогон обязан остановиться, а не дописать
    шестьсот бодрых «ничего не найдено» за деньги. См. :data:`REFUSAL_CODES`.
*   **Прогон кончается тремя разными способами**, и они не синонимы:
    :class:`RunStatus`. Дошёл до конца, остановлен отказом источника, оборвался
    на сбое — под одной подписью «завершено» это ложь про полноту очереди.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.config import Settings
from app.db.models import BatchItem, BatchRun, Debtor
from app.db.repository import AuditRepository, BatchRepository, DebtorRepository
from app.db.session import Database
from app.domain.enums import SearchType
from app.domain.identity import NameParseError, SearchSubject, parse_fio
from app.domain.models import DebtorReport
from app.domain.verdict import VERDICT_ORDER, Verdict, VerdictDecision
from app.logging_setup import get_logger
from app.providers.newdb import individual_inn
from app.services.search import SearchService, build_query_hash
from app.services.verdict import VerdictEngine

logger = get_logger(__name__)

PAGE_SIZE = 200
ProgressCallback = Callable[["BatchProgress"], Awaitable[None]]

# Коды отказа, после которых продолжать прогон — значит платить за пустоту.
# Оба невосстановимы по своей природе: ``payment_required`` — кончившийся
# баланс, ``unauthorized`` — отклонённый ключ (агрегатор отвечает на оба одним
# сообщением, см. ``providers/newdb.py``). Повтор не чинит ни то, ни другое, а
# главное — вердикт, посчитанный без источника, выглядит на странице ровно так
# же, как посчитанный с ним.
REFUSAL_CODES = frozenset({"payment_required", "unauthorized"})
# Сколько должников должны получить отказ, прежде чем прогон встанет. Не один:
# одиночный отказ бывает и случайным, а три подряд — это уже состояние счёта, а
# не совпадение.
REFUSALS_BEFORE_HALT = 3


class RunStatus(StrEnum):
    """Чем кончился прогон. Три конца, и они не взаимозаменяемы.

    ``FINISHED``     дошёл до последней строки выгрузки;
    ``STOPPED``      остановлен нами: источник отказал по деньгам или по ключу;
    ``INTERRUPTED``  оборвался на сбое, который прогон пережить не смог.

    Строка статуса — единственное, что связывает бота и веб-страницу по этому
    поводу, поэтому причина закодирована статусом, а не свободным текстом: под
    свободный текст нужна колонка, а под два известных исхода — не нужна.
    """

    RUNNING = "running"
    FINISHED = "finished"
    STOPPED = "stopped"
    INTERRUPTED = "interrupted"


class VerdictMoney:
    """Деньги прогона, посчитанные из счётчиков вердиктов.

    Общая часть сводки в чате и среза для веб-страницы. Живёт отдельно ровно
    потому, что чат и страница называют одни и те же числа: посчитанные вторым
    способом, они однажды разойдутся, и оператор увидит в боте одну экономию, а
    на странице другую.
    """

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

    @property
    def actionable(self) -> int:
        return self.count(Verdict.FILE) + self.count(Verdict.ORDER)

    @property
    def actionable_debt(self) -> Decimal:
        return self.debt(Verdict.FILE) + self.debt(Verdict.ORDER)

    @property
    def actionable_fee(self) -> Decimal:
        """Пошлина, которую придётся заплатить, если нести всех окупающихся."""
        return self.fee(Verdict.FILE) + self.fee(Verdict.ORDER)

    @property
    def total_debt(self) -> Decimal:
        return sum((self.debt(verdict) for verdict in Verdict), Decimal("0"))

    @property
    def total_fee(self) -> Decimal:
        """Пошлина по всем строкам, включая ту, что платить не придётся.

        Знаменатель для экономии: без него «сэкономлено 1,2 млн» — число без
        масштаба.
        """
        return sum((self.fee(verdict) for verdict in Verdict), Decimal("0"))


@dataclass(frozen=True, slots=True)
class BatchEstimate:
    """Смета прогона, которую оператор подтверждает до списания средств."""

    debtors: int
    cached: int
    to_query: int
    providers_per_debtor: int
    capped: bool
    # Мост «паспорт → ИНН» — отдельный терм, а не множитель: он стоит один вызов
    # на должника, а не один на источник, и включается собственным флагом.
    bridge_enabled: bool = False
    bridge_calls: int = 0
    # Должники, по которым банкротство, статус ИП и арбитраж не будут проверены
    # вовсе: у них нет ИНН физлица, а взять его неоткуда.
    without_inn: int = 0
    # Строки, по которым искать нечего: ни ФИО, ни номера договора. Они не стоят
    # ни запроса и попадают в очередь как «не проверено». В смете стоят потому,
    # что это единственный момент, когда их ещё можно починить в выгрузке.
    unusable: int = 0
    # Цена одного обращения к платному источнику, ₽. Ноль — цена не задана.
    cost_per_request: Decimal = Decimal("0")

    @property
    def requests(self) -> int:
        return self.to_query * self.providers_per_debtor + self.bridge_calls

    @property
    def cost(self) -> Decimal | None:
        """Во что встанет прогон. ``None`` — цена обращения не настроена.

        Именно ``None``, а не ноль: ноль рублей — это утверждение «бесплатно», и
        смета, сказавшая его вместо «цена не задана», обманывает ровно там, где
        оператор решает, тратить ли деньги.
        """
        if self.cost_per_request <= 0:
            return None
        return self.cost_per_request * self.requests


@dataclass(frozen=True, slots=True)
class _Scan:
    """Итоги одного прохода по выгрузке, нужные смете."""

    cached: int
    without_inn: int
    bridge_calls: int
    unusable: int


@dataclass(slots=True)
class BatchProgress:
    processed: int
    total: int
    failed: int
    # Номер прогона известен с первого же события: очередь наполняется на ходу,
    # и ссылку на неё бот обязан дать сразу, а не через полчаса вместе с итогом.
    run_id: int = 0

    @property
    def percent(self) -> int:
        return round(self.processed * 100 / self.total) if self.total else 0


@dataclass(slots=True)
class BatchSummary(VerdictMoney):
    run_id: int
    total: int
    processed: int
    failed: int
    counts: dict[str, int]
    totals: dict[str, Decimal]
    status: str = RunStatus.FINISHED
    # Источники, которые отказались отвечать по деньгам или по ключу. Названы
    # поимённо: «прогон остановлен» без имени источника оператору нечего чинить.
    refused_sources: tuple[str, ...] = ()
    # Тип исключения, оборвавшего прогон. Не текст ошибки: он уедет в чат, а в
    # тексте вендора бывают и персональные данные, и внутренние адреса.
    error: str | None = None

    @property
    def unchecked(self) -> int:
        """Строки выгрузки, до которых прогон не дошёл.

        Не «ноль найдено» и не ошибка проверки — их просто не проверяли.
        """
        return max(self.total - self.processed, 0)

    @property
    def is_complete(self) -> bool:
        return self.status == RunStatus.FINISHED and self.unchecked == 0


@dataclass(slots=True)
class QueueSnapshot(VerdictMoney):
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
    # Один из :class:`RunStatus`. Страница обязана отличать идущий прогон от
    # завершённого — и оба от прогона, который встал на полпути: те же цифры под
    # теми же подписями — разные утверждения.
    status: str = RunStatus.RUNNING
    # Сколько платных источников опрашивается на одного должника. Нужно
    # странице, чтобы назвать цену прогона в запросах, а не в «уже что-то идёт».
    providers_per_debtor: int = 0

    @property
    def is_running(self) -> bool:
        return self.finished_at is None and self.status == RunStatus.RUNNING

    @property
    def is_torn(self) -> bool:
        """Прогон кончился, не дойдя до конца выгрузки.

        Отдельно от ``is_running``: страница не должна показывать оборванный
        прогон ни как идущий (он не идёт), ни как завершённый (он не дошёл).
        """
        return self.status in {RunStatus.STOPPED, RunStatus.INTERRUPTED}

    @property
    def unchecked(self) -> int:
        return max(self.total - self.processed, 0)


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
                # Счётчик прогона отстаёт от записанных строк ровно на то время,
                # пока пишется страница результатов; берём большее из двух,
                # иначе оборванный прогон показывает ноль проверенных при полной
                # очереди на экране.
                processed=max(run.processed, len(items)),
                failed=max(run.failed, sum(1 for item in items if item.error)),
                counts=counts,
                totals=totals,
                items=items,
                status=run.status,
                providers_per_debtor=len(self._search.registry.configured_names),
            )

    # ------------------------------------------------------------- estimate

    async def estimate(self) -> BatchEstimate:
        """Сколько должников и сколько запросов будет стоить прогон."""
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
            # Строки без ФИО и без договора не доходят до источников, поэтому
            # они не стоят ни обращения и в оплачиваемое число не входят.
            to_query=max(debtors - scan.cached - scan.unusable, 0),
            providers_per_debtor=providers,
            capped=total > cap,
            bridge_enabled=bridge is not None and bridge.is_configured,
            bridge_calls=scan.bridge_calls,
            without_inn=scan.without_inn,
            unusable=scan.unusable,
            cost_per_request=self._settings.provider_request_cost,
        )

    async def _scan(self, limit: int) -> _Scan:
        """Один проход по выгрузке: кэш, отсутствие ИНН и вызовы моста.

        ``bridge_calls`` считается честно — по тому, дошло бы дело до платного
        вызова, — а не подставляется нулём. Сегодня он всё равно выходит нулевым:
        у ``Debtor`` нет паспортной колонки, ``_subject_for`` паспорт не
        заполняет, и брать его в массовом прогоне неоткуда. Захардкоженный ноль
        стал бы враньём в тот день, когда колонка появится; посчитанный —
        просто изменится.
        """
        from app.db.repository import SearchRepository

        bridge = self._search.registry.inn_bridge
        cached = 0
        without_inn = 0
        bridge_calls = 0
        unusable = 0
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
                        # Искать нечего. Раньше такая строка молча пропускалась
                        # и всплывала только в очереди, после того как за прогон
                        # уже заплатили; чинится она правкой выгрузки, то есть
                        # до запуска, то есть знать о ней надо в смете.
                        unusable += 1
                        continue
                    if individual_inn(subject) is None:
                        without_inn += 1
                    is_cached = False
                    if self._settings.cache_enabled:
                        found = await search_repo.find_cached_request(
                            build_query_hash(subject),
                            ttl_hours=self._settings.cache_ttl_hours,
                        )
                        is_cached = found is not None
                    if is_cached:
                        cached += 1
                    elif bridge is not None and bridge.will_query(subject):
                        bridge_calls += 1
        return _Scan(
            cached=cached,
            without_inn=without_inn,
            bridge_calls=bridge_calls,
            unusable=unusable,
        )

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

        state = BatchProgress(processed=0, total=total, failed=0, run_id=run_id)
        semaphore = asyncio.Semaphore(self._settings.batch_concurrency)
        lock = asyncio.Lock()
        halt = _Halt()
        # Первое событие — до единого запроса. Оно несёт номер прогона, а с ним
        # и ссылку на очередь: страница заполняется на ходу, и ждать полчаса,
        # чтобы её открыть, незачем.
        if progress is not None:
            await progress(BatchProgress(0, total, 0, run_id=run_id))

        status = RunStatus.FINISHED
        error: str | None = None
        try:
            for page in _pages(total):
                async with self._database.session() as session:
                    rows = await DebtorRepository(session).iter_all(
                        limit=page.size, offset=page.offset
                    )
                    # Держим только то, что нужно: сессия закроется до сетевых вызовов.
                    snapshot = [_snapshot(row, telegram_user_id) for row in rows]
                if not snapshot:
                    break

                # Сеть — параллельно, запись — одной транзакцией на страницу.
                # Отдельная транзакция на должника даёт восемьсот транзакций и
                # конкуренцию за запись; на SQLite это ещё и теряет строки.
                queue_rows = await asyncio.gather(
                    *(
                        self._process(item, run_id, semaphore, state, lock, progress, halt)
                        for item in snapshot
                    )
                )
                async with self._database.session() as session:
                    repo = BatchRepository(session)
                    # ``None`` — должник, которого не стали проверять после
                    # остановки. Строки у него нет намеренно: пустая строка
                    # «не проверено» неотличима от провалившейся проверки, а это
                    # разные вещи — до него просто не дошли.
                    for queue_row in queue_rows:
                        if queue_row is not None:
                            await repo.add_item(queue_row)
                    # Счётчик прогона обновляется вместе со страницей результатов, а
                    # не только в конце: иначе веб-страница все полчаса показывает
                    # «обработано 0», хотя очередь под ней уже наполовину заполнена.
                    await repo.update_progress(
                        run_id, processed=state.processed, failed=state.failed
                    )
                if halt.should_stop():
                    break
        except asyncio.CancelledError:
            # Бота остановили посреди прогона. Пометить прогон надо всё равно:
            # иначе он навсегда останется «идущим», и страница будет ждать
            # строк, которых уже никто не напишет.
            #
            # Отметка делается «по возможности»: процесс уже гасят, и запись
            # может не успеть. Сбой здесь подавляется намеренно — заслонить им
            # отмену значило бы соврать вызывающему о причине. Последний рубеж
            # на этот случай остаётся у страницы: прогон, не написавший ни
            # строки десять минут, она объявляет оборвавшимся сама.
            with suppress(Exception):
                await self._close(run_id, state, RunStatus.INTERRUPTED, telegram_user_id)
            raise
        except Exception as exc:
            # Прогон не пережил сбоя — но посчитанное до сбоя остаётся верным и
            # уже лежит в очереди. Поэтому здесь не «упасть», а «закрыться
            # честно»: вызывающему уходит сводка по тому, что успели.
            logger.exception("batch.torn", run_id=run_id, error=type(exc).__name__)
            status = RunStatus.INTERRUPTED
            error = type(exc).__name__
        else:
            if halt.should_stop():
                status = RunStatus.STOPPED

        counts, totals = await self._close(run_id, state, status, telegram_user_id)
        logger.info(
            "batch.done",
            run_id=run_id,
            status=status.value,
            processed=state.processed,
            failed=state.failed,
        )
        return BatchSummary(
            run_id=run_id,
            total=total,
            processed=state.processed,
            failed=state.failed,
            counts=counts,
            totals=totals,
            status=status.value,
            refused_sources=halt.sources,
            error=error,
        )

    async def _close(
        self,
        run_id: int,
        state: BatchProgress,
        status: RunStatus,
        telegram_user_id: int,
    ) -> tuple[dict[str, int], dict[str, Decimal]]:
        """Закрыть прогон и снять итоговые счётчики."""
        async with self._database.session() as session:
            repo = BatchRepository(session)
            await repo.finish_run(
                run_id,
                processed=state.processed,
                failed=state.failed,
                status=status.value,
            )
            counts = await repo.verdict_counts(run_id)
            totals = await repo.verdict_totals(run_id)
            await AuditRepository(session).record(
                telegram_user_id=telegram_user_id,
                action=f"batch.{status.value}",
                entity_id=str(run_id),
                detail=(f"обработано {state.processed} из {state.total}, ошибок {state.failed}"),
            )
        return counts, totals

    async def _process(
        self,
        item: DebtorSnapshot,
        run_id: int,
        semaphore: asyncio.Semaphore,
        state: BatchProgress,
        lock: asyncio.Lock,
        progress: ProgressCallback | None,
        halt: _Halt,
    ) -> BatchItem | None:
        """Проверить одного должника и вернуть строку очереди для записи.

        ``None`` — должника не проверяли: прогон уже остановлен. Проверка стоит
        денег, и списывать их за ответы, которых источник всё равно не даст, —
        ровно то, ради чего остановка и заведена.
        """
        if halt.should_stop():
            return None
        async with semaphore:
            # Флаг мог подняться, пока должник ждал очереди на семафоре: между
            # входом в страницу и своим запросом он стоит минуты.
            if halt.should_stop():
                return None
            checked = await self._check(item)

        async with lock:
            if checked.refused_by:
                halt.note(checked.refused_by)
            state.processed += 1
            if checked.error is not None:
                state.failed += 1
            should_report = progress is not None and (
                state.processed % self._settings.batch_progress_every == 0
                or state.processed == state.total
            )
        if should_report and progress is not None:
            await progress(BatchProgress(state.processed, state.total, state.failed, run_id=run_id))
        return _to_row(run_id, item, checked.decision, checked.score, checked.error)

    async def _check(self, item: DebtorSnapshot) -> _Checked:
        """Проверить одного должника. Ошибка возвращается, а не бросается."""
        if item.subject is None:
            return _Checked(error="в карточке нет ни ФИО, ни номера договора")
        try:
            report = await self._search.search(item.subject, telegram_user_id=item.telegram_user_id)
        except Exception as exc:
            logger.warning(
                "batch.debtor_failed", debtor_id=item.debtor_id, error=type(exc).__name__
            )
            return _Checked(error=f"{type(exc).__name__}")
        score = report.recovery_score.score if report.recovery_score else None
        return _Checked(
            decision=self._verdict.decide(report),
            score=score,
            refused_by=_refusals(report),
        )


# ---------------------------------------------------------------- отказ источника


@dataclass(frozen=True, slots=True)
class _Checked:
    """Что вернула проверка одного должника."""

    decision: VerdictDecision | None = None
    score: int | None = None
    error: str | None = None
    # Источники, которые отказались отвечать по деньгам или по ключу. Отдельно
    # от ``error``: должник проверен, вердикт есть — просто посчитан не по всему.
    refused_by: tuple[str, ...] = ()


@dataclass(slots=True)
class _Halt:
    """Счётчик отказов и решение остановить прогон.

    Живёт под тем же замком, что и счётчики прогресса: страница в двести
    должников идёт параллельно, и без замка порог проскакивают несколько
    корутин сразу.
    """

    debtors: int = 0
    sources: tuple[str, ...] = ()
    stopped: bool = False

    def note(self, refused_by: tuple[str, ...]) -> None:
        self.debtors += 1
        self.sources = tuple(dict.fromkeys((*self.sources, *refused_by)))
        if self.debtors >= REFUSALS_BEFORE_HALT and not self.stopped:
            self.stopped = True
            logger.warning("batch.halted", sources=list(self.sources), debtors=self.debtors)

    def should_stop(self) -> bool:
        """Спрашивается методом, а не полем, и это не украшение.

        Флаг поднимает соседняя корутина, поэтому его перечитывают в двух
        местах подряд — до очереди на семафоре и после неё. Прочитанный как
        поле, второй раз он выглядит для проверок типов уже известным, и
        повторная проверка тихо объявляется недостижимой.
        """
        return self.stopped


def _refusals(report: DebtorReport) -> tuple[str, ...]:
    """Источники отчёта, отказавшиеся отвечать по деньгам или по ключу.

    Ответы из кэша сюда не попадают: они ничего не стоили и говорят о вчерашнем
    состоянии счёта, а останавливать сегодняшний прогон надо по сегодняшнему.
    """
    if report.from_cache:
        return ()
    return tuple(
        dict.fromkeys(
            result.provider.value
            for result in report.provider_results
            if result.error_code in REFUSAL_CODES
        )
    )


# ---------------------------------------------------------------- snapshots


@dataclass(frozen=True, slots=True)
class DebtorSnapshot:
    """Данные должника, отвязанные от сессии БД."""

    debtor_id: int
    telegram_user_id: int
    subject: SearchSubject | None


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
