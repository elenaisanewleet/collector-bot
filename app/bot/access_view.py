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
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from app.bot.keyboards import access_decision_keyboard
from app.logging_setup import get_logger
from app.services.access import REQUEST_COOLDOWN_HOURS
from app.utils.formatting import pluralize_ru

logger = get_logger(__name__)

REQUEST_SENT = """🔒 Доступ к боту — по одобрению владельца.

Каждая проверка тратит запросы, оплаченные владельцем. Поэтому доступ \
выдаётся вручную.

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
    "баланс и поднимают данные живых людей под вашей учётной записью."
)

NO_OWNER_REACHED = "access.owner_unreachable"


# ------------------------------------------------- дорогое и опасное: владельцу


@dataclass(frozen=True, slots=True)
class OwnerOnlyAction:
    """Действие, которое допущенному не открыто.

    Название и причина ходят парой, потому что отказ без причины читается как
    «бот сломался» или «меня подозревают», а здесь ни то, ни другое: допущенный
    сотрудник — свой, просто прогон по всей базе и импорт стоят денег и правят
    ту базу, по которой заказчик идёт в суд.
    """

    title: str
    why: str


BATCH_ACTION = OwnerOnlyAction(
    title="Проверка всей базы и выгрузка очереди",
    why=(
        "Одно нажатие уходит в платные источники по всем должникам сразу — это "
        "сотни оплаченных обращений, — а очередь и список выгружаются файлом со "
        "всеми персональными данными."
    ),
)

IMPORT_ACTION = OwnerOnlyAction(
    title="Импорт выгрузки должников",
    why=(
        "Импорт правит ту самую базу, по которой решают, на кого подавать в суд: "
        "строка, попавшая в неё со стороны, становится чужим решением о взыскании."
    ),
)


BASE_ACTION = OwnerOnlyAction(
    title="Список должников",
    why=(
        "За ссылкой вся база: имена, даты рождения, адреса, машины и суммы. "
        "Кому её открывать, решает владелец, а не тот, кому она понадобилась."
    ),
)


CALC_ACTION = OwnerOnlyAction(
    title="Как считается пошлина",
    why=(
        "Экран раскрывает тариф, ступени и порог окупаемости — то, из чего "
        "складывается решение о деньгах. Подписывает это решение владелец, "
        "и проверять расчёт — его дело."
    ),
)


AUDIT_ACTION = OwnerOnlyAction(
    title="Журнал событий",
    why=(
        "Журнал показывает, кто из коллег какие проверки запускал и с каким "
        "результатом. Это ответ на вопрос про людей, а не про должников, и "
        "отвечать на него — дело владельца бота."
    ),
)


def owner_only_alert(user_id: int | None) -> str:
    """Всплывающее окно на нажатие кнопки. Самодостаточно — другого места нет.

    Кнопка могла приехать в пересланном сообщении, и тогда сообщения, к
    которому её прицепили, у нажавшего нет: ответить в чат не выйдет, и
    объяснение целиком должно уместиться в эти двести символов Telegram.
    """
    who = f" Ваш ID: {user_id}." if user_id is not None else ""
    return (
        "🔒 Это делает владелец бота: прогон по всей базе и импорт. "
        f"Проверка одного должника вам открыта. Доступ владельца — по его решению.{who}"
    )


def owner_only_text(action: OwnerOnlyAction, user_id: int | None) -> str:
    """Отказ, после которого понятно, что делать дальше.

    «Недостаточно прав» — это тупик: человек не знает ни чьё это право, ни как
    его получить, ни что ему всё-таки можно. Поэтому здесь четыре блока: что
    закрыто, почему, что вместо этого доступно и к кому идти.
    """
    lines = [
        f"🔒 {action.title} — только для владельца бота.",
        "",
        action.why,
        "",
        "Вам это доступно: проверка одного должника — кнопка «🔍 Проверить одного» "
        "или команда /search. Она работает как работала.",
        "",
        "Как получить: попросите владельца бота открыть вам эти действия — он "
        "добавляет Telegram ID в настройку OWNER_TELEGRAM_USER_IDS.",
    ]
    if user_id is not None:
        lines.append(f"Ваш Telegram ID: {user_id} — его и назовите.")
    return "\n".join(lines)


async def refuse_owner_only(
    event: TelegramObject, *, action: OwnerOnlyAction, user_id: int | None
) -> None:
    """Сказать «нет» так, чтобы это дошло любым путём.

    Путей три, и они не взаимозаменяемы: команда со слешем и нажатие нижней
    кнопки приходят сообщением, нажатие инлайн-кнопки — колбэком, а колбэк из
    пересланного сообщения приходит без доступного сообщения вовсе. Молчание на
    третьем пути выглядит как зависший бот, поэтому всплывающее окно уходит
    всегда, а подробный текст — когда есть куда.
    """
    text = owner_only_text(action, user_id)
    if isinstance(event, Message):
        await event.answer(text)
        return
    if isinstance(event, CallbackQuery):
        await event.answer(owner_only_alert(user_id), show_alert=True)
        target = event.message
        if isinstance(target, Message):
            await target.answer(text)


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
    "AUDIT_ACTION",
    "BASE_ACTION",
    "BATCH_ACTION",
    "CALC_ACTION",
    "IMPORT_ACTION",
    "OWNER_HEADER",
    "REQUEST_PENDING",
    "REQUEST_REJECTED",
    "REQUEST_SENT",
    "REVOKED_NOTICE",
    "OwnerOnlyAction",
    "describe_user",
    "notify_owners_of_request",
    "notify_user",
    "owner_only_alert",
    "owner_only_text",
    "owner_request_text",
    "refuse_owner_only",
    "request_throttled",
]
