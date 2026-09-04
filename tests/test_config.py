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


def test_fssp_needs_both_token_and_url() -> None:
    assert not make_settings(fssp_api_token="t").fssp_configured
    assert not make_settings(fssp_base_url="https://x.test").fssp_configured
    assert make_settings(fssp_api_token="t", fssp_base_url="https://x.test").fssp_configured


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
    assert make_settings(fssp_base_url="https://x.test/").fssp_base_url == "https://x.test"


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
