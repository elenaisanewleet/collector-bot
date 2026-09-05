"""``/help`` — commands, limits and an explicit statement of what is connected."""

from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from app.bot.keyboards import main_menu
from app.container import Container
from app.domain.enums import PROVIDER_TITLES, ProviderName

COMMANDS = """Команды:
/start — главное меню
/search — новая проверка
/history — последние 10 проверок
/import — импорт CSV с должниками
/help — эта справка
/cancel — прервать текущий диалог"""

LIMITS = """Что делает сервис:
• ищет должника в нашей базе и в подключённых легальных источниках;
• сопоставляет записи и показывает уровень совпадения;
• считает Recovery Score с объяснением каждого фактора.

Чего сервис не делает:
• не ищет банковские счета и остатки;
• не определяет местоположение;
• не использует базы утечек и «пробив»;
• не заменяет юридическую проверку."""

CONFIDENCE_NOTE = """Про совпадения:
Одинаковое ФИО из разных источников — это ещё не один человек.
Подтверждённым совпадение становится только при совпадении
даты рождения или ИНН. Остальное показывается как «возможное»."""


def _sources_section(container: Container) -> str:
    """Lists each source and whether it is actually connected right now."""
    lines = ["Источники:"]
    if container.settings.is_demo:
        lines.append("⚠️ Демо-режим: внешние источники заменены тестовыми данными.")
    for provider in container.registry.external:
        title = PROVIDER_TITLES.get(provider.name, provider.name.value)
        mark = "✓" if provider.is_configured else "○"
        state = "подключено" if provider.is_configured else "не подключено"
        lines.append(f"{mark} {title} — {state}")
    lines.append(f"✓ {PROVIDER_TITLES[ProviderName.INTERNAL]} — CSV + внутренняя база")
    bridge = container.registry.inn_bridge
    if bridge is not None:
        # Мост не источник фактов, поэтому идёт отдельной строкой и со своим
        # объяснением: без него три источника выше не проверяются вовсе.
        mark = "✓" if bridge.is_configured else "○"
        state = (
            "подключено — один платный вызов на должника"
            if bridge.is_configured
            else "не подключено: без ИНН банкротство, статус ИП и арбитраж не проверяются"
        )
        lines.append(f"{mark} {PROVIDER_TITLES[ProviderName.INN_BRIDGE]} — {state}")
    return "\n".join(lines)


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="help")

    @router.message(Command("help"))
    async def handle_help(message: Message, container: Container) -> None:
        text = "\n\n".join(
            [
                f"{container.settings.app_name} — справка",
                COMMANDS,
                LIMITS,
                CONFIDENCE_NOTE,
                _sources_section(container),
            ]
        )
        await message.answer(text, reply_markup=main_menu())

    return router
