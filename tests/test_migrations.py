"""Миграции и модели — одна схема, а не две.

Приложение поднимает схему через ``create_all``, а деплой — через
``alembic upgrade head``. Пока за этими двумя путями никто не следит, они
расходятся молча и обнаруживаются на проде, где ``create_all`` уже не спасёт.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from app.config import get_settings
from app.db import models  # noqa: F401  — регистрирует таблицы в метаданных
from app.db.base import Base

REPO_ROOT = Path(__file__).resolve().parents[1]


def _upgraded_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, set[str]]:
    # env.py берёт адрес базы из настроек приложения, поэтому подменяем его
    # через окружение, а не через alembic.ini.
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()
    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    try:
        command.upgrade(config, "head")
    finally:
        get_settings.cache_clear()

    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        return {
            table: {column["name"] for column in inspector.get_columns(table)}
            for table in inspector.get_table_names()
            if table != "alembic_version"
        }
    finally:
        engine.dispose()


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_migrations_match_the_models(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    migrated = _upgraded_schema(tmp_path, monkeypatch)
    expected = {
        name: {column.name for column in table.columns}
        for name, table in Base.metadata.tables.items()
    }

    assert set(migrated) == set(expected), "набор таблиц разошёлся с моделями"
    for table, columns in expected.items():
        assert migrated[table] == columns, f"колонки таблицы {table} разошлись с моделью"
