"""``/sources`` — экран «Откуда данные».

Роутер подключается одним из первых, чтобы команда работала и посреди диалога:
человек, которого бот только что попросил ввести ФИО, имеет право спросить, куда
эти данные пойдут, и получить ответ, а не «Недопустимые символы».
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.common import answer_callback, callback_message
from app.bot.handlers.start import menu_markup
from app.bot.keyboards import MENU_PREFIX
from app.bot.sources import sources_screen
from app.container import Container
from app.db.repository import DebtorRepository
from app.utils.formatting import split_message

SOURCES_CALLBACK = f"{MENU_PREFIX}:sources"


async def send_sources(message: Message, container: Container, user_id: int) -> None:
    """Экран «Откуда данные» и под ним меню.

    Меню здесь не было вовсе: экран кончался последней строкой про источники, и
    выйти с него можно было только нижней клавиатурой или /start. Правило
    «тупиков нет ни на одном экране» в проекте записано, а этот экран его
    нарушал — просто заметили не сразу, потому что нижняя клавиатура прикрывала
    дыру у всех, кто нажал /start после последней выкладки.
    """
    async with container.database.session() as session:
        debtors = await DebtorRepository(session).count()
    chunks = split_message(sources_screen(container, debtors=debtors))
    markup = await menu_markup(container, user_id)
    for index, chunk in enumerate(chunks):
        # Меню — только под последним куском: клавиатура посреди текста
        # читается как его конец.
        last = index == len(chunks) - 1
        await message.answer(chunk, reply_markup=markup if last else None)


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="sources")

    @router.message(Command("sources"))
    async def handle_sources(message: Message, container: Container, user_id: int) -> None:
        await send_sources(message, container, user_id)

    @router.callback_query(F.data == SOURCES_CALLBACK)
    async def handle_sources_callback(
        callback: CallbackQuery, container: Container, user_id: int
    ) -> None:
        await answer_callback(callback)
        message = callback_message(callback)
        if message:
            await send_sources(message, container, user_id)

    return router
