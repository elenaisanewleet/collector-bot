"""Entrypoint.

``python -m app.main`` starts the Telegram bot. ``python -m app.main demo`` runs
the pipeline end to end in the terminal, with no token and no network.
"""

from __future__ import annotations

import asyncio
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import (
    TelegramConflictError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.router import setup_dispatcher
from app.config import Settings, get_settings
from app.container import Container, build_container
from app.logging_setup import configure_logging, get_logger

logger = get_logger(__name__)

MISSING_TOKEN = "TELEGRAM_BOT_TOKEN не задан. Укажите его в .env — токен выдаёт @BotFather."
EMPTY_ALLOWLIST = (
    "ALLOWED_TELEGRAM_USER_IDS пуст: бот закрытый и никого не пустит. "
    "Укажите числовые Telegram ID через запятую."
)
BAD_TOKEN = "Telegram отклонил токен. Проверьте TELEGRAM_BOT_TOKEN в .env."
NO_NETWORK = (
    "Не удалось связаться с Telegram API. Проверьте сетевой доступ к "
    "api.telegram.org (прокси, firewall, DNS)."
)
ALREADY_RUNNING = (
    "С этим токеном уже запущен другой экземпляр бота. "
    "Остановите его или используйте отдельный токен."
)
BAD_NEWDB_FIELD_MAP = (
    "NEWDB_FIELD_MAP указывает на файл, который не читается. "
    "Пока он не исправлен, методы NewDB кроме fssp_person остались бы "
    "неподключёнными молча — поэтому запуск остановлен."
)
BAD_ONEC_FIELD_MAP = (
    "ONEC_FIELD_MAP указывает на файл, который не читается. "
    "Пока он не исправлен, 1С осталась бы неподключённой молча, а отчёт "
    "писал бы «совпадений во внутренней базе не найдено» — поэтому запуск "
    "остановлен."
)
INSECURE_ONEC_URL = (
    "ONEC_BASE_URL указывает на http://. Пароль от рабочей базы 1С уйдёт "
    "по сети открытым текстом: заголовок Basic отправляется до редиректа, "
    "поэтому переезд на https силами сервера не спасает. Укажите https:// "
    "или, если это осознанно (изолированный контур, стенд), включите "
    "ONEC_ALLOW_INSECURE_HTTP=true."
)


async def start_bot(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level, json_output=resolved.log_json)

    _validate(resolved)

    container = build_container(resolved)
    await container.database.create_all()

    bot = Bot(
        token=resolved.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dispatcher = setup_dispatcher(Dispatcher(storage=MemoryStorage()), container)

    logger.info(
        "bot.starting",
        app_name=resolved.app_name,
        mode=resolved.app_mode.value,
        allowed_users=len(resolved.allowed_user_ids),
    )
    try:
        # Drop updates queued while the bot was down: acting on a stale search
        # request after a restart is worse than losing it.
        await bot.delete_webhook(drop_pending_updates=True)
        await dispatcher.start_polling(bot)
    except TelegramUnauthorizedError as exc:
        raise SystemExit(BAD_TOKEN) from exc
    except TelegramConflictError as exc:
        raise SystemExit(ALREADY_RUNNING) from exc
    except TelegramNetworkError as exc:
        # An operator reading a stack trace learns nothing they can act on.
        logger.error("bot.network_error", detail=str(exc))
        raise SystemExit(NO_NETWORK) from exc
    except (KeyboardInterrupt, SystemExit):
        logger.info("bot.stopped")
        raise
    finally:
        await bot.session.close()
        await container.dispose()


def _validate(settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise SystemExit(MISSING_TOKEN)
    if not settings.allowed_user_ids:
        raise SystemExit(EMPTY_ALLOWLIST)
    _validate_newdb_field_map(settings)
    _validate_onec(settings)


def _validate_onec(settings: Settings) -> None:
    """Check the 1С wiring before anyone can read a report built without it.

    Both failures here are quiet ones. A broken map leaves the source
    unconnected while the report says the internal base is clean, and an
    ``http://`` endpoint leaks the customer's database password to anyone on the
    wire without anything looking wrong.
    """
    from app.providers.mapping import FieldMapError
    from app.providers.onec.lookup_map import OneCLookupMaps

    if settings.onec_base_url.lower().startswith("http://") and (
        not settings.onec_allow_insecure_http
    ):
        raise SystemExit(INSECURE_ONEC_URL)
    if settings.onec_field_map is None:
        return
    try:
        OneCLookupMaps.load(settings.onec_field_map)
    except FieldMapError as exc:
        raise SystemExit(f"{BAD_ONEC_FIELD_MAP}\n{exc.message}") from exc


def _validate_newdb_field_map(settings: Settings) -> None:
    """Read the NewDB row maps before serving anyone.

    A broken map is a configuration mistake with a quiet failure mode: every
    method it describes would report "источник не подключён" and the operator
    would read that as the truth about the sources rather than about the file.
    """
    from app.providers.mapping import FieldMapError
    from app.providers.newdb import NewDBFieldMaps

    if settings.newdb_field_map is None:
        return
    try:
        NewDBFieldMaps.load(settings.newdb_field_map)
    except FieldMapError as exc:
        raise SystemExit(f"{BAD_NEWDB_FIELD_MAP}\n{exc.message}") from exc


async def run_demo(settings: Settings | None = None) -> int:
    """Terminal walkthrough of the full pipeline — used by ``make demo``."""
    from app.demo import run_demo_flow

    return await run_demo_flow(settings)


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "demo":
        raise SystemExit(asyncio.run(run_demo()))
    asyncio.run(start_bot())


__all__ = ["Container", "main", "run_demo", "start_bot"]


if __name__ == "__main__":
    main()
