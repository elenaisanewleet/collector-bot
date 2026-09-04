"""Access control.

The allowlist is the security boundary of this tool. These tests assert that an
unauthorized user cannot reach a handler at all — not that a handler declines to
act once reached.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from aiogram.types import Chat, Message, User

from app.bot.middleware import ACCESS_DENIED_MESSAGE, AllowlistMiddleware
from app.config import Settings


@pytest.fixture
def replies(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Capture what the middleware sends back, without a live Bot session."""
    captured: list[str] = []

    async def fake_answer(self: Message, text: str, **_kwargs: Any) -> None:
        captured.append(text)

    monkeypatch.setattr(Message, "answer", fake_answer, raising=True)
    return captured


def make_message() -> Message:
    """A real aiogram Message — the middleware dispatches on its type."""
    return Message.model_construct(
        message_id=1,
        date=datetime(2026, 9, 4),
        chat=Chat(id=1, type="private"),
        text="Иванов Иван Иванович",
    )


def make_user(user_id: int) -> User:
    return User(id=user_id, is_bot=False, first_name="Operator")


class HandlerSpy:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, event: Any, data: dict[str, Any]) -> str:
        self.calls += 1
        return "handled"


@pytest.fixture
def handler() -> HandlerSpy:
    return HandlerSpy()


async def test_allowed_user_reaches_the_handler(handler: HandlerSpy, replies: list[str]) -> None:
    middleware = AllowlistMiddleware(frozenset({111}))

    result = await middleware(handler, make_message(), {"event_from_user": make_user(111)})

    assert handler.calls == 1
    assert result == "handled"
    assert replies == []


async def test_unauthorized_user_is_refused_before_the_handler(
    handler: HandlerSpy, replies: list[str]
) -> None:
    """No handler runs, so no search request and no external API call happens."""
    middleware = AllowlistMiddleware(frozenset({111}))

    result = await middleware(handler, make_message(), {"event_from_user": make_user(999)})

    assert handler.calls == 0
    assert result is None
    assert replies == [ACCESS_DENIED_MESSAGE]


async def test_missing_user_is_refused(handler: HandlerSpy, replies: list[str]) -> None:
    middleware = AllowlistMiddleware(frozenset({111}))

    await middleware(handler, make_message(), {})

    assert handler.calls == 0
    assert replies == [ACCESS_DENIED_MESSAGE]


async def test_empty_allowlist_denies_everyone(handler: HandlerSpy, replies: list[str]) -> None:
    """A closed bot fails shut: a misconfigured allowlist locks the door, it
    does not open it."""
    middleware = AllowlistMiddleware(frozenset())

    await middleware(handler, make_message(), {"event_from_user": make_user(111)})

    assert handler.calls == 0
    assert replies == [ACCESS_DENIED_MESSAGE]


async def test_allowed_user_id_is_injected(handler: HandlerSpy, replies: list[str]) -> None:
    middleware = AllowlistMiddleware(frozenset({111}))
    data: dict[str, Any] = {"event_from_user": make_user(111)}

    await middleware(handler, make_message(), data)

    assert data["user_id"] == 111


async def test_unauthorized_search_never_reaches_a_provider(
    settings: Settings, replies: list[str]
) -> None:
    """End-to-end version of the same guarantee: the refused update must not
    produce a provider call."""
    calls: list[str] = []

    async def handler(event: Any, data: dict[str, Any]) -> None:
        calls.append("search")

    middleware = AllowlistMiddleware(settings.allowed_user_ids)
    await middleware(handler, make_message(), {"event_from_user": make_user(4242)})

    assert calls == []
    assert replies == [ACCESS_DENIED_MESSAGE]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("111,222", {111, 222}),
        ("111, 222 ,333", {111, 222, 333}),
        ("111;222", {111, 222}),
        ("", set()),
        ("abc", set()),
        ("111,abc,222", {111, 222}),
    ],
)
def test_allowlist_parsing(raw: str, expected: set[int]) -> None:
    settings = Settings(allowed_telegram_user_ids=raw, _env_file=None)
    assert set(settings.allowed_user_ids) == expected


def test_is_allowed_helper() -> None:
    middleware = AllowlistMiddleware(frozenset({111}))
    assert middleware.is_allowed(111)
    assert not middleware.is_allowed(222)
    assert not middleware.is_allowed(None)
