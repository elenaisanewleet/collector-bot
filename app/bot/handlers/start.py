"""``/start``, приветствие, главное меню и ``/cancel``."""

from __future__ import annotations

from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.bot.banner import send_welcome
from app.bot.common import answer_callback, callback_message, reset_state
from app.bot.handlers import query_card
from app.bot.keyboards import (
    BACK_CALLBACK,
    CANCEL_CALLBACK,
    MENU_HOME,
    MENU_MORE,
    BaseListing,
    main_menu,
    main_reply_keyboard,
    more_menu,
)
from app.container import Container
from app.db.repository import DebtorRepository
from app.services.share import ShareKind, ShareTarget
from app.utils.formatting import pluralize_ru
from app.utils.money import format_compact_amount

CANCELLED = "Отменено. Вы в главном меню."
CHOOSE_TYPE = "Вы в главном меню. С чего начнём?"
CHOOSE_OTHER = "Ищем по чему-то другому:"

# Приветствие — ОДНА фраза. Дословное и много раз повторённое требование
# владелицы; всё, что я дописывала сверх, она возвращала обратно.
#
# Что делать, экран не объясняет, и это не потеря: под полем ввода стоят две
# кнопки, а в самом поле — подсказка «Напишите номер телефона должника».
# Инструкция словами поверх этого была третьим объяснением того же.
#
# Оговорка «не проверено ≠ чисто» отсюда ушла, но из продукта не делась: она
# стоит на карточке под каждым прочерком, в отчёте и в справке — то есть там,
# где человек её читает по делу, а не до первого своего действия.
#
# Ни реестров, ни базы по имени: из чего собран ответ, показывает экран «Откуда
# данные» — тому, кто спросил.
WELCOME_STEPS = "Проверяю ваших должников и говорю, стоит ли подавать и платить пошлину."

DEMO_NOTE = "Демо-режим: внешние источники не опрашиваются, данные вымышленные."


def welcome_text(container: Container, *, base: str | None = None) -> str:
    """Приветствие без служебного заголовка.

    Первой строкой стояло название приложения из настроек — «Collector Bot»
    латиницей над русским текстом. Оно ничего не сообщает: имя бота Telegram и
    так печатает в шапке чата, а вот английская строка над первым экраном
    заказчика бросается в глаза сразу.

    ``base`` — готовая строка со ссылкой на всю базу, размеченная под HTML.
    Собирает её обработчик: чтобы её составить, нужны запрос к базе и выпуск
    ссылки, а функция, печатающая текст, ходить в базу не должна.
    """
    lines = [WELCOME_STEPS]
    if base:
        lines.extend(("", base))
    if container.settings.is_demo:
        lines.extend(("", DEMO_NOTE))
    return "\n".join(lines)


async def base_listing(container: Container, user_id: int) -> BaseListing | None:
    """Ссылка на веб-список должников с числом и суммой — или ``None``.

    Зачем она вообще. Заказчик открывает бота не только чтобы проверить
    кого-то одного: половина вопросов — «кто у меня вообще есть». Сегодня за
    этим лезут в 1С. Владелица просила её на первом экране дословно:
    «заказчику ссылку на базу сразу».

    Только владельцу. За ссылкой имена, адреса и суммы двух тысяч человек, а
    ``ALLOWED_TELEGRAM_USER_IDS=*`` пускает в бота кого угодно. Показать её
    всем — это отдать базу первому, кто нажал «Start».

    ``None`` — если веб-отчёты выключены, база пуста или спрашивает не
    владелец. Экран тогда обходится без ссылки, а не показывает мёртвую.

    Достаёт здесь, печатают — :func:`base_html` и :func:`main_menu`: на
    приветствии это строка текста, в меню кнопка, а данные одни и те же.
    """
    if not container.access_service.is_owner(user_id):
        return None
    async with container.database.session() as session:
        repo = DebtorRepository(session)
        total = await repo.count()
        amount = await repo.total_debt()
    if not total:
        return None
    url = await container.share_service.issue(
        ShareTarget(ShareKind.BASE, 0), telegram_user_id=user_id
    )
    if url is None:
        return None
    return BaseListing(url=url, total=total, amount=amount)


def base_html(base: BaseListing) -> str:
    """Та же ссылка строкой текста — для приветствия.

    Кнопкой её там поставить нельзя: у сообщения Telegram бывает либо
    инлайн-клавиатура, либо нижняя, а приветствие несёт нижнюю — те самые две
    кнопки. Отдельным сообщением клавиатуру уже слали: тогда кнопок не увидел
    никто.
    """
    noun = pluralize_ru(base.total, "должник", "должника", "должников")
    money = f", {format_compact_amount(base.amount)}" if base.amount else ""
    return f'<a href="{escape(base.url, quote=True)}">Вся база</a> — {base.total} {noun}{money}'


async def menu_markup(container: Container, user_id: int) -> InlineKeyboardMarkup:
    """Главное меню под этого человека, со ссылкой на базу, если она положена."""
    return main_menu(
        owner=container.access_service.is_owner(user_id),
        base=await base_listing(container, user_id),
    )


async def show_menu(message: Message, container: Container, user_id: int) -> None:
    """Показать главное меню НОВЫМ сообщением, ничего не переписывая.

    Правка на месте была ошибкой, и дорогой. «В меню» стоит в том числе под
    карточкой запроса, а карточка — это собранная оператором работа: телефон,
    ФИО, дата. Правка превращала её в текст меню, и со стороны выглядело, будто
    бот на введённый номер вообще не ответил: нажатие инлайн-кнопки не оставляет
    в чате пузыря, поэтому шага «я нажала В меню» в переписке не видно — виден
    только исчезнувший ответ.

    Лишнее сообщение в чате дешевле стёртой работы. Экономия на нём и стоила
    доверия к боту.
    """
    await message.answer(CHOOSE_TYPE, reply_markup=await menu_markup(container, user_id))


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
        # Одно сообщение: фраза и две кнопки под полем ввода. Дословный ориентир
        # владелицы — «одна фраза при старте, две кнопки внизу».
        #
        # Клавиатура едет НА самом приветствии, а не отдельным сообщением,
        # которое тут же удаляют. Тот трюк экономил сообщение и стоил главного:
        # у сообщения Telegram бывает либо инлайн-клавиатура, либо нижняя, и
        # приветствие несло инлайн-меню, а нижняя приезжала носителем и
        # исчезала вместе с ним. Настройка на стороне Telegram оставалась, но
        # клиент сворачивал клавиатуру в значок «≡» — тех самых двух кнопок
        # никто так и не увидел.
        #
        # Инлайн-меню на приветствии больше нет: оно открывается кнопкой
        # «Главное меню», как в боте-образце. Первый экран говорит ровно две
        # вещи — на какой вопрос бот отвечает и что написать.
        base = await base_listing(container, user_id)
        await send_welcome(
            message,
            welcome_text(container, base=base_html(base) if base else None),
            reply_markup=main_reply_keyboard(owner=owner),
            # Разметка включается ТОЛЬКО когда в тексте есть ссылка. Без неё
            # приветствие остаётся простым текстом, как весь остальной бот.
            parse_mode="HTML" if base else None,
        )

    @router.message(Command("search"))
    async def handle_search(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await reset_state(state)
        await message.answer(CHOOSE_TYPE, reply_markup=await menu_markup(container, user_id))

    @router.message(Command("cancel"))
    async def handle_cancel(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await reset_state(state)
        await cancel_card(container, message.chat.id, user_id)
        await message.answer(CANCELLED, reply_markup=await menu_markup(container, user_id))

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
