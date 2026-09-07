"""``/history`` — the operator's last searches, and re-running one.

Only masked queries are shown, because that is all that was stored.
"""

from __future__ import annotations

from contextlib import suppress

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.common import answer_callback, callback_message, run_and_send_report
from app.bot.keyboards import (
    MENU_HISTORY,
    REFRESH_CONFIRM_PREFIX,
    REFRESH_PREFIX,
    REPEAT_CONFIRM_PREFIX,
    REPEAT_PREFIX,
    history_keyboard,
    spend_confirm_keyboard,
)
from app.container import Container
from app.db.repository import HISTORY_PAGE_SIZE, SearchRepository
from app.domain.enums import SEARCH_TYPE_TITLES, SearchType
from app.domain.identity import SearchSubject
from app.services.reporting import (
    history_mark,
    render_history_line,
    render_provider_summary,
)
from app.services.search import subject_from_json
from app.utils.dates import format_datetime
from app.utils.formatting import split_message

EMPTY_HISTORY = "История пуста. Первую проверку начните с номера телефона — просто напишите его."
HISTORY_HEAD = "История проверок (последние 10)"
STALE_TOKEN = "Данные устарели, запустите поиск заново."
#: «Обновить» читается как чтение, а стоит как проверка. Спрашиваем до списания.
CONFIRM_REFRESH = (
    "Спросить источники заново? Это новая платная проверка: ответ берётся не из "
    "кэша, а из реестров."
)


async def send_history(message: Message, container: Container, user_id: int) -> None:
    """Последние проверки оператора — одним сообщением.

    Экран собран по образцу, который выбрала владелица: заголовок, строка на
    запрос, и две кнопки под списком — «Назад» и «Обновить». Ни повторов
    поимённо, ни списка источников: за повтором есть кнопка под самим отчётом,
    а имена источников на общем экране — это выдача поставщиков.

    Правится на месте, если пришли из меню: «Обновить» не должно добавлять в
    чат десятый список подряд.
    """
    async with container.database.session() as session:
        repo = SearchRepository(session)
        requests = await repo.recent_for_user(user_id, limit=HISTORY_PAGE_SIZE)
        lines: list[str] = []
        for index, request in enumerate(requests, start=1):
            results = await repo.results_for_request(request.id)
            report_row = await repo.report_for_request(request.id)
            states = [(row.provider, row.provider_status) for row in results]
            lines.append(
                f"{history_mark(states)} "
                + render_history_line(
                    index,
                    created_at=format_datetime(request.created_at),
                    search_type=_search_type_title(request.search_type),
                    masked_query=request.masked_query,
                    score=report_row.score if report_row else None,
                    category=report_row.category if report_row else None,
                    provider_summary=render_provider_summary(states),
                )
            )

    if not lines:
        await _show(message, EMPTY_HISTORY, history_keyboard())
        return

    text = HISTORY_HEAD + "\n\n" + "\n".join(lines)
    chunks = split_message(text)
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await _show(message, chunks[-1], history_keyboard())


async def _show(message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    """Поправить сообщение на месте; не вышло — отправить новое."""
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)


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
        await send_history(message, container, user_id)

    @router.callback_query(F.data == MENU_HISTORY)
    async def handle_history_callback(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await state.clear()
        await answer_callback(callback)
        message = callback_message(callback)
        if message:
            await send_history(message, container, user_id)

    @router.callback_query(F.data.startswith(f"{REPEAT_PREFIX}:"))
    async def repeat_search(callback: CallbackQuery, container: Container) -> None:
        """Повтор из истории. Спрашивает, потому что идёт мимо кэша и стоит денег."""
        raw = (callback.data or "").split(":", maxsplit=1)[-1]
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        await message.answer(
            CONFIRM_REFRESH, reply_markup=spend_confirm_keyboard(REPEAT_CONFIRM_PREFIX, raw)
        )

    @router.callback_query(F.data.startswith(f"{REPEAT_CONFIRM_PREFIX}:"))
    async def repeat_confirmed(callback: CallbackQuery, container: Container, user_id: int) -> None:
        raw = (callback.data or "").split(":", maxsplit=1)[-1]
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        await _drop(message)
        subject = await _subject_from_request(container, raw, user_id)
        if subject is None:
            await message.answer(STALE_TOKEN)
            return
        await run_and_send_report(message, container, subject, user_id=user_id, force_refresh=True)

    @router.callback_query(F.data.startswith(f"{REFRESH_PREFIX}:"))
    async def refresh_search(callback: CallbackQuery, container: Container) -> None:
        """«Обновить» под отчётом. Само нажатие ничего не тратит — тратит ответ на вопрос.

        Кнопка живёт под каждым прошлым отчётом бесконечно, и до этой правки
        каждое её нажатие уходило прямиком в платные источники мимо кэша. При
        остатке в два десятка запросов четырёх случайных нажатий хватало, чтобы
        баланс кончился к показу заказчику.
        """
        token = (callback.data or "").split(":", maxsplit=1)[-1]
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        await message.answer(
            CONFIRM_REFRESH, reply_markup=spend_confirm_keyboard(REFRESH_CONFIRM_PREFIX, token)
        )

    @router.callback_query(F.data.startswith(f"{REFRESH_CONFIRM_PREFIX}:"))
    async def refresh_confirmed(
        callback: CallbackQuery, container: Container, user_id: int
    ) -> None:
        token = (callback.data or "").split(":", maxsplit=1)[-1]
        subject = container.subject_store.get(token)
        await answer_callback(callback)
        message = callback_message(callback)
        if message is None:
            return
        await _drop(message)
        if subject is None:
            await message.answer(STALE_TOKEN)
            return
        await run_and_send_report(message, container, subject, user_id=user_id, force_refresh=True)

    return router


async def _drop(message: Message) -> None:
    """Убрать вопрос о подтверждении: на него уже ответили."""
    with suppress(Exception):
        await message.delete()
