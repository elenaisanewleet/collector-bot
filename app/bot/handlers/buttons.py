"""Нижняя клавиатура: нажатие кнопки делает то же, что команда.

Telegram не отличает нажатие кнопки ``ReplyKeyboardMarkup`` от набранного
текста: в бота приходит обычное сообщение с подписью кнопки. Поэтому здесь нет
ничего, кроме пяти точных совпадений по тексту — и двух решений, которые важнее
самих обработчиков.

**Роутер подключается первым.** Карточка запроса забирает себе весь текст,
дошедший до последнего роутера, и разбирает его как данные о должнике. Подключи
этот роутер после неё — и нижние кнопки перестали бы работать ровно там, где они
нужнее всего: «История проверок» посреди сбора карточки легла бы в поле
«фамилия».

**Совпадение точное и по всей строке.** Это не педантизм, а единственный
доступный способ отличить нажатие от ввода. Нажатие присылает подпись байт в
байт; свободный ввод — номер договора, адрес, ФИО — доезжает до своего сценария
нетронутым, а «Проверить всю базу», набранное словами на шаге ввода ФИО,
остаётся попыткой ввести ФИО и получает разбор ФИО. Совпадение по подстроке или
по нижнему регистру крало бы чужой ввод, и обнаружилось бы это на живых данных.

**Каждая подпись, которая когда-либо стояла на клавиатуре, обязана иметь
обработчик — навсегда.** Нижняя клавиатура живёт на стороне Telegram и
обновляется только со следующим /start, поэтому у всех, кто его не нажимал,
кнопки остаются старыми. Подпись без обработчика уходит в разбор свободного
текста, и «Главное меню» становится фамилией нового должника: оператор жмёт
«домой» и получает вопрос «это другой человек или исправление?». Отсюда
``LEGACY_BUTTON_*`` рядом с каждой нынешней подписью — снятые кнопки продолжают
работать.

Состояние диалога сбрасывают только те кнопки, которые начинают новый сценарий:
меню, прогон, проверка одного, история. «Откуда данные» и «Как это работает»
ничего не начинают — это чтение, и оборвать ради него наполовину введённую
проверку было бы наказанием за любопытство. После них человек возвращается ровно
на тот шаг, где стоял.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from app.bot.access_view import BATCH_ACTION, refuse_owner_only
from app.bot.common import reset_state
from app.bot.handlers.batch import offer_batch
from app.bot.handlers.help import send_help
from app.bot.handlers.history import send_history
from app.bot.handlers.query_card import start_person_card
from app.bot.handlers.sources import send_sources
from app.bot.handlers.start import CHOOSE_OTHER, CHOOSE_TYPE, cancel_card, menu_markup
from app.bot.keyboards import (
    BUTTON_BATCH,
    BUTTON_HELP,
    BUTTON_HISTORY,
    BUTTON_MENU,
    BUTTON_MORE,
    BUTTON_SEARCH,
    BUTTON_SOURCES,
    LEGACY_BUTTON_BATCH,
    LEGACY_BUTTON_HELP,
    LEGACY_BUTTON_HISTORY,
    LEGACY_BUTTON_SEARCH,
    LEGACY_BUTTON_SOURCES,
    more_menu,
)
from app.container import Container


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="buttons")

    @router.message(F.text.in_({BUTTON_MENU}))
    async def press_menu(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        """«Главное меню» — левая из двух кнопок под полем ввода.

        Обработчик обязателен, и его отсутствие стоит дороже, чем кажется:
        нажатие нижней кнопки приходит обычным текстом, и без точного
        совпадения оно доезжает до карточки запроса, где «Главное меню»
        разбирается как ФИО нового должника. Оператор жмёт «домой» и получает
        вопрос «это другой человек или исправление?».

        Состояние сбрасывается: в меню уходят, чтобы начать другое, а не чтобы
        вернуться в недоигранный вопрос.
        """
        await reset_state(state)
        await cancel_card(container, message.chat.id, user_id)
        await message.answer(
            CHOOSE_TYPE, reply_markup=await menu_markup(container, user_id)
        )

    @router.message(F.text.in_({BUTTON_BATCH, LEGACY_BUTTON_BATCH}))
    async def press_batch(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        if not container.access_service.is_owner(user_id):
            # Кнопки у допущенного нет, но подпись — обычный текст, а нижняя
            # клавиатура живёт на стороне Telegram и переживает и смену прав, и
            # пересылку: нажатие обязано упереться в ту же проверку, что /batch.
            await refuse_owner_only(message, action=BATCH_ACTION, user_id=user_id)
            return
        # Состояние не чистим: offer_batch либо ставит своё, либо чистит сам.
        await offer_batch(message, state, container)

    @router.message(F.text.in_({BUTTON_SEARCH, LEGACY_BUTTON_SEARCH}))
    async def press_search(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        """Сразу к первому вопросу, без промежуточного меню.

        Кнопка вела в инлайн-меню из семи типов проверки: оператор выбирал ещё
        раз то, что уже выбрал нажатием. Сценарий из ТЗ — ввёл телефон, получил
        сводку, — и лишний экран стоял поперёк него. Остальные шесть типов
        доступны из меню за «Другие способы поиска».
        """
        await reset_state(state)
        await start_person_card(message, container, user_id)

    @router.message(F.text == BUTTON_MORE)
    async def press_more(message: Message, container: Container, user_id: int) -> None:
        """Редкие способы поиска. Состояние не трогаем: это чтение меню."""
        owner = container.access_service.is_owner(user_id)
        await message.answer(CHOOSE_OTHER, reply_markup=more_menu(owner=owner))

    @router.message(F.text.in_({BUTTON_HISTORY, LEGACY_BUTTON_HISTORY}))
    async def press_history(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await reset_state(state)
        await send_history(message, container, user_id)

    @router.message(F.text.in_({BUTTON_SOURCES, LEGACY_BUTTON_SOURCES}))
    async def press_sources(message: Message, container: Container, user_id: int) -> None:
        await send_sources(message, container, user_id)

    @router.message(F.text.in_({BUTTON_HELP, LEGACY_BUTTON_HELP}))
    async def press_help(message: Message, container: Container, user_id: int) -> None:
        await send_help(message, container, user_id)

    return router
