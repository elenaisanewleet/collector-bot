"""Список команд бота — один на синюю кнопку «Меню» и на справку.

Два списка команд неизбежно разъезжаются: команду добавляют в ``/help`` и
забывают в ``set_my_commands``, и человек видит в меню Telegram не то, что
умеет бот. Поэтому список здесь один, а ``/help`` и запуск бота его читают.
"""

from __future__ import annotations

from aiogram.types import BotCommand

# Порядок — по частоте, а не по алфавиту: в меню Telegram первые строки видно
# без прокрутки, и там должно стоять то, ради чего бот открывают.
BOT_COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "главное меню"),
    ("search", "проверить одного должника"),
    ("history", "последние 10 проверок"),
    ("sources", "откуда берутся данные"),
    ("help", "как это работает"),
    ("status", "состояние системы и источников"),
    ("revoke", "погасить выданные ссылки на отчёты"),
    ("audit", "последние события журнала"),
    ("cancel", "прервать текущий диалог"),
)


# Команды владельца. В общий список не идут: список допущенных — это ответ на
# вопрос «кто ещё пользуется ботом», и сотруднику его видеть незачем. Telegram
# умеет показывать разное меню разным чатам (BotCommandScopeChat), и это ровно
# тот случай, ради которого scope и придуман.
#
# Здесь же прогон и импорт. Они закрыты по владельцу
# (:class:`~app.bot.middleware.OwnerOnlyMiddleware`), а команда в синем меню,
# которая всем отвечает «нельзя», — это обещание, которого бот не держит.
OWNER_COMMANDS: tuple[tuple[str, str], ...] = (
    ("batch", "проверить всю базу и получить очередь взыскания"),
    ("import", "загрузить выгрузку должников из 1С"),
    ("access", "кто допущен к боту и заявки на доступ"),
)


def commands_for(*, owner: bool) -> tuple[tuple[str, str], ...]:
    """Что показать этому человеку — и в каком порядке.

    Владельческие команды встают сразу за ``/start``, а не в хвост: прогон по
    всей базе — главный сценарий продукта, а в меню Telegram без прокрутки видно
    первые строки.
    """
    if not owner:
        return BOT_COMMANDS
    head, *tail = BOT_COMMANDS
    return (head, *OWNER_COMMANDS, *tail)


def telegram_commands() -> list[BotCommand]:
    """Тот же список в виде, который принимает Telegram."""
    return [BotCommand(command=name, description=title) for name, title in BOT_COMMANDS]


def owner_telegram_commands() -> list[BotCommand]:
    """Общий список плюс то, что видит только владелец."""
    return [BotCommand(command=name, description=title) for name, title in commands_for(owner=True)]


def commands_help(*, owner: bool) -> str:
    """Тот же список для справки в чате."""
    lines = ["КОМАНДЫ"]
    lines.extend(f"/{name} — {title}" for name, title in commands_for(owner=owner))
    return "\n".join(lines)


__all__ = [
    "BOT_COMMANDS",
    "OWNER_COMMANDS",
    "commands_for",
    "commands_help",
    "owner_telegram_commands",
    "telegram_commands",
]
