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


def welcome_text(container: Container) -> str:
    """Приветствие без служебного заголовка.

    Первой строкой стояло название приложения из настроек — «Collector Bot»
    латиницей над русским текстом. Оно ничего не сообщает: имя бота Telegram и
    так печатает в шапке чата, а вот английская строка над первым экраном
    заказчика бросается в глаза сразу.
    """
    lines = [WELCOME_STEPS]
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
    async def handle_start(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await reset_state(state)
        # Меню и нижняя клавиатура собираются под того, кто их получит: кнопки
        # прогона и импорта есть только у владельца, и показывать их остальным
        # значит обещать то, чего бот не даст.
        owner = container.access_service.is_owner(user_id)
        # Одно сообщение вместо двух. У сообщения Telegram бывает либо инлайн-
        # клавиатура, либо нижняя, и раньше ради нижней уходила вторая строка —
        # «внизу экрана постоянные кнопки, их не надо помнить». Она объясняла
        # интерфейс вместо того, чтобы работать: те же две кнопки уже стоят под
        # приветствием, а нижние человек увидит сам, они прямо под пальцем.
        #
        # Нижняя клавиатура ставится для чата и живёт там, пока её не заменят.
        # Обратная сторона: у того, кто /start нажимал давно, кнопки обновятся
        # только со следующим /start.
        await send_welcome(
            message, welcome_text(container), reply_markup=main_reply_keyboard(owner=owner)
        )

    @router.message(Command("search"))
    async def handle_search(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await reset_state(state)
        await message.answer(
            CHOOSE_TYPE, reply_markup=main_menu(owner=container.access_service.is_owner(user_id))
        )

    @router.message(Command("cancel"))
    async def handle_cancel(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await reset_state(state)
        await message.answer(
            CANCELLED, reply_markup=main_menu(owner=container.access_service.is_owner(user_id))
        )

    @router.callback_query(F.data == f"{MENU_PREFIX}:back")
    async def handle_back_to_menu(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        """«🔍 Новая проверка» под каждой карточкой отчёта.

        Обработчика у неё не было вовсе — кнопка молча ничего не делала. Теперь,
        когда карточка стала главным местом, откуда оператор идёт к следующему
        должнику, молчание тут дороже четырёх строк кода.
        """
        await reset_state(state)
        message = callback_message(callback)
        if message:
            await message.answer(
                CHOOSE_TYPE,
                reply_markup=main_menu(owner=container.access_service.is_owner(user_id)),
            )
        await answer_callback(callback)

    @router.callback_query(F.data == MENU_MORE)
    async def handle_more(callback: CallbackQuery, container: Container, user_id: int) -> None:
        """Способы поиска, кроме человека и всей базы.

        Отдельным экраном, потому что в главном меню их было девять штук разом —
        ровно то, что назвали кучей кнопок. Здесь они никому не мешают: сюда
        заходят, когда телефона нет и надо искать иначе.
        """
        message = callback_message(callback)
        if message:
            owner = container.access_service.is_owner(user_id)
            await message.answer(CHOOSE_OTHER, reply_markup=more_menu(owner=owner))
        await answer_callback(callback)

    @router.callback_query(F.data == CANCEL_CALLBACK)
    async def handle_cancel_callback(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await reset_state(state)
        message = callback_message(callback)
        if message:
            await message.answer(
                CANCELLED, reply_markup=main_menu(owner=container.access_service.is_owner(user_id))
            )
        await answer_callback(callback)

    @router.callback_query(F.data == BACK_CALLBACK)
    async def handle_back(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        """Кнопка «Новая проверка» под каждым отчётом.

        Обработчика у неё не было вовсе: нажатие висело часиками до таймаута
        Telegram. Состояние сбрасывается — эту кнопку жмут, чтобы начать
        сначала, а не чтобы вернуться в недоигранный диалог.
        """
        await reset_state(state)
        message = callback_message(callback)
        if message:
            await message.answer(
                CHOOSE_TYPE,
                reply_markup=main_menu(owner=container.access_service.is_owner(user_id)),
            )
        await answer_callback(callback)

    return router
