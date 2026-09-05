"""The demo runner.

``make demo`` is the project's smoke test; if this passes, the whole pipeline
runs without credentials.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.config import AppMode, Settings
from app.db.session import Database
from app.demo import run_demo_flow
from app.providers.internal.onec import OneCODataProvider


@pytest.fixture
def demo_settings(tmp_path: Path, settings: Settings) -> Settings:
    return settings.model_copy(
        update={"database_url": f"sqlite+aiosqlite:///{tmp_path / 'demo.db'}"}
    )


async def test_demo_runs_without_credentials(
    demo_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    exit_code = await run_demo_flow(demo_settings)
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "RECOVERY SCORE" in output
    assert "ВЫСОКАЯ" in output
    assert "СРЕДНЯЯ" in output
    assert "НИЗКАЯ" in output
    assert "ДЕМО-РЕЖИМ" in output


async def test_demo_refuses_to_run_against_live_providers(
    demo_settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """Asked to demo in live mode, the runner switches to demo rather than
    fabricating output from real adapters."""
    await run_demo_flow(demo_settings.model_copy(update={"app_mode": AppMode.LIVE}))
    assert "ДЕМО-РЕЖИМ" in capsys.readouterr().out


def _onec_ready(settings: Settings, tmp_path: Path) -> Settings:
    """Настройки, при которых 1С подключилась бы: адрес, доступ и карта."""
    field_map = tmp_path / "onec.json"
    field_map.write_text(
        json.dumps(
            {
                "by_contract": {
                    "collection": "Catalog_ПРИМЕР_Должники",
                    "filter_template": "ПРИМЕР_НомерДоговора eq {value}",
                    "fields": {"full_name": "ПРИМЕР_ФИО"},
                }
            }
        ),
        encoding="utf-8",
    )
    return settings.model_copy(
        update={
            "app_mode": AppMode.LIVE,
            "onec_base_url": "https://1c.example.test/base",
            "onec_username": "reader",
            "onec_password": SecretStr("secret"),
            "onec_field_map": field_map,
        }
    )


def _has_onec(settings: Settings, database: Database) -> bool:
    from app.providers.registry import build_internal_provider

    provider = build_internal_provider(settings, database)
    return any(isinstance(source, OneCODataProvider) for source in provider.sources)


def test_onec_is_not_wired_without_credentials(settings: Settings, database: Database) -> None:
    """Нет адреса, доступа или карты — источника нет вовсе.

    Полумера здесь опаснее отсутствия: провайдер, который не может ни построить
    ``$filter``, ни прочитать ответ, отвечал бы «совпадений нет» — то есть
    выдавал бы неподключённую базу за проверенную и пустую.
    """
    live = settings.model_copy(update={"app_mode": AppMode.LIVE})
    assert not _has_onec(live, database)


def test_onec_is_not_queried_in_demo_mode(
    settings: Settings, database: Database, tmp_path: Path
) -> None:
    """Демо-режим не ходит в боевую базу заказчика, даже когда может.

    Гейт по режиму — не украшение. Внешние источники его смотрят, а сборка
    внутренних не смотрела: ``make demo`` на машине с заполненным ``.env``
    отправил бы живые запросы в 1С заказчика, печатая при этом «данные
    вымышленные». Ошибка тихая — в выводе она никак не видна.
    """
    ready = _onec_ready(settings, tmp_path)
    assert _has_onec(ready, database), "выборка теряет смысл: 1С и так не подключилась бы"

    assert not _has_onec(ready.model_copy(update={"app_mode": AppMode.DEMO}), database)


def test_registry_reports_which_providers_are_live(settings: Settings) -> None:
    from app.db.session import Database
    from app.providers.registry import build_registry

    registry = build_registry(settings, Database("sqlite+aiosqlite:///:memory:"))
    names = {name.value for name in registry.configured_names}

    # Demo mode wires every demo source; everything else stays unconnected.
    assert names == {"fssp", "fedresurs", "fns", "pledge", "court"}
