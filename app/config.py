"""Application settings.

Everything that varies between deployments — credentials, endpoints, retention
policy — arrives through the environment. Nothing here has a value that would
make the application talk to a real external system by accident: every provider
stays *not configured* until its credentials are supplied.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppMode(StrEnum):
    """Which provider set the registry assembles.

    ``demo`` wires deterministic in-process providers so the whole pipeline can
    be exercised without credentials. ``live`` wires the real HTTP adapters,
    each of which reports ``NOT_CONFIGURED`` until it has what it needs.
    """

    DEMO = "demo"
    LIVE = "live"


class FNSBackend(StrEnum):
    """Which backend answers ЕГРЮЛ/ЕГРИП lookups.

    The registry data is public, but there is no single canonical free API, so
    the concrete vendor is a deployment choice rather than a code-level one.
    """

    NONE = "none"
    DEMO = "demo"
    GENERIC_JSON = "generic_json"


class FedresursBackend(StrEnum):
    NONE = "none"
    DEMO = "demo"
    GENERIC_JSON = "generic_json"


class AuthStyle(StrEnum):
    """How a vendor expects credentials to be presented.

    Vendors differ; making this configuration rather than code means a new
    vendor needs an ``.env`` change, not an adapter.
    """

    NONE = "none"
    BEARER = "bearer"
    HEADER = "header"
    QUERY = "query"
    BASIC = "basic"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---------------------------------------------------------------- app
    app_env: str = "development"
    app_mode: AppMode = AppMode.DEMO
    app_name: str = "Collector Bot"
    log_level: str = "INFO"
    log_json: bool = False

    # ---------------------------------------------------------------- telegram
    telegram_bot_token: str = ""
    allowed_telegram_user_ids: str = ""

    # ---------------------------------------------------------------- storage
    database_url: str = "sqlite+aiosqlite:///./collector_bot.db"
    internal_csv_path: Path = Path("./data/demo_debtors.csv")

    # ---------------------------------------------------------------- ФССП
    fssp_api_token: str = ""
    fssp_base_url: str = ""
    fssp_search_path: str = "/api/v1.0/search/physical"
    fssp_status_path: str = "/api/v1.0/status"
    fssp_result_path: str = "/api/v1.0/result"
    fssp_poll_attempts: Annotated[int, Field(ge=1, le=60)] = 10
    fssp_poll_interval_seconds: Annotated[float, Field(ge=0.1, le=30)] = 2.0

    # ---------------------------------------------------------------- ЕФРСБ
    fedresurs_backend: FedresursBackend = FedresursBackend.NONE
    fedresurs_base_url: str = ""
    fedresurs_username: str = ""
    fedresurs_password: str = ""
    fedresurs_api_key: str = ""
    fedresurs_search_path: str = ""
    fedresurs_auth_style: AuthStyle = AuthStyle.BEARER
    fedresurs_auth_name: str = "X-Api-Key"
    # Maps the vendor's JSON onto BankruptcyRecord. Required for a live backend:
    # no vendor schema is assumed.
    fedresurs_field_map: Path | None = None

    # ---------------------------------------------------------------- ФНС
    fns_provider: FNSBackend = FNSBackend.NONE
    fns_base_url: str = ""
    fns_api_key: str = ""
    fns_search_path: str = ""
    fns_auth_style: AuthStyle = AuthStyle.QUERY
    fns_auth_name: str = "key"
    fns_field_map: Path | None = None

    # ---------------------------------------------------------------- http
    request_timeout_seconds: Annotated[float, Field(ge=1, le=120)] = 15.0
    provider_concurrency: Annotated[int, Field(ge=1, le=32)] = 5
    provider_max_retries: Annotated[int, Field(ge=0, le=5)] = 2
    provider_retry_backoff_seconds: Annotated[float, Field(ge=0.0, le=10)] = 0.5

    # ---------------------------------------------------------------- cache
    cache_ttl_hours: Annotated[int, Field(ge=0, le=24 * 30)] = 24

    # ---------------------------------------------------------------- privacy
    store_raw_responses: bool = False
    store_sensitive_identifiers: bool = False

    # ---------------------------------------------------------------- import
    max_import_file_bytes: Annotated[int, Field(ge=1024)] = 5 * 1024 * 1024
    max_import_rows: Annotated[int, Field(ge=1)] = 50_000

    @field_validator("log_level")
    @classmethod
    def _upper_log_level(cls, value: str) -> str:
        return value.strip().upper() or "INFO"

    @field_validator(
        "fssp_base_url",
        "fedresurs_base_url",
        "fns_base_url",
    )
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @property
    def allowed_user_ids(self) -> frozenset[int]:
        """Parsed allowlist.

        Malformed entries are dropped rather than crashing the bot, but an empty
        result means *nobody* is allowed — the closed bot fails shut.
        """
        ids: set[int] = set()
        for chunk in self.allowed_telegram_user_ids.replace(";", ",").split(","):
            token = chunk.strip()
            if not token:
                continue
            try:
                ids.add(int(token))
            except ValueError:
                continue
        return frozenset(ids)

    @property
    def is_demo(self) -> bool:
        return self.app_mode is AppMode.DEMO

    @property
    def cache_enabled(self) -> bool:
        return self.cache_ttl_hours > 0

    @property
    def fssp_configured(self) -> bool:
        return bool(self.fssp_api_token and self.fssp_base_url)

    @property
    def fedresurs_configured(self) -> bool:
        if self.fedresurs_backend is FedresursBackend.NONE:
            return False
        if self.fedresurs_backend is FedresursBackend.DEMO:
            return True
        has_auth = bool(
            self.fedresurs_api_key or (self.fedresurs_username and self.fedresurs_password)
        )
        return bool(
            self.fedresurs_base_url
            and self.fedresurs_search_path
            and has_auth
            and self.fedresurs_field_map is not None
        )

    @property
    def fns_configured(self) -> bool:
        if self.fns_provider is FNSBackend.NONE:
            return False
        if self.fns_provider is FNSBackend.DEMO:
            return True
        return bool(
            self.fns_base_url
            and self.fns_search_path
            and self.fns_api_key
            and self.fns_field_map is not None
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton.

    Cached so that importing modules do not each re-read the environment; tests
    clear the cache or construct :class:`Settings` directly.
    """
    return Settings()


def reset_settings_cache() -> None:
    get_settings.cache_clear()
