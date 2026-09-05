"""Vendor-agnostic JSON adapter.

ЕФРСБ and ЕГРЮЛ/ЕГРИП data is reachable through several licensed API vendors,
each with its own request shape and its own field names. Rather than hard-coding
one vendor's schema — which would be a guess dressed up as an integration — the
adapter takes the endpoint, the auth style and a field map from configuration.

Supply a field map (a small JSON file) and the adapter works against your
vendor. Supply nothing and the provider stays ``NOT_CONFIGURED``.
"""

from __future__ import annotations

import base64
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import AuthStyle
from app.providers.base import ProviderUnavailableError
from app.providers.http import RetryPolicy, build_client, request_json
from app.providers.mapping import FieldMap, RecordDict


@dataclass(frozen=True, slots=True)
class VendorConfig:
    """Everything needed to call one vendor endpoint."""

    base_url: str
    path: str
    auth_style: AuthStyle = AuthStyle.NONE
    auth_name: str = ""
    api_key: str = ""
    username: str = ""
    password: str = ""
    method: str = "GET"
    field_map_path: Path | None = None

    @property
    def is_usable(self) -> bool:
        return bool(self.base_url and self.path and self.field_map_path is not None)


class VendorJsonClient:
    """Calls a configured endpoint and maps the response onto flat dicts."""

    def __init__(
        self,
        config: VendorConfig,
        *,
        timeout_seconds: float,
        retry: RetryPolicy,
        provider_label: str,
    ) -> None:
        self._config = config
        self._timeout = timeout_seconds
        self._retry = retry
        self._label = provider_label
        self._field_map: FieldMap | None = None

    def _load_field_map(self) -> FieldMap:
        if self._field_map is None:
            assert self._config.field_map_path is not None  # guarded by is_usable
            self._field_map = FieldMap.from_file(self._config.field_map_path)
        return self._field_map

    async def fetch_records(self, params: Mapping[str, Any]) -> tuple[list[RecordDict], str]:
        field_map = self._load_field_map()
        headers = self._auth_headers()
        query = dict(params)
        query.update(self._auth_query())

        async with build_client(
            base_url=self._config.base_url,
            timeout_seconds=self._timeout,
            headers=headers,
        ) as client:
            payload, raw = await request_json(
                client,
                self._config.method,
                self._config.path,
                params=query if self._config.method.upper() == "GET" else None,
                json_body=query if self._config.method.upper() != "GET" else None,
                retry=self._retry,
                provider=self._label,
            )
        records, unreadable = field_map.read_all(payload)
        if unreadable:
            # Тот же счёт потерь, что у методов NewDB. Элемент массива записей,
            # который записью не является, раньше отбрасывался фильтром внутри
            # карты: ответ из двух таких элементов приходил к провайдеру как
            # пустой список и печатался как «источник проверен, ничего нет».
            raise ProviderUnavailableError(
                "unexpected_schema",
                f"Карта полей не разобрала {unreadable} из "
                f"{unreadable + len(records)} записей ответа источника",
            )
        return records, raw

    def _auth_headers(self) -> dict[str, str]:
        style = self._config.auth_style
        if style is AuthStyle.BEARER and self._config.api_key:
            return {"Authorization": f"Bearer {self._config.api_key}"}
        if style is AuthStyle.HEADER and self._config.api_key:
            return {self._config.auth_name or "X-Api-Key": self._config.api_key}
        if style is AuthStyle.BASIC and self._config.username:
            token = base64.b64encode(
                f"{self._config.username}:{self._config.password}".encode()
            ).decode()
            return {"Authorization": f"Basic {token}"}
        return {}

    def _auth_query(self) -> dict[str, str]:
        if self._config.auth_style is AuthStyle.QUERY and self._config.api_key:
            return {self._config.auth_name or "key": self._config.api_key}
        return {}
