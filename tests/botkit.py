"""Стенд для сценариев бота: настоящий диспетчер, перехваченный Telegram.

Вынесен из :mod:`tests.test_bot_flow` в тот момент, когда сценариев бота стало
два файла. Скопировать заглушку во второй файл было бы дешевле на пять минут и
дороже потом: два стенда расходятся молча — один начинает считать правки
сообщений, другой нет, и тесты про «одно сообщение вместо восьмисот» проходят
там, где живой бот шлёт восемьсот.

Наружу торчит ровно то, что нужно сценарию: что бот отправил
(:class:`SentMessages`), чем его подтолкнуть (:func:`make_message`,
:func:`make_callback`, :func:`feed`) и какие кнопки он показал
(:func:`buttons`, :func:`callbacks`). Фикстуры, которые всё это собирают, живут
в ``conftest.py``: pytest находит их только там.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.container import Container

FAKE_TOKEN = "123456789:AAEnoughCharactersToLookLikeARealToken00"
OPERATOR_ID = 111
OUTSIDER_ID = 999
CHAT_ID = 500


class SentMessages:
    """Captures everything the bot tried to send."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []
        self.callback_answers: list[str] = []
        self.documents: list[tuple[str, bytes]] = []
        # Правки отдельно от отправок. Разница между «одно сообщение, которое
        # меняется» и «лента из восьмисот» видна только здесь: в ``texts`` они
        # выглядят одинаково.
        self.edits: list[str] = []
        self.sends: list[str] = []

    @property
    def joined(self) -> str:
        return "\n".join(self.texts)

    def contains(self, needle: str) -> bool:
        return any(needle in text for text in self.texts)


def install_bot(sent: SentMessages, monkeypatch: pytest.MonkeyPatch) -> Bot:
    """Bot, чьи исходящие вызовы перехвачены, а не отправлены."""
    instance = Bot(token=FAKE_TOKEN, default=DefaultBotProperties(parse_mode=None))
    counter = {"id": 1000}

    async def fake_call(self: Bot, method: TelegramMethod[Any], *args: Any, **kwargs: Any) -> Any:
        if isinstance(method, SendMessage):
            sent.texts.append(method.text)
            sent.sends.append(method.text)
            sent.markups.append(method.reply_markup)
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
            sent.edits.append(method.text or "")
            sent.markups.append(method.reply_markup)
            return True
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

    monkeypatch.setattr(Bot, "__call__", fake_call, raising=True)
    return instance


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
    повторяют проводку.
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
