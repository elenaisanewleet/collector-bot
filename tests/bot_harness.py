"""Перехват исходящих вызовов Telegram.

Общий стенд для всех тестов, которые гоняют настоящие апдейты через настоящий
диспетчер. Живёт отдельным модулем, потому что нужен больше чем одному файлу
тестов, а фикстуры на его основе объявлены в ``conftest.py``: pytest находит
их только там.

Стенд ровно один, и это условие, а не удобство. Скопировать заглушку во второй
файл дешевле на пять минут и дороже потом: два стенда расходятся молча — один
начинает считать правки сообщений, другой нет, и тест про «одно сообщение
вместо восьмисот» проходит там, где живой бот шлёт восемьсот. Ровно это здесь
уже случилось: ``tests/botkit.py`` прожил одну ветку и был влит сюда.

Наружу торчит то, что нужно сценарию: что бот отправил
(:class:`SentMessages`), чем его подтолкнуть (:func:`make_message`,
:func:`make_callback`, :func:`feed`), какой диспетчер над ним поднять
(:func:`dispatcher_for`) и какие кнопки он показал (:func:`buttons`,
:func:`callbacks`, :func:`urls`).
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    SendDocument,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, PhotoSize, Update, User

if TYPE_CHECKING:
    from app.container import Container

FAKE_TOKEN = "123456789:AAEnoughCharactersToLookLikeARealToken00"
OPERATOR_ID = 111
OUTSIDER_ID = 999
CHAT_ID = 500
# То, чем Telegram отвечает на загруженное фото: дальше бот шлёт этот file_id
# вместо файла, и тест на отсутствие перезаливки опирается на это значение.
BANNER_FILE_ID = "banner-file-id"


class SentMessages:
    """Captures everything the bot tried to send."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []
        # Кому ушло сообщение. Нужно там, где бот пишет не в текущий чат:
        # карточка заявки уходит владельцу, а решение по ней — просителю, и
        # «отправлено» без адресата такие тесты не проверяет.
        self.chats: list[int | str | None] = []
        self.callback_answers: list[str] = []
        self.documents: list[tuple[str, bytes]] = []
        # Что бот пытался отправить картинкой: имя файла или file_id и подпись.
        self.photos: list[tuple[str, str | None]] = []
        # Правки отдельно от отправок. Разница между «одно сообщение, которое
        # меняется» и «лента из восьмисот» видна только здесь: в ``texts`` они
        # выглядят одинаково.
        self.sends: list[str] = []
        self.edits: list[str] = []

    @property
    def joined(self) -> str:
        return "\n".join(self.texts)

    def contains(self, needle: str) -> bool:
        return any(needle in text for text in self.texts)

    def to_chat(self, chat_id: int) -> list[str]:
        """Тексты, ушедшие конкретному адресату.

        ``chats`` пополняется ровно там же, где ``texts``, — иначе списки
        разъезжаются и адресат приписывается чужому сообщению.
        """
        return [text for text, chat in zip(self.texts, self.chats, strict=True) if chat == chat_id]


def intercept(sent: SentMessages) -> Any:
    """Замена ``Bot.__call__``, складывающая исходящее в ``sent``."""
    counter = {"id": 1000}

    async def fake_call(self: Bot, method: TelegramMethod[Any], *args: Any, **kwargs: Any) -> Any:
        if isinstance(method, SendMessage):
            sent.texts.append(method.text)
            sent.markups.append(method.reply_markup)
            sent.chats.append(method.chat_id)
            sent.sends.append(method.text)
            counter["id"] += 1
            return Message.model_construct(
                message_id=counter["id"],
                date=datetime(2026, 9, 4),
                chat=Chat(id=CHAT_ID, type="private"),
                text=method.text,
            ).as_(self)
        if isinstance(method, EditMessageText):
            # Прогресс правится на месте — для теста это такой же текст.
            sent.texts.append(method.text or "")
            sent.markups.append(method.reply_markup)
            sent.chats.append(method.chat_id)
            sent.edits.append(method.text or "")
            return True
        if isinstance(method, SendPhoto):
            photo = method.photo
            sent.photos.append((str(getattr(photo, "path", photo)), method.caption))
            if method.caption is not None:
                # Подпись под фото — новое сообщение, а не правка старого.
                sent.texts.append(method.caption)
                sent.markups.append(method.reply_markup)
                sent.chats.append(method.chat_id)
                sent.sends.append(method.caption)
            counter["id"] += 1
            # Ответ несёт file_id: на нём держится отказ от перезаливки файла.
            return Message.model_construct(
                message_id=counter["id"],
                date=datetime(2026, 9, 4),
                chat=Chat(id=CHAT_ID, type="private"),
                caption=method.caption,
                photo=[PhotoSize(file_id=BANNER_FILE_ID, file_unique_id="u", width=1, height=1)],
            ).as_(self)
        if isinstance(method, SendDocument):
            document = method.document
            sent.documents.append(
                (getattr(document, "filename", ""), getattr(document, "data", b""))
            )
            return True
        if isinstance(method, AnswerCallbackQuery):
            sent.callback_answers.append(method.text or "")
            return True
        if isinstance(method, DeleteMessage):
            return True
        return True

    return fake_call


def make_message(text: str, user_id: int = OPERATOR_ID, message_id: int = 1) -> Message:
    return Message.model_construct(
        message_id=message_id,
        date=datetime(2026, 9, 4),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Operator"),
        text=text,
    )


def make_callback(data: str, user_id: int = OPERATOR_ID) -> CallbackQuery:
    return CallbackQuery.model_construct(
        id=f"cb-{data}",
        from_user=User(id=user_id, is_bot=False, first_name="Operator"),
        chat_instance="chat-instance",
        data=data,
        message=make_message("предыдущее сообщение", user_id=user_id, message_id=2),
    )


async def feed(dispatcher: Dispatcher, bot: Bot, **update: Any) -> None:
    await dispatcher.feed_update(bot, Update.model_construct(update_id=1, **update))


def dispatcher_for(container: Container) -> Dispatcher:
    """Настоящий диспетчер поверх заданного контейнера.

    Сценарии, которым нужен свой контейнер (со ссылками на веб, с включённым
    мостом, с отказывающим источником), собирают его этой функцией, а не
    повторяют проводку. Импорт роутера локальный: стенд не должен тянуть
    ``app`` только за то, что его импортировали.
    """
    from app.bot.router import setup_dispatcher

    return setup_dispatcher(Dispatcher(storage=MemoryStorage()), container)


def buttons(sent: SentMessages) -> list[str]:
    """Тексты всех кнопок, которые бот показал за прогон."""
    return [
        button.text
        for markup in sent.markups
        if markup is not None and getattr(markup, "inline_keyboard", None)
        for row in markup.inline_keyboard
        for button in row
    ]


def callbacks(sent: SentMessages) -> list[str]:
    return [
        button.callback_data
        for markup in sent.markups
        if markup is not None and getattr(markup, "inline_keyboard", None)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def urls(sent: SentMessages) -> list[str]:
    return [
        button.url
        for markup in sent.markups
        if markup is not None and getattr(markup, "inline_keyboard", None)
        for row in markup.inline_keyboard
        for button in row
        if button.url
    ]
