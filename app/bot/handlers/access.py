"""``/access`` — список допущенных и решения владельца по заявкам.

Всё здесь только для владельца, и проверка стоит в каждом хендлере отдельно, а
не одним фильтром на роутер: список допущенных — это ответ на вопрос «кто ещё
пользуется ботом», и допущенный сотрудник не обязан его видеть. Middleware в
этом не помощник: она решает, пускать ли в бота вообще, а не кто здесь главный.

Payload кнопки несёт числовой Telegram ID, а не позицию в списке: карточка
заявки живёт в чате владельца сколько угодно долго, и «одобрить третью строку»
после перезапуска одобрило бы уже другого человека.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from app.bot.access_view import (
    APPROVED_NOTICE,
    REQUEST_REJECTED,
    REVOKED_NOTICE,
    describe_user,
    notify_user,
)
from app.bot.keyboards import (
    ACCESS_ALLOW,
    ACCESS_DENY,
    ACCESS_REVOKE,
    access_list_keyboard,
)
from app.container import Container
from app.db.models import AccessRequest
from app.services.access import AccessService
from app.utils.dates import format_datetime
from app.utils.formatting import split_message

NOT_OWNER = "Команда доступна только владельцу бота."
NOT_OWNER_ALERT = "Решение по заявкам принимает владелец бота."
MODERATION_OFF = (
    "Доступ по одобрению выключен.\n\n"
    "Включается настройкой OWNER_TELEGRAM_USER_IDS в .env — там перечисляются "
    "числовые Telegram ID владельцев. Пока их нет, заявку некому показать, и "
    "посторонний просто получает «Доступ запрещён»."
)
OPEN_ACCESS_WARNING = (
    "⚠️ ALLOWED_TELEGRAM_USER_IDS = «*»: бот сейчас отвечает всем, кто его найдёт.\n"
    "Заявки в этом режиме не создаются — одобрять нечего, пущены и так все."
)
UNKNOWN_REQUEST = "Заявки от этого пользователя нет."

HEADER = "👥 ДОСТУП К БОТУ"
# Сколько строк с кнопками показывать. Предела на число рядов у Telegram нет, а
# у экрана есть: список на сорок кнопок не читается и не прокручивается.
MAX_BUTTON_ROWS = 8


async def send_access_list(message: Message, container: Container) -> None:
    access = container.access_service
    if not access.moderation_enabled:
        note = OPEN_ACCESS_WARNING if container.settings.telegram_access_is_open else MODERATION_OFF
        await message.answer(f"{HEADER}\n\n{note}\n\n{_static_block(container)}")
        return

    pending = await access.pending()
    approved = await access.approved()
    refused = await access.refused()

    blocks = [HEADER, _static_block(container)]
    if pending:
        blocks.append(_section("Ждут решения", pending, stamp=_requested_stamp))
    if approved:
        blocks.append(_section("Допущены вами", approved, stamp=_decided_stamp))
    if refused:
        blocks.append(_section("Отклонены и отозванные", refused, stamp=_decided_stamp))
    if not (pending or approved or refused):
        blocks.append("Заявок пока не было.")

    markup = access_list_keyboard(
        pending=[(row.telegram_user_id, _short(row)) for row in pending[:MAX_BUTTON_ROWS]],
        approved=[(row.telegram_user_id, _short(row)) for row in approved[:MAX_BUTTON_ROWS]],
    )
    chunks = split_message("\n\n".join(blocks))
    for chunk in chunks[:-1]:
        await message.answer(chunk)
    await message.answer(chunks[-1], reply_markup=markup)


def _static_block(container: Container) -> str:
    """Кто пущен настройкой, а не кнопкой.

    Показывается всегда и отдельно, потому что этих людей кнопкой не отозвать:
    их доступ живёт в ``.env`` на сервере, и попытка «убрать лишнего» через бот
    ничего не даст. Лучше сказать это прямо, чем оставить владельца искать
    несуществующую кнопку.
    """
    settings = container.settings
    lines = ["Пущены настройкой — правится в .env на сервере, кнопкой не отзывается:"]
    owners = sorted(settings.owner_user_ids)
    lines.append(
        "• владельцы (OWNER_TELEGRAM_USER_IDS): " + (", ".join(map(str, owners)) or "не заданы")
    )
    if settings.telegram_access_is_open:
        allowed = "«*» — открыто всем"
    else:
        allowed = ", ".join(map(str, sorted(settings.allowed_user_ids))) or "пусто"
    lines.append(f"• список (ALLOWED_TELEGRAM_USER_IDS): {allowed}")
    return "\n".join(lines)


def _section(
    title: str, rows: list[AccessRequest], *, stamp: Callable[[AccessRequest], str]
) -> str:
    lines = [f"{title} ({len(rows)}):"]
    lines.extend(f"• {describe_row(row)} — {stamp(row)}" for row in rows)
    return "\n".join(lines)


def describe_row(row: AccessRequest) -> str:
    return describe_user(
        user_id=row.telegram_user_id, username=row.username, full_name=row.full_name
    )


def _short(row: AccessRequest) -> str:
    """Подпись для кнопки: имя, если оно есть, иначе ID.

    Кнопка «Отозвать у 482913746» — это лотерея, поэтому имя важнее точности.
    Обрезается, потому что длинная подпись превращает ряд кнопок в кашу.
    """
    name = row.full_name or (f"@{row.username}" if row.username else str(row.telegram_user_id))
    return name if len(name) <= 24 else f"{name[:23]}…"


def _requested_stamp(row: AccessRequest) -> str:
    return f"заявка от {format_datetime(row.requested_at)}"


def _decided_stamp(row: AccessRequest) -> str:
    return format_datetime(row.decided_at) if row.decided_at else "—"


class Decision(Protocol):
    """Одно из трёх решений владельца.

    Протокол, а не строка с именем метода: строку легко опечатать, и опечатка
    вылезла бы во время нажатия кнопки, а не при запуске тестов.
    """

    async def __call__(self, user_id: int, *, by: int) -> AccessRequest | None: ...


def _target_id(callback: CallbackQuery) -> int | None:
    raw = (callback.data or "").rsplit(":", maxsplit=1)[-1]
    try:
        return int(raw)
    except ValueError:
        return None


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="access")

    @router.message(Command("access"))
    async def handle_access(message: Message, container: Container, user_id: int) -> None:
        if not container.access_service.is_owner(user_id):
            await message.answer(NOT_OWNER)
            return
        await send_access_list(message, container)

    @router.callback_query(F.data.startswith(f"{ACCESS_ALLOW}:"))
    async def handle_allow(
        callback: CallbackQuery, container: Container, user_id: int, bot: Bot
    ) -> None:
        await _decide(
            callback,
            container.access_service,
            user_id,
            bot,
            decide=container.access_service.approve,
            toast="Доступ открыт",
            notice=APPROVED_NOTICE,
            confirm="✅ Доступ открыт",
        )

    @router.callback_query(F.data.startswith(f"{ACCESS_DENY}:"))
    async def handle_deny(
        callback: CallbackQuery, container: Container, user_id: int, bot: Bot
    ) -> None:
        await _decide(
            callback,
            container.access_service,
            user_id,
            bot,
            decide=container.access_service.reject,
            toast="Отклонено",
            notice=REQUEST_REJECTED,
            confirm="🚫 Отклонён",
        )

    @router.callback_query(F.data.startswith(f"{ACCESS_REVOKE}:"))
    async def handle_revoke(
        callback: CallbackQuery, container: Container, user_id: int, bot: Bot
    ) -> None:
        await _decide(
            callback,
            container.access_service,
            user_id,
            bot,
            decide=container.access_service.revoke,
            toast="Доступ закрыт",
            notice=REVOKED_NOTICE,
            confirm="🚪 Доступ закрыт",
        )

    return router


async def _decide(
    callback: CallbackQuery,
    access: AccessService,
    owner_id: int,
    bot: Bot,
    *,
    decide: Decision,
    toast: str,
    notice: str,
    confirm: str,
) -> None:
    """Общий путь для «разрешить», «отклонить» и «отозвать».

    Три кнопки отличаются одним вызовом и тремя строками текста, и разводить их
    по трём копиям значит однажды забыть проверку владельца в одной из них.
    """
    if not access.is_owner(owner_id):
        await callback.answer(NOT_OWNER_ALERT, show_alert=True)
        return

    target = _target_id(callback)
    if target is None:
        await callback.answer(UNKNOWN_REQUEST, show_alert=True)
        return

    row = await decide(target, by=owner_id)
    if row is None:
        await callback.answer(UNKNOWN_REQUEST, show_alert=True)
        return

    await callback.answer(toast)
    delivered = await notify_user(bot, target, notice)
    message = callback.message
    if isinstance(message, Message):
        tail = "" if delivered else "\nСообщить ему не удалось — возможно, он заблокировал бота."
        await message.answer(f"{confirm}: {describe_row(row)}.{tail}")


__all__ = ["build_router", "send_access_list"]
