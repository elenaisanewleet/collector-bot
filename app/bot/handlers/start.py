"""``/start``, приветствие, главное меню и ``/cancel``."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.banner import install_reply_keyboard, send_welcome
from app.bot.common import answer_callback, callback_message, reset_state
from app.bot.handlers import query_card
from app.bot.keyboards import (
    BACK_CALLBACK,
    CANCEL_CALLBACK,
    MENU_HOME,
    MENU_MORE,
    main_menu,
    main_reply_keyboard,
    more_menu,
)
from app.container import Container

CANCELLED = "Отменено. Вы в главном меню."
CHOOSE_TYPE = "Вы в главном меню. С чего начнём?"
CHOOSE_OTHER = "Ищем по чему-то другому:"

# Приветствие — одна фраза, и это дословный ориентир владелицы: «одна фраза при
# старте, две кнопки внизу». Раньше здесь были три пронумерованных шага, четыре
# эмодзи и предупреждение — экран, который читают по диагонали и закрывают.
#
# **Ни реестры, ни 1С здесь не названы, и это не забывчивость.** Первая версия
# начиналась словами «проверяю должника по официальным реестрам» — неправда
# дважды. Основа проверки — база самого заказчика: по ней бот поднимает
# должника, его договор, машину и сумму долга, и ради неё всё написано; реестры
# добирают недостающее и стоят вторыми. Но и базу называть своим именем на
# первом экране незачем: правило владелицы — «не надо про это рассказывать
# всем». Из чего собран ответ, узнаёт тот, кто спросил, экраном «Откуда данные».
#
# Экран говорит ровно две вещи: на какой вопрос бот отвечает и что нажать.
#
# Оговорка про «не проверено» осталась и осталась именно здесь: разницу между
# «не смотрели» и «чисто» надо узнать до первого отчёта, а не из справки,
# которую откроет один из десяти.
WELCOME_STEPS = """Проверяю ваших должников и отвечаю на один вопрос: \
стоит ли подавать и платить пошлину.

Напишите номер телефона — подниму карточку должника, остальное соберу сам. \
Чего не проверил, так и пишу «не проверено»: это не то же самое, что «чисто»."""

DEMO_NOTE = "Демо-режим: внешние источники не опрашиваются, данные вымышленные."


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


async def show_menu(message: Message, container: Container, user_id: int) -> None:
    """Показать главное меню — правкой того же сообщения, если это возможно.

    Возврат «в меню» из подменю не должен добавлять в чат ещё одно сообщение:
    оператор нажал «назад», а не «покажи ещё раз».
    """
    owner = container.access_service.is_owner(user_id)
    await edit_or_answer(message, CHOOSE_TYPE, main_menu(owner=owner))


async def edit_or_answer(message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    """Поправить сообщение на месте; не вышло — отправить новое.

    Не выходит в двух случаях: сообщение с картинкой (у ``/start`` баннер) и
    сообщение оператора, а не бота. Оба законны, и оба обязаны кончаться меню
    на экране, а не тишиной.
    """
    try:
        await message.edit_text(text, reply_markup=markup)
    except Exception:
        await message.answer(text, reply_markup=markup)


async def cancel_card(container: Container, chat_id: int, user_id: int) -> None:
    """Снять с карточки заданный вопрос: «Отменено» обязано отменять.

    Раньше отменялось только состояние FSM, а карточка оставалась стоять на
    шаге «фамилия» — и следующее сообщение, каким бы оно ни было, ложилось в
    это поле. Оператор говорил «отмена», получал «отменено» и продолжал
    заполнять ту же карточку, сам того не зная.

    Сама карточка не чистится: собранное — это работа оператора, а «отмена»
    относится к вопросу, а не к ней.
    """
    if not chat_id:
        return
    card = await container.query_cards.load(user_id, chat_id)
    container.query_cards.leave_steps(container.query_cards.cancel_ask(card))
    await container.query_cards.save(card)


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
        # Одно сообщение на /start, как в боте-образце: приветствие с меню под
        # ним. Нижняя клавиатура приезжает отдельным носителем, который тут же
        # удаляется, — она принадлежит чату и сообщение ей не нужно. Раньше за
        # неё платили второй строкой, объясняющей интерфейс вместо того, чтобы
        # работать.
        await install_reply_keyboard(message, main_reply_keyboard(owner=owner))
        await send_welcome(message, welcome_text(container), reply_markup=main_menu(owner=owner))

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
        await cancel_card(container, message.chat.id, user_id)
        await message.answer(
            CANCELLED, reply_markup=main_menu(owner=container.access_service.is_owner(user_id))
        )

    @router.callback_query(F.data == BACK_CALLBACK)
    async def handle_new_check(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        """«Новая проверка» под карточкой отчёта.

        Ведёт туда же, куда «Новая проверка» на самой карточке: к чистой
        карточке следующего должника. Раньше эти две одноимённые кнопки
        расходились в поведении — та чистила, эта показывала меню, — и через
        меню оператор попадал в карточку ПРЕДЫДУЩЕГО человека. Одна подпись
        обязана значить одно.

        Обработчик один на оба значения ``menu:back``: их было два, зарегистри-
        рованных на одну и ту же строку, и работал всегда первый.
        """
        await reset_state(state)
        message = callback_message(callback)
        await answer_callback(callback)
        if message is not None:
            await query_card.start_person_card(message, container, user_id)

    @router.callback_query(F.data == MENU_HOME)
    async def handle_home(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        """«В меню» — общий выход с любого экрана. Тупиков в боте нет."""
        await reset_state(state)
        message = callback_message(callback)
        await answer_callback(callback)
        if message is not None:
            await show_menu(message, container, user_id)

    @router.callback_query(F.data == MENU_MORE)
    async def handle_more(callback: CallbackQuery, container: Container, user_id: int) -> None:
        """Способы поиска, кроме человека и всей базы.

        Правится на месте: меню и подменю — это один экран, который меняет
        содержимое, а не два сообщения подряд.
        """
        message = callback_message(callback)
        await answer_callback(callback)
        if message is not None:
            owner = container.access_service.is_owner(user_id)
            await edit_or_answer(message, CHOOSE_OTHER, more_menu(owner=owner))

    @router.callback_query(F.data == CANCEL_CALLBACK)
    async def handle_cancel_callback(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await reset_state(state)
        message = callback_message(callback)
        await answer_callback(callback)
        await cancel_card(container, message.chat.id if message else 0, user_id)
        if message is not None:
            await show_menu(message, container, user_id)

    return router
