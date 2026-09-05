"""Shared helpers for handlers.

Handlers stay thin: they collect input, call a service and render. Everything
that is neither collection nor rendering lives here or in the service layer.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot import view
from app.bot.keyboards import report_keyboard
from app.container import Container
from app.domain.identity import SearchSubject
from app.domain.models import DebtorReport
from app.logging_setup import get_logger
from app.services.reporting import render_report
from app.services.share import ShareKind, ShareTarget
from app.utils.formatting import split_message

logger = get_logger(__name__)

# Как часто двигать полосу, пока идёт проверка. Реже, чем лимит Telegram на
# правку сообщения, и достаточно часто, чтобы это читалось как движение.
STAGE_INTERVAL_SECONDS = 1.6


async def run_and_send_report(
    message: Message,
    container: Container,
    subject: SearchSubject,
    *,
    user_id: int,
    force_refresh: bool = False,
) -> DebtorReport:
    """Проверить должника и показать результат.

    В чат уходит карточка с вердиктом и кнопкой на веб-отчёт, а не текст на
    три сообщения: таблицу производств в сообщении Telegram всё равно не
    сверстать. Пока идёт проверка, одно и то же сообщение правится на месте —
    так видно, что работа идёт, и чат не засоряется.

    Если публичный адрес не задан, ссылки нет, и бот честно отдаёт полный
    текстовый отчёт: лучше простыня, чем нерабочая кнопка.
    """
    notice = await message.answer(view.searching(0, subject_name=subject.display_name))
    ticker = asyncio.create_task(_tick_stages(notice, subject.display_name))
    try:
        outcome = await container.search_service.search_detailed(
            subject, telegram_user_id=user_id, force_refresh=force_refresh
        )
    finally:
        ticker.cancel()

    report = outcome.report
    decision = container.verdict_engine.decide(report)

    url: str | None = None
    if outcome.request_id is not None:
        url = await container.share_service.issue(
            ShareTarget(ShareKind.REPORT, outcome.request_id), telegram_user_id=user_id
        )

    if url is None:
        await _safe_delete(notice)
        for chunk in split_message(render_report(report, demo_mode=container.settings.is_demo)):
            await message.answer(chunk)
        return report

    token = container.subject_store.put(subject)
    await _edit_or_send(
        notice,
        message,
        view.report_card(report, decision),
        reply_markup=report_keyboard(url=url, refresh_token=token),
    )
    return report


async def _tick_stages(notice: Message, subject_name: str) -> None:
    """Двигать полосу прогресса, пока идёт проверка.

    Отдельная задача, потому что сам поиск ничего о показе не знает и знать не
    должен. Отменяется, как только результат готов.
    """
    try:
        for index in range(1, len(view.STAGES)):
            await asyncio.sleep(STAGE_INTERVAL_SECONDS)
            with suppress(Exception):  # правка сообщения — дело необязательное
                await notice.edit_text(view.searching(index, subject_name=subject_name))
    except asyncio.CancelledError:  # pragma: no cover - обычный путь отмены
        pass


async def _edit_or_send(
    notice: Message, message: Message, text: str, *, reply_markup: object = None
) -> None:
    """Заменить сообщение о ходе работы результатом.

    Правка на месте вместо нового сообщения: пользователь смотрит туда же, куда
    смотрел, и в чате не остаётся мусора. Если правка не прошла — сообщение
    удалили, прошло слишком много времени — отправляем обычным сообщением.
    """
    try:
        await notice.edit_text(text, reply_markup=reply_markup)  # type: ignore[arg-type]
    except Exception:
        logger.debug("report.edit_failed")
        await _safe_delete(notice)
        await message.answer(text, reply_markup=reply_markup)  # type: ignore[arg-type]


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
