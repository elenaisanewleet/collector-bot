"""``/history`` — the operator's last searches, and re-running one.

Only masked queries are shown, because that is all that was stored.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.common import answer_callback, callback_message, run_and_send_report
from app.bot.keyboards import (
    MENU_PREFIX,
    REFRESH_PREFIX,
    REPEAT_PREFIX,
    history_keyboard,
    main_menu,
)
from app.container import Container
from app.db.repository import HISTORY_PAGE_SIZE, SearchRepository
from app.domain.enums import SEARCH_TYPE_TITLES, SearchType
from app.domain.identity import SearchSubject
from app.services.reporting import render_history_line, render_provider_summary
from app.services.search import subject_from_json
from app.utils.dates import format_datetime
from app.utils.formatting import split_message

EMPTY_HISTORY = "История пуста. Запустите первую проверку через /search."
STALE_TOKEN = "Данные устарели, запустите поиск заново."


async def _send_history(message: Message, container: Container, user_id: int) -> None:
    async with container.database.session() as session:
        repo = SearchRepository(session)
        requests = await repo.recent_for_user(user_id, limit=HISTORY_PAGE_SIZE)
        entries: list[tuple[str, int]] = []
        lines: list[str] = []
        for index, request in enumerate(requests, start=1):
            results = await repo.results_for_request(request.id)
            report_row = await repo.report_for_request(request.id)
            lines.append(
                render_history_line(
                    index,
                    created_at=format_datetime(request.created_at),
                    search_type=_search_type_title(request.search_type),
                    masked_query=request.masked_query,
                    score=report_row.score if report_row else None,
                    category=report_row.category if report_row else None,
                    provider_summary=render_provider_summary(
                        [(row.provider, row.provider_status) for row in results]
                    ),
                )
            )
            entries.append((request.masked_query, request.id))

    if not lines:
        await message.answer(EMPTY_HISTORY, reply_markup=main_menu())
        return

    text = "🕘 Последние проверки\n\n" + "\n\n".join(lines)
    chunks = split_message(text)
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(
        chunks[-1],
        reply_markup=_repeat_keyboard(entries),
    )


def _repeat_keyboard(entries: list[tuple[str, int]]) -> InlineKeyboardMarkup:
    return history_keyboard(
        [(index, str(request_id)) for index, (_, request_id) in enumerate(entries, start=1)]
    )


def _search_type_title(value: str) -> str:
    try:
        return SEARCH_TYPE_TITLES[SearchType(value)]
    except (ValueError, KeyError):
        return value


async def _subject_from_request(
    container: Container, raw_id: str, user_id: int
) -> SearchSubject | None:
    """Load the stored query behind a history entry.

    The ownership check matters: a callback payload is user-supplied, so a
    request id belonging to another operator must not be re-runnable.
    """
    try:
        request_id = int(raw_id)
    except ValueError:
        return container.subject_store.get(raw_id)

    async with container.database.session() as session:
        request = await SearchRepository(session).get_request(request_id)
        if request is None or request.telegram_user_id != user_id:
            return None
        payload = request.subject_json
    return subject_from_json(payload)


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="history")

    @router.message(Command("history"))
    async def handle_history_command(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await state.clear()
        await _send_history(message, container, user_id)

    @router.callback_query(F.data == f"{MENU_PREFIX}:history")
    async def handle_history_callback(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await state.clear()
        await answer_callback(callback)
        message = callback_message(callback)
        if message:
            await _send_history(message, container, user_id)

    @router.callback_query(F.data.startswith(f"{REPEAT_PREFIX}:"))
    async def repeat_search(callback: CallbackQuery, container: Container, user_id: int) -> None:
        """Re-running a search never reuses the cache — that is the point of the button."""
        raw = (callback.data or "").split(":", maxsplit=1)[-1]
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return

        subject = await _subject_from_request(container, raw, user_id)
        if subject is None:
            await message.answer(STALE_TOKEN)
            return
        await run_and_send_report(message, container, subject, user_id=user_id, force_refresh=True)

    @router.callback_query(F.data.startswith(f"{REFRESH_PREFIX}:"))
    async def refresh_search(callback: CallbackQuery, container: Container, user_id: int) -> None:
        token = (callback.data or "").split(":", maxsplit=1)[-1]
        subject = container.subject_store.get(token)
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        if subject is None:
            await message.answer(STALE_TOKEN)
            return
        await run_and_send_report(message, container, subject, user_id=user_id, force_refresh=True)

    return router
