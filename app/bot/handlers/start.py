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
    MENU_PREFIX,
    main_menu,
    main_reply_keyboard,
)
from app.container import Container

CANCELLED = "Отменено. Возвращаемся в главное меню."
CHOOSE_TYPE = "Выберите тип проверки:"

# Приветствие устроено как три шага — нажми, введи, получи, — потому что первый
# экран обязан говорить, что делать, а не описывать себя. Оговорка про «не
# проверено» стоит здесь, а не в справке, которую откроет один человек из
# десяти: узнать разницу между «не смотрели» и «чисто» надо до первого отчёта.
WELCOME_STEPS = """Проверяю должника по официальным реестрам и говорю, есть ли смысл \
тратить пошлину.

1️⃣ Нажмите кнопку под этим сообщением — «Проверить всю базу» или «Физлицо»
2️⃣ Пишите что знаете — ФИО, дату рождения, ИНН, телефон. Хоть строкой, хоть \
по одному слову: бот собирает всё в одну карточку
3️⃣ Нажмите «Проверить» и получите вердикт — иск, судебный приказ, \
проверить руками или не подавать — с суммой пошлины и ссылкой на полный отчёт

⚠️ Если источник не ответил, бот пишет «не проверено». Это не значит, что там чисто.

ℹ️ Откуда данные — /sources
❓ Как это работает — /help"""

DEMO_NOTE = "⚠️ Демо-режим: внешние источники не опрашиваются, данные вымышленные."

# Отдельным сообщением, потому что иначе никак: у сообщения Telegram может быть
# либо инлайн-клавиатура, либо нижняя, но не обе сразу. Приветствие несёт
# инлайн-меню, значит нижнюю клавиатуру доставляет следующая строка — и заодно
# объясняет, что это и зачем. Один раз на /start, а дальше клавиатура живёт в
# чате сама: Telegram хранит её на своей стороне, и следующего сообщения от бота
# для этого не нужно.
KEYBOARD_HINT = (
    "⌨️ Внизу экрана — постоянные кнопки. Это то же самое, что команды со слешем, "
    "только их не надо помнить."
)


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
