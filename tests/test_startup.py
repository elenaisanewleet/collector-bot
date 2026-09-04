"""Startup validation.

The bot must refuse to start misconfigured rather than come up in a state where
it cannot work or, worse, would let anyone in.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.container import build_container
from app.main import EMPTY_ALLOWLIST, MISSING_TOKEN, _validate


def test_missing_token_stops_startup(settings: Settings) -> None:
    with pytest.raises(SystemExit) as exc_info:
        _validate(settings.model_copy(update={"telegram_bot_token": ""}))
    assert MISSING_TOKEN in str(exc_info.value)


def test_empty_allowlist_stops_startup(settings: Settings) -> None:
    """A closed bot with no allowlist can only be a misconfiguration."""
    with pytest.raises(SystemExit) as exc_info:
        _validate(settings.model_copy(update={"allowed_telegram_user_ids": ""}))
    assert EMPTY_ALLOWLIST in str(exc_info.value)


def test_valid_configuration_passes(settings: Settings) -> None:
    _validate(settings)  # does not raise


async def test_container_wires_every_service(settings: Settings) -> None:
    container = build_container(settings)
    try:
        assert container.search_service is not None
        assert container.import_service is not None
        assert container.registry.internal is not None
        assert container.registry.external
        # The schema is creatable from the models alone, for tests and `make demo`.
        await container.database.create_all()
    finally:
        await container.dispose()


async def test_database_schema_matches_the_migration(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Alembic and the ORM models must not drift apart."""
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.db import models  # noqa: F401  - registers the tables
    from app.db.base import Base
    from app.db.session import Database

    url = f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}"
    database = Database(url)
    await database.create_all()
    await database.dispose()

    engine = create_async_engine(url)
    async with engine.connect() as connection:
        diff = await connection.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )
    await engine.dispose()

    assert diff == [], f"schema drift: {diff}"
