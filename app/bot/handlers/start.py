"""``/start``, the main menu and ``/cancel``."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.common import answer_callback, callback_message, reset_state
from app.bot.keyboards import CANCEL_CALLBACK, main_menu
from app.container import Container

CANCELLED = "Отменено. Возвращаемся в главное меню."


def menu_text(container: Container) -> str:
    mode_note = (
        "\n\n⚠️ Демо-режим: внешние источники не опрашиваются, данные вымышленные."
        if container.settings.is_demo
        else ""
    )
    return f"{container.settings.app_name}\n\nВнутренний сервис проверки должников.{mode_note}"


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="start")

    @router.message(CommandStart())
    async def handle_start(message: Message, state: FSMContext, container: Container) -> None:
        await reset_state(state)
        await message.answer(menu_text(container), reply_markup=main_menu())

    @router.message(Command("search"))
    async def handle_search(message: Message, state: FSMContext, container: Container) -> None:
        await reset_state(state)
        await message.answer("Выберите тип проверки:", reply_markup=main_menu())

    @router.message(Command("cancel"))
    async def handle_cancel(message: Message, state: FSMContext) -> None:
        await reset_state(state)
        await message.answer(CANCELLED, reply_markup=main_menu())

    @router.callback_query(F.data == CANCEL_CALLBACK)
    async def handle_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await reset_state(state)
        message = callback_message(callback)
        if message:
            await message.answer(CANCELLED, reply_markup=main_menu())
        await answer_callback(callback)

    return router
