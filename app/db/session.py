"""Async engine and session factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.db.base import Base


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
