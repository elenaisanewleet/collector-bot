"""``/start``, приветствие, главное меню и ``/cancel``."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.banner import send_welcome
from app.bot.common import answer_callback, callback_message, reset_state
from app.bot.keyboards import (
    BACK_CALLBACK,
    CANCEL_CALLBACK,
    MENU_MORE,
    MENU_PREFIX,
    main_menu,
    main_reply_keyboard,
    more_menu,
)
from app.container import Container

CANCELLED = "Отменено. Возвращаемся в главное меню."
CHOOSE_TYPE = "Что делаем?"
CHOOSE_OTHER = "Ищем по чему-то другому:"

# Приветствие короткое, и это принципиально. Раньше здесь были три
# пронумерованных шага, четыре эмодзи и предупреждение — экран, который читают
# по диагонали и закрывают. Первый экран обязан сказать, что бот делает и что
# нажать, всё остальное человек узнает по ходу.
#
# Оговорка про «не проверено» осталась: разницу между «не смотрели» и «чисто»
# надо узнать до первого отчёта, а не из справки, которую откроет один из
# десяти.
WELCOME_STEPS = """Проверяю должника по официальным реестрам и говорю, \
стоит ли тратить пошлину.

Нажмите «Проверить человека» и пришлите номер телефона — остальное подтяну \
из вашей выгрузки.

Если источник не ответил, пишу «не проверено». Это не то же самое, что «чисто»."""

DEMO_NOTE = "Демо-режим: внешние источники не опрашиваются, данные вымышленные."

# Отдельным сообщением, потому что иначе никак: у сообщения Telegram может быть
# либо инлайн-клавиатура, либо нижняя, но не обе сразу. Приветствие несёт
# инлайн-меню, значит нижнюю клавиатуру доставляет следующая строка — и заодно
# объясняет, что это и зачем. Один раз на /start, а дальше клавиатура живёт в
# чате сама: Telegram хранит её на своей стороне, и следующего сообщения от бота
# для этого не нужно.
KEYBOARD_HINT = "Внизу экрана — постоянные кнопки, их не надо помнить."


def welcome_text(container: Container) -> str:
    lines = [container.settings.app_name, "", WELCOME_STEPS]
    if container.settings.is_demo:
        lines.extend(("", DEMO_NOTE))
    return "\n".join(lines)


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
        await send_welcome(message, welcome_text(container), reply_markup=main_menu())
        # Единственное место, откуда уходит нижняя клавиатура. Больше и не надо:
        # она не «показывается на сообщение», а устанавливается для чата и живёт
        # там, пока её не заменят или не снимут явно, — а снимать её мы нигде не
        # умеем. Обратная сторона: у того, кто /start уже нажимал когда-то,
        # кнопки появятся только после следующего /start. Это цена честного
        # одного вызова вместо клавиатуры, дописанной к каждому ответу бота.
        await message.answer(KEYBOARD_HINT, reply_markup=main_reply_keyboard())

    @router.message(Command("search"))
    async def handle_search(message: Message, state: FSMContext, container: Container) -> None:
        await reset_state(state)
        await message.answer(CHOOSE_TYPE, reply_markup=main_menu())

    @router.message(Command("cancel"))
    async def handle_cancel(message: Message, state: FSMContext) -> None:
        await reset_state(state)
        await message.answer(CANCELLED, reply_markup=main_menu())

    @router.callback_query(F.data == f"{MENU_PREFIX}:back")
    async def handle_back_to_menu(
        callback: CallbackQuery, state: FSMContext, container: Container
    ) -> None:
        """«🔍 Новая проверка» под каждой карточкой отчёта.

        Обработчика у неё не было вовсе — кнопка молча ничего не делала. Теперь,
        когда карточка стала главным местом, откуда оператор идёт к следующему
        должнику, молчание тут дороже четырёх строк кода.
        """
        await reset_state(state)
        message = callback_message(callback)
        if message:
            await message.answer(CHOOSE_TYPE, reply_markup=main_menu())
        await answer_callback(callback)

    @router.callback_query(F.data == MENU_MORE)
    async def handle_more(callback: CallbackQuery) -> None:
        """Способы поиска, кроме человека и всей базы.

        Отдельным экраном, потому что в главном меню их было девять штук разом —
        ровно то, что назвали кучей кнопок. Здесь они никому не мешают: сюда
        заходят, когда телефона нет и надо искать иначе.
        """
        message = callback_message(callback)
        if message:
            await message.answer(CHOOSE_OTHER, reply_markup=more_menu())
        await answer_callback(callback)

    @router.callback_query(F.data == CANCEL_CALLBACK)
    async def handle_cancel_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await reset_state(state)
        message = callback_message(callback)
        if message:
            await message.answer(CANCELLED, reply_markup=main_menu())
        await answer_callback(callback)

    @router.callback_query(F.data == BACK_CALLBACK)
    async def handle_back(callback: CallbackQuery, state: FSMContext) -> None:
        """Кнопка «Новая проверка» под каждым отчётом.

        Обработчика у неё не было вовсе: нажатие висело часиками до таймаута
        Telegram. Состояние сбрасывается — эту кнопку жмут, чтобы начать
        сначала, а не чтобы вернуться в недоигранный диалог.
        """
        await reset_state(state)
        message = callback_message(callback)
        if message:
            await message.answer(CHOOSE_TYPE, reply_markup=main_menu())
        await answer_callback(callback)

    return router
