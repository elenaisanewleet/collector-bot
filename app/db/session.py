"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    """SQLite забывает про внешние ключи на каждом новом соединении.

    ``PRAGMA foreign_keys`` по умолчанию выключен и действует ровно одно
    соединение, поэтому объявленный в схеме ``ON DELETE CASCADE`` молча ничего
    не делал: ретеншен удалял строку ``search_requests``, а ``search_results`` и
    ``debtor_reports`` оставались в базе — с ФИО должников, данными посторонних
    умерших и нотариусами — и продолжали читаться по ``search_request_id``.
    Хук рапортовал об успехе, потому что rowcount родителя действительно был
    больше нуля. Прод по умолчанию на SQLite, срок хранения объявлен 90 дней —
    фактически данные лежали бессрочно.

    Выбран прагма-хук, а не явное удаление детей в ретеншене. Причина: пар
    «родитель — ребёнок» в схеме несколько (запрос → результаты и отчёт, прогон
    → элементы очереди, должник → элементы очереди), и явный DELETE пришлось бы
    повторять на каждом месте удаления и держать в синхронности со схемой —
    ровно тот вид дублирования, который однажды и разошёлся. Здесь же
    включается то, что схема уже объявила, причём не только для ретеншена:
    новые сироты перестают появляться вообще.

    На других СУБД (PostgreSQL) внешние ключи и так работают, поэтому хук
    вешается только на SQLite.
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


class Database:
    """Owns the engine and hands out sessions.

    Kept as an object rather than module globals so tests can spin up an
    isolated in-memory database per case.
    """

    def __init__(self, url: str, *, echo: bool = False) -> None:
        self._url = url
        self._engine: AsyncEngine = create_async_engine(
            url, echo=echo, future=True, pool_pre_ping=True
        )
        self._session_factory = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )
        if self._engine.dialect.name == "sqlite":
            # На каждом соединении, а не один раз на движке: пул открывает их
            # заново, и прагма живёт вместе с соединением.
            event.listen(self._engine.sync_engine, "connect", _enable_sqlite_foreign_keys)

    @property
    def url(self) -> str:
        return self._url

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that commits on success and rolls back on failure."""
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def create_all(self) -> None:
        """Create the schema directly.

        Used by tests and by ``make demo`` for a zero-step start. Deployments use
        Alembic instead — see ``migrations/``.
        """
        self._ensure_sqlite_directory()
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def dispose(self) -> None:
        await self._engine.dispose()

    def _ensure_sqlite_directory(self) -> None:
        if not self._url.startswith("sqlite"):
            return
        _, _, tail = self._url.partition(":///")
        if not tail or tail.startswith(":memory:"):
            return
        parent = Path(tail).expanduser().parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
