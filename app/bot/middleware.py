"""Access control and request context.

The bot is closed. The allowlist check runs before any handler, so an
unauthorized user cannot start a search, create a search request, or cause a
single outbound call to an external API. The refusal is logged — the user id and
nothing else — so an attempt is visible without recording what was asked.

Незнакомец при этом упирается не обязательно в стену. Если у бота есть владелец
(``OWNER_TELEGRAM_USER_IDS``), первое сообщение постороннего превращается в
заявку: он читает, что доступ по одобрению, владелец получает карточку с именем
и кнопками. Это единственное, что посторонний может вызвать в боте, и оно не
доходит ни до одного хендлера и ни до одного платного запроса — заявка пишется
здесь же, в middleware.

Границ здесь две, и вторая появилась не от подозрительности. Допущенный — это
чаще всего сотрудник, и одиночная проверка ему открыта. Но «Проверить всю базу»
одним нажатием уводит в платные источники всю выгрузку заказчика, а импорт
правит ту базу, по которой решают, на кого подавать в суд. Поэтому дорогое и
опасное закрыто отдельно — :class:`OwnerOnlyMiddleware`, — и закрыто по
владельцам, а не по списку допущенных.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware, Bot
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.bot.access_view import (
    REQUEST_PENDING,
    REQUEST_SENT,
    OwnerOnlyAction,
    notify_owners_of_request,
    refuse_owner_only,
    request_throttled,
)
from app.container import Container
from app.logging_setup import get_logger
from app.services.access import AccessService, RequestOutcome

logger = get_logger(__name__)

ACCESS_DENIED_MESSAGE = "Доступ запрещён."


class AllowlistMiddleware(BaseMiddleware):
    """Rejects every update from a user outside ``ALLOWED_TELEGRAM_USER_IDS``.

    An empty allowlist denies everyone: a closed tool must fail shut, never open.

    ``access`` включает режим одобрения. Без него поведение ровно прежнее —
    список из ``.env`` и отказ всем остальным; с ним решение принимает
    :class:`~app.services.access.AccessService`, у которого порядок проверок
    записан в одном месте, а не размазан между настройкой и middleware.
    """

    def __init__(
        self,
        allowed_user_ids: frozenset[int],
        *,
        open_access: bool = False,
        access: AccessService | None = None,
    ) -> None:
        self._allowed = allowed_user_ids
        self._open = open_access
        self._access = access
        if open_access:
            logger.warning(
                "allowlist.open",
                note="bot is open to everyone: every stranger spends the balance",
            )
        elif access is not None and access.moderation_enabled:
            logger.info("allowlist.moderated", owners=len(access.owner_ids))
        elif not allowed_user_ids:
            logger.warning("allowlist.empty", note="no user can access the bot")

    def is_allowed(self, user_id: int | None) -> bool:
        """Синхронная проверка по одному только ``.env``.

        Осталась синхронной намеренно: решения, принятые кнопкой, лежат в базе и
        читаются :meth:`AccessService.is_allowed`, а эта отвечает на вопрос
        «пущен ли он настройкой» — там, где ходить в базу незачем.
        """
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

        if await self._passes(user_id):
            data["user_id"] = user_id
            return await handler(event, data)

        # Deliberately does not log the message text: the request content of an
        # unauthorized user is not ours to retain.
        logger.warning("access.denied", user_id=user_id)
        if self._access is not None and self._access.moderation_enabled and user is not None:
            await self._offer_request(event, data, user)
            return None
        await _refuse(event)
        return None

    async def _passes(self, user_id: int | None) -> bool:
        if self._access is None:
            return self.is_allowed(user_id)
        return await self._access.is_allowed(user_id)

    async def _offer_request(self, event: TelegramObject, data: dict[str, Any], user: User) -> None:
        """Первое сообщение незнакомца — это заявка.

        Только для сообщений. Заявку по нажатию инлайн-кнопки не принимаем:
        кнопка у постороннего может взяться лишь из пересланного чужого
        сообщения, и «запрос отправлен» в ответ на такое нажатие объяснило бы
        человеку не то, что произошло.
        """
        access = self._access
        if access is None:  # pragma: no cover - вызывается только под проверкой
            return
        if not isinstance(event, Message):
            await _refuse(event)
            return

        result = await access.submit_request(
            user_id=user.id, username=user.username, full_name=user.full_name or None
        )
        if result.outcome is RequestOutcome.THROTTLED:
            await event.answer(request_throttled(result.retry_after_hours))
            return
        if result.outcome is RequestOutcome.PENDING:
            await event.answer(REQUEST_PENDING)
            return

        await event.answer(REQUEST_SENT)
        bot = data.get("bot")
        if isinstance(bot, Bot):
            await notify_owners_of_request(bot, access.owner_ids, user)


async def _refuse(event: TelegramObject) -> None:
    if isinstance(event, Message):
        await event.answer(ACCESS_DENIED_MESSAGE)
    elif isinstance(event, CallbackQuery):
        await event.answer(ACCESS_DENIED_MESSAGE, show_alert=True)


class OwnerOnlyMiddleware(BaseMiddleware):
    """Пускает к хендлерам роутера только владельца.

    Вешается на роутер целиком, а не проверкой в каждом хендлере: у прогона по
    базе четыре входа (команда, инлайн-кнопка, подтверждение сметы, выгрузка), у
    импорта — три, и проверка, размноженная по семи местам, однажды окажется
    забытой в восьмом. Отсюда же условие: под этим роутером не должно быть ни
    одного хендлера, открытого допущенным.

    Middleware роутерная (inner), а не диспетчерная: она срабатывает только
    когда апдейт УЖЕ подошёл хендлеру этого роутера, поэтому чужие сообщения
    идут дальше по цепочке нетронутыми — иначе роутер импорта, стоящий выше
    свободного ввода, глотал бы чужой текст.
    """

    def __init__(self, action: OwnerOnlyAction) -> None:
        self._action = action

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user_id = data.get("user_id")
        container = data.get("container")
        if isinstance(container, Container) and container.access_service.is_owner(user_id):
            return await handler(event, data)

        # Логируем ровно то же, что и отказ на входе в бота: кто, и ничего о том,
        # что он хотел сделать.
        logger.warning("access.owner_only_denied", user_id=user_id, action=self._action.title)
        await refuse_owner_only(event, action=self._action, user_id=user_id)
        return None


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
