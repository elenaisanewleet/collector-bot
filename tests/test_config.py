"""Configuration semantics.

The property under test throughout: nothing is "configured" by accident.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.config import AppMode, FedresursBackend, FNSBackend, Settings


def make_settings(**overrides: Any) -> Settings:
    """Settings built in isolation from any on-disk ``.env``."""
    return Settings(_env_file=None, **overrides)


def test_defaults_are_safe() -> None:
    settings = make_settings()
    assert settings.app_mode is AppMode.DEMO
    assert settings.store_raw_responses is False
    assert settings.store_sensitive_identifiers is False
    assert settings.allowed_user_ids == frozenset()
    assert not settings.fssp_configured
    assert not settings.fedresurs_configured
    assert not settings.fns_configured


def test_fssp_needs_a_newdb_key() -> None:
    """ФССП is served by NEWDB now; the base URL defaults, so only the key is
    genuinely missing out of the box."""
    assert not make_settings().fssp_configured
    assert not make_settings(newdb_base_url="https://x.test").fssp_configured
    assert make_settings(newdb_api_key="k").fssp_configured
    assert not make_settings(newdb_api_key="k", newdb_base_url="").fssp_configured


def test_newdb_has_working_defaults() -> None:
    assert make_settings().newdb_base_url == "https://api.newdb.net"
    assert make_settings().newdb_method_path == "/v2"


def test_fedresurs_needs_a_field_map(tmp_path: Path) -> None:
    """Without a field map the vendor response cannot be parsed, so the
    provider is not considered configured."""
    base = {
        "fedresurs_backend": FedresursBackend.GENERIC_JSON,
        "fedresurs_base_url": "https://x.test",
        "fedresurs_search_path": "/search",
        "fedresurs_api_key": "key",
    }
    assert not make_settings(**base).fedresurs_configured
    assert make_settings(**base, fedresurs_field_map=tmp_path / "m.json").fedresurs_configured


def test_fns_needs_a_field_map(tmp_path: Path) -> None:
    base = {
        "fns_provider": FNSBackend.GENERIC_JSON,
        "fns_base_url": "https://x.test",
        "fns_search_path": "/search",
        "fns_api_key": "key",
    }
    assert not make_settings(**base).fns_configured
    assert make_settings(**base, fns_field_map=tmp_path / "m.json").fns_configured


def test_demo_backends_are_configured_without_credentials() -> None:
    assert make_settings(fedresurs_backend=FedresursBackend.DEMO).fedresurs_configured
    assert make_settings(fns_provider=FNSBackend.DEMO).fns_configured


def test_base_urls_lose_their_trailing_slash() -> None:
    assert make_settings(newdb_base_url="https://x.test/").newdb_base_url == "https://x.test"


def test_cache_can_be_disabled() -> None:
    assert not make_settings(cache_ttl_hours=0).cache_enabled
    assert make_settings(cache_ttl_hours=1).cache_enabled


@pytest.mark.parametrize("value", ["debug", "Debug", "DEBUG"])
def test_log_level_is_normalized(value: str) -> None:
    assert make_settings(log_level=value).log_level == "DEBUG"


def test_out_of_range_values_are_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_settings(provider_concurrency=0)
    with pytest.raises(ValidationError):
        make_settings(request_timeout_seconds=0)


def test_poll_that_outlasts_the_budget_is_rejected() -> None:
    """Опрос, не влезающий в бюджет источника, не должен молча доехать до прода.

    Ровно эта комбинация стояла в бою: 30 × 3 с = 90 с при бюджете 90 с. ФССП
    обрывалась на последней попытке, вызов был оплачен, а в отчёте значилось
    «источник не ответил» — и вердикт уходил в «проверить руками» по причине,
    которой у должника не было.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="бюджет источника"):
        make_settings(
            newdb_poll_attempts=30,
            newdb_poll_interval_seconds=3.0,
            provider_budget_seconds=90.0,
        )


def test_budget_must_cover_the_http_request_on_top_of_polling() -> None:
    """Запас нужен и на сами обращения, а не только на паузы между ними.

    Интервал — это сон между попытками; каждая попытка вдобавок ждёт ответа до
    request_timeout_seconds. Бюджет, равный сумме одних пауз, обрывает источник
    на последнем запросе.
    """
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        make_settings(
            newdb_poll_attempts=10,
            newdb_poll_interval_seconds=2.0,
            request_timeout_seconds=15.0,
            provider_budget_seconds=20.0,
        )

    settings = make_settings(
        newdb_poll_attempts=10,
        newdb_poll_interval_seconds=2.0,
        request_timeout_seconds=15.0,
        provider_budget_seconds=40.0,
    )
    assert settings.provider_budget_seconds == 40.0


def test_shipped_defaults_leave_room_for_a_slow_source() -> None:
    """Значения по умолчанию обязаны переживать самый медленный живой источник.

    Замеры: rosreestr — 49 с, arbitr_legal — 59 с. Если умолчания перестанут их
    покрывать, ошибётся не конфигуратор, а каждый, кто ничего не настраивал.
    """
    settings = make_settings()
    poll_seconds = settings.newdb_poll_attempts * settings.newdb_poll_interval_seconds
    assert poll_seconds >= 59.0
    assert poll_seconds + settings.request_timeout_seconds <= settings.provider_budget_seconds
