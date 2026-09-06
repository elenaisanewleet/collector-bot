"""Миграции и модели — одна схема, а не две.

Тесты и ``make demo`` поднимают схему через ``create_all``, а запуск бота и
деплой — через ``alembic upgrade head``. Пока за этими двумя путями никто не
следит, они расходятся молча и обнаруживаются на проде, где ``create_all`` уже
не спасёт: недостающую таблицу он создаст, недостающую колонку в существующей —
нет.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
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


@pytest.mark.asyncio
@pytest.mark.filterwarnings("ignore::DeprecationWarning")
async def test_startup_brings_an_outdated_database_to_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Запуск бота обязан догонять схему, а не полагаться на ручную выкладку.

    Ровно это и разошлось на сервере: таблицы там родились из моделей, версия
    осталась на 0005, а код ушёл на 0009. ``create_all`` создаёт недостающие
    таблицы, но не добавляет колонки в существующие, поэтому расхождение
    пряталось, пока таблицы были пусты, и вылезло бы на первой выгрузке
    заказчика ошибкой «no such column: debtors.inn».
    """
    import asyncio

    from sqlalchemy import text

    from app.main import _upgrade_schema

    db_path = tmp_path / "outdated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path}")
    get_settings.cache_clear()

    config = Config(str(REPO_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    try:
        # Alembic внутри поднимает свой цикл событий — из async-теста его,
        # как и в проде, приходится звать отдельным потоком.
        await asyncio.to_thread(command.upgrade, config, "0005")

        engine = create_engine(f"sqlite:///{db_path}")
        try:
            columns = {c["name"] for c in inspect(engine).get_columns("debtors")}
            assert "inn" not in columns, "0005 не та ревизия: колонка уже есть"
        finally:
            engine.dispose()

        await _upgrade_schema()

        engine = create_engine(f"sqlite:///{db_path}")
        try:
            columns = {c["name"] for c in inspect(engine).get_columns("debtors")}
            assert "inn" in columns
            with engine.connect() as connection:
                version = connection.execute(
                    text("select version_num from alembic_version")
                ).scalar_one()
            assert version == ScriptDirectory.from_config(config).get_current_head()
        finally:
            engine.dispose()
    finally:
        get_settings.cache_clear()
