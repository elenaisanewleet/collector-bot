"""Access control and request context.

The bot is closed. The allowlist check runs before any handler, so an
unauthorized user cannot start a search, create a search request, or cause a
single outbound call to an external API. The refusal is logged — the user id and
nothing else — so an attempt is visible without recording what was asked.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.logging_setup import get_logger

logger = get_logger(__name__)

ACCESS_DENIED_MESSAGE = "Доступ запрещён."


class AllowlistMiddleware(BaseMiddleware):
    """Rejects every update from a user outside ``ALLOWED_TELEGRAM_USER_IDS``.

    An empty allowlist denies everyone: a closed tool must fail shut, never open.
    """

    def __init__(self, allowed_user_ids: frozenset[int], *, open_access: bool = False) -> None:
        self._allowed = allowed_user_ids
        self._open = open_access
        if open_access:
            logger.warning(
                "allowlist.open",
                note="bot is open to everyone: every stranger spends the balance",
            )
        elif not allowed_user_ids:
            logger.warning("allowlist.empty", note="no user can access the bot")

    def is_allowed(self, user_id: int | None) -> bool:
        if user_id is None:
            return False
        return self._open or user_id in self._allowed

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        user_id = user.id if user else None

        if self.is_allowed(user_id):
            data["user_id"] = user_id
            return await handler(event, data)

        # Deliberately does not log the message text: the request content of an
        # unauthorized user is not ours to retain.
        logger.warning("access.denied", user_id=user_id)
        await _refuse(event)
        return None


async def _refuse(event: TelegramObject) -> None:
    if isinstance(event, Message):
        await event.answer(ACCESS_DENIED_MESSAGE)
    elif isinstance(event, CallbackQuery):
        await event.answer(ACCESS_DENIED_MESSAGE, show_alert=True)


class DependencyMiddleware(BaseMiddleware):
    """Injects the application container into every handler.

    Handlers receive services already constructed; they never build their own,
    which keeps wiring in one place and makes handlers trivial to test.
    """

    def __init__(self, **dependencies: Any) -> None:
        self._dependencies = dependencies

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        data.update(self._dependencies)
        return await handler(event, data)
