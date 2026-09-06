"""Entrypoint.

``python -m app.main`` starts the Telegram bot. ``python -m app.main demo`` runs
the pipeline end to end in the terminal, with no token and no network.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import (
    TelegramAPIError,
    TelegramConflictError,
    TelegramNetworkError,
    TelegramUnauthorizedError,
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommandScopeChat, MenuButtonCommands

from app.bot.commands import owner_telegram_commands, telegram_commands
from app.bot.router import setup_dispatcher
from app.config import Settings, get_settings
from app.container import build_container
from app.logging_setup import configure_logging, get_logger
from app.services.retention import run_retention
from app.web.app import run_web_server

logger = get_logger(__name__)

MISSING_TOKEN = "TELEGRAM_BOT_TOKEN не задан. Укажите его в .env — токен выдаёт @BotFather."
EMPTY_ALLOWLIST = (
    "ALLOWED_TELEGRAM_USER_IDS пуст: бот закрытый и никого не пустит. "
    "Укажите числовые Telegram ID через запятую — или «*», чтобы открыть всем. "
    "Третий вариант — OWNER_TELEGRAM_USER_IDS: тогда посторонний присылает "
    "заявку, а владелец пускает его кнопкой."
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
INSECURE_WEB_URL = (
    "WEB_PUBLIC_URL должен начинаться с https://. По http токен доступа к отчёту "
    "идёт открытым текстом, а за ссылкой персональные данные должника. "
    "Для локальной отладки поставьте WEB_ALLOW_INSECURE=true."
)
BAD_NEWDB_FIELD_MAP = (
    "NEWDB_FIELD_MAP указывает на файл, который не читается. "
    "Пока он не исправлен, методы NewDB кроме fssp_person остались бы "
    "неподключёнными молча — поэтому запуск остановлен."
)


async def _upgrade_schema() -> None:
    """Привести схему базы к текущей ревизии перед стартом.

    Раньше здесь стоял ``create_all``, создающий таблицы прямо из моделей, хотя
    его собственная документация отправляла развёртывание к миграциям. Так и
    вышло: на сервере схема родилась из моделей, ``alembic_version`` остался на
    0005, а к моменту проверки код ушёл на 0009. Расхождение не проявлялось,
    пока таблицы были пусты, и всплыло бы на первой же выгрузке заказчика —
    «no such column: debtors.inn» вместо импорта.

    Миграции идут на старте, а не руками при выкладке: ручной шаг здесь уже был
    и уже был забыт. Alembic внутри поднимает свой цикл событий, поэтому
    вызывается в отдельном потоке.
    """

    def _run() -> None:
        from alembic import command
        from alembic.config import Config

        root = Path(__file__).resolve().parent.parent
        config = Config(str(root / "alembic.ini"))
        config.set_main_option("script_location", str(root / "migrations"))
        command.upgrade(config, "head")

    await asyncio.to_thread(_run)
    logger.info("db.schema_upgraded")


async def start_bot(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level, json_output=resolved.log_json)

    _validate(resolved)

    container = build_container(resolved)
    await _upgrade_schema()

    bot = Bot(
        token=resolved.telegram_bot_token,
        default=DefaultBotProperties(parse_mode=None),
    )
    dispatcher = setup_dispatcher(Dispatcher(storage=MemoryStorage()), container)

    # Веб-сервер отчётов живёт в том же цикле событий, что и опрос Telegram:
    # это одна и та же библиотека (aiohttp), отдельный процесс не нужен.
    web_runner = await run_web_server(container) if resolved.web_enabled else None
    # Ретеншен идёт тем же циклом: отдельный планировщик ради одной ежесуточной
    # задачи — лишняя движущаяся часть.
    retention = asyncio.create_task(run_retention(resolved, container.database))

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
        await publish_commands(bot, resolved)
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
        retention.cancel()
        if web_runner is not None:
            await web_runner.cleanup()
        await bot.session.close()
        await container.dispose()


async def publish_commands(bot: Bot, settings: Settings | None = None) -> None:
    """Синяя кнопка «Меню» со списком команд.

    Без неё команды существуют, но их негде увидеть: человек либо знает слово
    после слеша, либо не знает. Отправляется при каждом старте, потому что
    список живёт на стороне Telegram и после правки кода сам не обновится.

    Владельцу дополнительно уходит свой список — с ``/access``. Область
    ``BotCommandScopeChat`` для чата, в который бот ещё ни разу не писал,
    отвергается Telegram, поэтому каждый владелец обрабатывается отдельно:
    один недоступный не должен лишить меню остальных.

    Отказ Telegram не должен ронять бота: список команд — удобство, а приём
    сообщений — работа. Ошибку логируем и идём дальше.
    """
    try:
        await bot.set_my_commands(telegram_commands())
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramAPIError as exc:
        logger.warning("commands.publish_failed", detail=str(exc))

    for owner_id in sorted(settings.owner_user_ids) if settings else ():
        try:
            await bot.set_my_commands(
                owner_telegram_commands(), scope=BotCommandScopeChat(chat_id=owner_id)
            )
        except TelegramAPIError as exc:
            logger.warning("commands.owner_publish_failed", owner_id=owner_id, detail=str(exc))


def _validate(settings: Settings) -> None:
    if not settings.telegram_bot_token:
        raise SystemExit(MISSING_TOKEN)
    if (
        not settings.allowed_user_ids
        and not settings.telegram_access_is_open
        and not settings.owner_user_ids
    ):
        # Владелец в списке — уже не «никого не пустит»: он и сам работает, и
        # пускает остальных кнопкой. Пустой ALLOWED_TELEGRAM_USER_IDS при
        # заданном владельце — рабочая конфигурация, а не ошибка.
        raise SystemExit(EMPTY_ALLOWLIST)
    if settings.web_url_is_insecure and not settings.web_allow_insecure:
        raise SystemExit(INSECURE_WEB_URL)
    if settings.access_moderation_enabled:
        logger.info("access.moderated", owners=len(settings.owner_user_ids))
    if settings.telegram_access_is_open:
        # Не отказ и не предупреждение в лог, которое никто не прочтёт: строка
        # печатается при каждом старте, потому что открытый бот тратит чужими
        # руками оплаченный баланс и тянет данные живых людей.
        logger.warning(
            "access.open",
            note="ALLOWED_TELEGRAM_USER_IDS=* — бот отвечает всем, кто его найдёт",
            # «*» сильнее владельцев: заявок не будет вовсе, потому что пущены
            # уже все. Молчать об этом нельзя — владелец, вписавший себя в
            # OWNER_TELEGRAM_USER_IDS, вправе думать, что включил одобрение.
            owner_approval="выключено символом «*»" if settings.owner_user_ids else "не настроено",
        )
    _validate_newdb_field_map(settings)


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


__all__ = ["main", "publish_commands", "run_demo", "start_bot"]


if __name__ == "__main__":
    main()
