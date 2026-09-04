"""Shared helpers for handlers.

Handlers stay thin: they collect input, call a service and render. Everything
that is neither collection nor rendering lives here or in the service layer.
"""

from __future__ import annotations

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.container import Container
from app.domain.identity import SearchSubject
from app.domain.models import DebtorReport
from app.logging_setup import get_logger
from app.services.reporting import render_report
from app.utils.formatting import split_message

logger = get_logger(__name__)

SEARCH_IN_PROGRESS = "🔎 Проверяю источники…"


async def run_and_send_report(
    message: Message,
    container: Container,
    subject: SearchSubject,
    *,
    user_id: int,
    force_refresh: bool = False,
) -> DebtorReport:
    """Run a search, render it and deliver it in Telegram-sized chunks."""
    notice = await message.answer(SEARCH_IN_PROGRESS)
    report = await container.search_service.search(
        subject, telegram_user_id=user_id, force_refresh=force_refresh
    )
    text = render_report(report, demo_mode=container.settings.is_demo)

    await _safe_delete(notice)
    for chunk in split_message(text):
        await message.answer(chunk)
    return report


async def _safe_delete(message: Message) -> None:
    try:
        await message.delete()
    except Exception:
        logger.debug("notice.delete_failed")


async def reset_state(state: FSMContext) -> None:
    await state.clear()


async def answer_callback(callback: CallbackQuery, text: str = "") -> None:
    await callback.answer(text)


def callback_message(callback: CallbackQuery) -> Message | None:
    """The message a callback is attached to, when it is still editable.

    Telegram delivers inaccessible messages for old inline keyboards; those
    cannot be replied to.
    """
    message = callback.message
    return message if isinstance(message, Message) else None
