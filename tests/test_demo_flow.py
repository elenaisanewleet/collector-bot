"""The demo runner.

``make demo`` is the project's smoke test; if this passes, the whole pipeline
runs without credentials.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import AppMode, Settings
from app.demo import run_demo_flow
from app.providers.internal.onec_future import OneCODataProvider


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


def test_onec_provider_is_a_placeholder_and_cannot_be_used() -> None:
    """1С is an extension point, not an implementation: constructing it fails
    loudly rather than pretending to connect."""
    with pytest.raises(NotImplementedError) as exc_info:
        OneCODataProvider()
    assert "not currently available" in str(exc_info.value).lower() or "placeholder" in str(
        exc_info.value
    )


def test_onec_provider_is_not_wired_into_the_registry(settings: Settings) -> None:
    from app.providers.registry import build_external_providers

    for provider in build_external_providers(settings):
        assert not isinstance(provider, OneCODataProvider)


def test_registry_reports_which_providers_are_live(settings: Settings) -> None:
    from app.db.session import Database
    from app.providers.registry import build_registry

    registry = build_registry(settings, Database("sqlite+aiosqlite:///:memory:"))
    names = {name.value for name in registry.configured_names}

    # Demo mode wires the three demo sources; everything else stays unconnected.
    assert names == {"fssp", "fedresurs", "fns"}
