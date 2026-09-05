"""Чистка того, что больше не нужно хранить.

Два хука существовали и раньше, но их никто не звал: история проверок и
просроченные ссылки копились бессрочно. За историей — ФИО, дата рождения и ИНН,
за ссылками — карта «оператор → какой отчёт он смотрел». Молчащий хук
ретеншена хуже отсутствующего: он выглядит как выполненное требование.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from app.config import Settings
from app.db.repository import QueryCardRepository, SearchRepository, ShareLinkRepository
from app.db.session import Database
from app.logging_setup import get_logger
from app.utils.dates import utcnow

logger = get_logger(__name__)

# Раз в сутки: чаще незачем, реже — и день простоя копит лишнее.
RETENTION_INTERVAL_SECONDS = 24 * 60 * 60


#: Сколько живёт брошенная карточка запроса. Не ``HISTORY_RETENTION_DAYS``: то
#: срок хранения истории проверок, а карточка — черновик на несколько минут, и
#: держать чужие ФИО девяносто дней ради него незачем. Трёх суток хватает,
#: чтобы вернуться к недособранному должнику после выходных.
CARD_RETENTION_DAYS = 3


async def purge_once(settings: Settings, database: Database) -> tuple[int, int, int]:
    """Один проход. Возвращает (запросов, ссылок, карточек)."""
    async with database.session() as session:
        links = await ShareLinkRepository(session).purge_expired()
        requests = 0
        if settings.history_retention_days:
            cutoff = utcnow() - timedelta(days=settings.history_retention_days)
            requests = await SearchRepository(session).purge_older_than(cutoff)
        cards = await QueryCardRepository(session).purge_older_than(
            utcnow() - timedelta(days=CARD_RETENTION_DAYS)
        )
    if requests or links or cards:
        logger.info("retention.purged", requests=requests, share_links=links, cards=cards)
    return requests, links, cards


async def run_retention(settings: Settings, database: Database) -> None:
    """Фоновая задача рядом с ботом: проход при старте и дальше раз в сутки."""
    while True:
        try:
            await purge_once(settings, database)
        except Exception as exc:  # pragma: no cover - чистка не должна ронять бота
            logger.warning("retention.failed", error=type(exc).__name__, detail=str(exc))
        await asyncio.sleep(RETENTION_INTERVAL_SECONDS)
