"""Нижняя клавиатура: нажатие кнопки делает то же, что команда.

Telegram не отличает нажатие кнопки ``ReplyKeyboardMarkup`` от набранного
текста: в бота приходит обычное сообщение с подписью кнопки. Поэтому здесь нет
ничего, кроме пяти точных совпадений по тексту — и двух решений, которые важнее
самих обработчиков.

**Роутер подключается первым.** Хендлер состояния забирает себе весь текст,
дошедший до его роутера: ``PersonSearch.waiting_fio`` разбирает как ФИО что
угодно. Подключи этот роутер после диалоговых — и нижние кнопки перестали бы
работать ровно там, где они нужнее всего, а «🕘 История» посреди ввода ФИО
отвечала бы «Нужно как минимум фамилия и имя».

**Совпадение точное, вместе с эмодзи.** Это не педантизм, а единственный
доступный способ отличить нажатие от ввода. Нажатие присылает подпись байт в
байт; человек, набирающий руками, эмодзи не поставит. Свободный ввод — номер
договора, адрес, ФИО — доезжает до своего сценария нетронутым, а «Проверить
всю базу», набранное словами на шаге ввода ФИО, остаётся попыткой ввести ФИО и
получает разбор ФИО. Совпадение по подстроке или по нижнему регистру без эмодзи
крало бы чужой ввод, и обнаружилось бы это на живых данных.

Состояние диалога сбрасывают только те кнопки, которые начинают новый сценарий:
прогон, проверка одного, история. «ℹ️ Откуда данные» и «❓ Как это работает»
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
from app.bot.keyboards import (
    BUTTON_BATCH,
    BUTTON_HELP,
    BUTTON_HISTORY,
    BUTTON_SEARCH,
    BUTTON_SOURCES,
)
from app.container import Container


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="buttons")

    @router.message(F.text == BUTTON_BATCH)
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

    @router.message(F.text == BUTTON_SEARCH)
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

    @router.message(F.text == BUTTON_HISTORY)
    async def press_history(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        await reset_state(state)
        await send_history(message, container, user_id)

    @router.message(F.text == BUTTON_SOURCES)
    async def press_sources(message: Message, container: Container) -> None:
        await send_sources(message, container)

    @router.message(F.text == BUTTON_HELP)
    async def press_help(message: Message, container: Container, user_id: int) -> None:
        await send_help(message, container, owner=container.access_service.is_owner(user_id))

    return router
