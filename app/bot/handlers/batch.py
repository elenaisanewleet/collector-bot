"""``/batch`` — массовая проверка всей выгрузки.

Три вещи, которые отличают этот флоу от одиночного поиска:

*   Смета до запуска. Прогон тратит платные запросы, поэтому оператор сначала
    видит, сколько должников и сколько обращений к источникам это будет.
*   Прогресс правится на месте. Одно сообщение, которое обновляется, вместо
    восьмисот новых — иначе чат станет нечитаемым уже на сотом должнике.
*   Результат — очередь, а не отчёт. Сначала сводка «сколько на что», потом
    списки по вердиктам и выгрузка в CSV.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message

from app.bot import view
from app.bot.common import answer_callback, callback_message
from app.bot.keyboards import (
    BATCH_PREFIX,
    batch_confirm_keyboard,
    batch_result_keyboard_with_link,
    main_menu,
)
from app.bot.states import BatchCheck
from app.container import Container
from app.db.repository import BatchRepository
from app.domain.verdict import VERDICT_TITLES, Verdict
from app.logging_setup import get_logger
from app.services.batch import BatchEstimate, BatchProgress, BatchSummary
from app.services.export import queue_to_csv
from app.services.share import ShareKind, ShareTarget
from app.utils.formatting import pluralize_ru, split_message
from app.utils.money import format_amount

logger = get_logger(__name__)

EMPTY_BASE = (
    "Внутренняя база пуста. Загрузите выгрузку должников через /import, и запускайте проверку."
)
NO_RUN = "Прогонов ещё не было. Запустите проверку через /batch."
LIST_PAGE_SIZE = 15


def render_estimate(estimate: BatchEstimate, app_name: str) -> str:
    noun = pluralize_ru(estimate.debtors, "должник", "должника", "должников")
    lines = [
        "Массовая проверка",
        "",
        f"В базе: {estimate.debtors} {noun}",
        f"Уже проверено недавно: {estimate.cached} — будут взяты из кэша",
        f"Нужно опросить: {estimate.to_query}",
    ]
    if estimate.providers_per_debtor:
        lines.append(
            f"Обращений к источникам: около {estimate.requests} "
            f"({estimate.providers_per_debtor} на должника)"
        )
    else:
        lines.append("Внешние источники не подключены — проверка пройдёт по внутренней базе.")
    if estimate.capped:
        lines.append("")
        lines.append("Прогон ограничен настройкой BATCH_MAX_DEBTORS.")
    lines.append("")
    lines.append("Запросы к платным источникам списываются с вашего баланса.")
    return "\n".join(lines)


def render_progress(progress: BatchProgress) -> str:
    return view.batch_progress(progress.processed, progress.total, progress.failed)


def render_summary(summary: BatchSummary) -> str:
    lines = ["Проверка завершена", ""]
    for verdict in (Verdict.FILE, Verdict.ORDER, Verdict.REVIEW, Verdict.DROP):
        count = summary.count(verdict)
        if not count:
            continue
        debt = summary.debt(verdict)
        row = f"{VERDICT_TITLES[verdict]}: {count}"
        if debt:
            row += f" — на {format_amount(debt)}"
        lines.append(row)

    if summary.saved_fees:
        lines.append("")
        lines.append(
            f"Не будет потрачено на пошлины по безнадёжным: {format_amount(summary.saved_fees)}"
        )
    if summary.failed:
        lines.append("")
        lines.append(f"Не удалось проверить: {summary.failed}")
    lines.append("")
    lines.append("Оценка аналитическая и не заменяет юридическую проверку.")
    return "\n".join(lines)


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="batch")

    @router.message(Command("batch"))
    async def handle_batch_command(
        message: Message, state: FSMContext, container: Container
    ) -> None:
        await _offer(message, state, container)

    @router.callback_query(F.data == f"{BATCH_PREFIX}:start")
    async def handle_batch_start(
        callback: CallbackQuery, state: FSMContext, container: Container
    ) -> None:
        await answer_callback(callback)
        target = callback_message(callback)
        if target:
            await _offer(target, state, container)

    async def _offer(message: Message, state: FSMContext, container: Container) -> None:
        estimate = await container.batch_service.estimate()
        if estimate.debtors == 0:
            await state.clear()
            await message.answer(EMPTY_BASE, reply_markup=main_menu())
            return
        await state.set_state(BatchCheck.waiting_confirm)
        await message.answer(
            render_estimate(estimate, container.settings.app_name),
            reply_markup=batch_confirm_keyboard(),
        )

    @router.callback_query(BatchCheck.waiting_confirm, F.data == f"{BATCH_PREFIX}:run")
    async def handle_batch_run(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await state.clear()
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return

        notice = await message.answer(
            render_progress(BatchProgress(processed=0, total=1, failed=0))
        )
        last = ""

        async def report(progress: BatchProgress) -> None:
            nonlocal last
            text = render_progress(progress)
            if text == last:
                return
            last = text
            try:
                await notice.edit_text(text)
            except Exception:
                logger.debug("batch.progress_edit_failed")

        summary = await container.batch_service.run(telegram_user_id=user_id, progress=report)
        # Ссылка на веб-очередь — главное действие после прогона: таблицу на
        # восемьсот строк в сообщении Telegram не показать.
        url = await container.share_service.issue(
            ShareTarget(ShareKind.QUEUE, summary.run_id), telegram_user_id=user_id
        )
        keyboard = batch_result_keyboard_with_link(url)
        try:
            await notice.edit_text(render_summary(summary), reply_markup=keyboard)
        except Exception:
            await message.answer(render_summary(summary), reply_markup=keyboard)

    @router.callback_query(F.data.startswith(f"{BATCH_PREFIX}:list:"))
    async def handle_batch_list(
        callback: CallbackQuery, container: Container, user_id: int
    ) -> None:
        raw = (callback.data or "").rsplit(":", maxsplit=1)[-1]
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        try:
            verdict = Verdict(raw)
        except ValueError:
            return

        async with container.database.session() as session:
            repo = BatchRepository(session)
            run = await repo.latest_run(user_id)
            if run is None:
                await message.answer(NO_RUN)
                return
            items = await repo.queue(run.id, verdict=verdict.value, limit=LIST_PAGE_SIZE)
            total = (await repo.verdict_counts(run.id)).get(verdict.value, 0)
            rows = [_queue_line(index, item) for index, item in enumerate(items, start=1)]

        if not rows:
            await message.answer(f"{VERDICT_TITLES[verdict]}: пусто.")
            return
        header = f"{VERDICT_TITLES[verdict]} — {total}"
        if total > len(rows):
            header += f" (показаны первые {len(rows)}, полный список — в CSV)"
        for chunk in split_message("\n\n".join([header, *rows])):
            await message.answer(chunk)

    @router.callback_query(F.data == f"{BATCH_PREFIX}:export")
    async def handle_batch_export(
        callback: CallbackQuery, container: Container, user_id: int
    ) -> None:
        await answer_callback(callback, "Готовлю файл…")
        message = callback_message(callback)
        if message is None:
            return

        async with container.database.session() as session:
            repo = BatchRepository(session)
            run = await repo.latest_run(user_id)
            if run is None:
                await message.answer(NO_RUN)
                return
            items = await repo.queue(run.id, limit=container.settings.batch_max_debtors)
            payload = queue_to_csv(
                items, include_phone=container.settings.store_sensitive_identifiers
            )
            run_id = run.id

        await message.answer_document(
            BufferedInputFile(payload, filename=f"ochered-{run_id}.csv"),
            caption=(
                f"Очередь взыскания, прогон №{run_id}: {len(items)} строк. Телефоны маскированы."
                if not container.settings.store_sensitive_identifiers
                else f"Очередь взыскания, прогон №{run_id}: {len(items)} строк."
            ),
        )

    return router


def _queue_line(index: int, item) -> str:  # type: ignore[no-untyped-def]
    debtor = item.debtor
    name = (debtor.fio if debtor else None) or (debtor.contract_number if debtor else None) or "—"
    parts = [f"{index}. {name}"]
    if item.debt_amount is not None:
        fee = f" · пошлина {format_amount(item.state_fee)}" if item.state_fee else ""
        parts.append(f"   Долг {format_amount(item.debt_amount)}{fee}")
    if debtor and debtor.contract_number:
        parts.append(f"   Договор {debtor.contract_number}")
    parts.append(f"   {item.headline}")
    return "\n".join(parts)
