"""Тексты и рассылка вокруг доступа по одобрению.

Отдельный модуль, потому что читателей у этих строк двое и они на разных
этажах: middleware (она встречает незнакомца до всякого хендлера) и хендлер
кнопок владельца. Класть их в любой из двух означало бы импорт хендлера из
middleware или наоборот.

Тон здесь ровный. Незнакомец, которому отказали, — не нарушитель: чаще всего
это сотрудник заказчицы, который написал боту раньше, чем его вписали в список.
Владелец же должен видеть не «пользователь 482…», а имя, ник и ID, потому что
решение он принимает по ним.
"""

from __future__ import annotations

from collections.abc import Iterable

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import User

from app.bot.keyboards import access_decision_keyboard
from app.logging_setup import get_logger
from app.services.access import REQUEST_COOLDOWN_HOURS
from app.utils.formatting import pluralize_ru

logger = get_logger(__name__)

REQUEST_SENT = """🔒 Доступ к боту — по одобрению владельца.

Бот проверяет должников по официальным реестрам, и каждая проверка тратит \
запросы, оплаченные владельцем. Поэтому доступ выдаётся вручную.

Запрос отправлен. Когда его рассмотрят, бот напишет сюда сам — писать ещё раз \
не нужно."""

REQUEST_PENDING = (
    "🔒 Запрос на доступ уже отправлен и ждёт решения владельца. "
    "Как только его рассмотрят, бот напишет сюда сам."
)

REQUEST_REJECTED = "🚫 Владелец отклонил запрос на доступ."

APPROVED_NOTICE = (
    "✅ Владелец открыл вам доступ.\n\nНажмите /start — бот покажет меню и кнопки внизу экрана."
)

REVOKED_NOTICE = "🚪 Владелец закрыл вам доступ к боту."

OWNER_HEADER = "🔔 Запрос на доступ к боту"

# Владелец нажимает кнопку один раз, а последствия у неё длинные: допущенный
# тратит оплаченный баланс и тянет данные живых людей под учётной записью
# владельца. Строка про это стоит над кнопками, а не в справке.
OWNER_STAKES = (
    "Если разрешить, он сможет запускать проверки: они тратят ваш оплаченный "
    "баланс и поднимают данные людей из официальных реестров."
)

NO_OWNER_REACHED = "access.owner_unreachable"


def request_throttled(hours: int) -> str:
    """Отказ и сколько ждать.

    Срок называется числом, а не «попробуйте позже»: человек, которому не
    сказали срок, пробует каждые пять минут.
    """
    noun = pluralize_ru(hours, "час", "часа", "часов")
    return (
        f"{REQUEST_REJECTED}\n\n"
        f"Отправить новый запрос можно через {hours} {noun} — не чаще раза в сутки."
    )


def describe_user(*, user_id: int, username: str | None, full_name: str | None) -> str:
    """Одна строка про человека для списков и подтверждений."""
    parts = [full_name or "без имени"]
    if username:
        parts.append(f"@{username}")
    parts.append(f"ID {user_id}")
    return " · ".join(parts)


def owner_request_text(*, user_id: int, username: str | None, full_name: str | None) -> str:
    lines = [
        OWNER_HEADER,
        "",
        f"Имя: {full_name or '—'}",
        f"Username: @{username}" if username else "Username: не задан",
        f"ID: {user_id}",
        "",
        OWNER_STAKES,
        f"Если отклонить, следующий запрос он сможет отправить не раньше "
        f"чем через {REQUEST_COOLDOWN_HOURS} ч.",
    ]
    return "\n".join(lines)


async def notify_owners_of_request(bot: Bot, owner_ids: Iterable[int], user: User) -> None:
    """Показать заявку каждому владельцу.

    Ошибка доставки одному владельцу не должна отменять доставку остальным и
    тем более ронять обработку апдейта: владелец, который ещё ни разу не писал
    боту, для Telegram не существует — ``chat not found``, — а заявка при этом
    уже лежит в базе и видна по /access.
    """
    text = owner_request_text(
        user_id=user.id, username=user.username, full_name=user.full_name or None
    )
    markup = access_decision_keyboard(user.id)
    for owner_id in owner_ids:
        try:
            await bot.send_message(owner_id, text, reply_markup=markup)
        except TelegramAPIError as exc:
            logger.warning(NO_OWNER_REACHED, owner_id=owner_id, detail=str(exc))


async def notify_user(bot: Bot, user_id: int, text: str) -> bool:
    """Сообщить человеку решение по его заявке.

    Возвращает, дошло ли: владельцу это важно знать. Человек мог заблокировать
    бота между заявкой и решением, и тогда «доступ открыт» никто не прочитает.
    """
    try:
        await bot.send_message(user_id, text)
    except TelegramAPIError as exc:
        logger.warning("access.notify_failed", user_id=user_id, detail=str(exc))
        return False
    return True


__all__ = [
    "APPROVED_NOTICE",
    "OWNER_HEADER",
    "REQUEST_PENDING",
    "REQUEST_REJECTED",
    "REQUEST_SENT",
    "REVOKED_NOTICE",
    "describe_user",
    "notify_owners_of_request",
    "notify_user",
    "owner_request_text",
    "request_throttled",
]
