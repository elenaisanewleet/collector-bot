"""ФНС — ЕГРЮЛ / ЕГРИП business relations.

Registry data is public, but there is no single canonical free API for it, so
the concrete backend is a deployment choice (``FNS_PROVIDER``). The domain layer
knows only :class:`~app.domain.models.BusinessRelation`; swapping vendors is a
configuration change.

A business relation is a *hint* about ability to pay, never a conclusion. An
active sole proprietorship means the person is registered, not that they earn.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import FNSBackend, Settings
from app.domain.enums import (
    BusinessRole,
    BusinessStatus,
    EntityType,
    ProviderName,
    ProviderStatus,
)
from app.domain.identity import SearchSubject
from app.domain.models import BusinessRelation, ProviderResult
from app.providers.base import BaseProvider
from app.providers.http import RetryPolicy
from app.providers.mapping import as_text
from app.providers.vendor_http import VendorConfig, VendorJsonClient
from app.utils.dates import parse_date, utcnow

_ACTIVE_TOKENS = frozenset({"active", "действует", "действующее", "действующий"})
_TERMINATED_TOKENS = frozenset(
    {"terminated", "liquidated", "прекратил", "прекращено", "ликвидировано", "закрыт"}
)
_ROLE_TOKENS: dict[str, BusinessRole] = {
    "ip": BusinessRole.SOLE_PROPRIETOR,
    "ип": BusinessRole.SOLE_PROPRIETOR,
    "sole_proprietor": BusinessRole.SOLE_PROPRIETOR,
    "director": BusinessRole.DIRECTOR,
    "руководитель": BusinessRole.DIRECTOR,
    "директор": BusinessRole.DIRECTOR,
    "founder": BusinessRole.FOUNDER,
    "учредитель": BusinessRole.FOUNDER,
}
MAX_RECORDS = 50


class FNSProvider(BaseProvider):
    name = ProviderName.FNS
    title = "ФНС"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: VendorJsonClient | None = None

    @property
    def is_configured(self) -> bool:
        return self._settings.fns_configured

    def _vendor_client(self) -> VendorJsonClient:
        if self._client is None:
            self._client = VendorJsonClient(
                VendorConfig(
                    base_url=self._settings.fns_base_url,
                    path=self._settings.fns_search_path,
                    auth_style=self._settings.fns_auth_style,
                    auth_name=self._settings.fns_auth_name,
                    api_key=self._settings.fns_api_key,
                    field_map_path=self._settings.fns_field_map,
                ),
                timeout_seconds=self._settings.request_timeout_seconds,
                retry=RetryPolicy(
                    max_retries=self._settings.provider_max_retries,
                    backoff_seconds=self._settings.provider_retry_backoff_seconds,
                ),
                provider_label=self.name.value,
            )
        return self._client

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if self._settings.fns_provider is not FNSBackend.GENERIC_JSON:
            return self.not_configured("Бэкенд ФНС не настроен")
        if subject.name is None and not subject.inn:
            return self.insufficient_query("Для проверки в ФНС нужно ФИО или ИНН")

        params: dict[str, Any] = {}
        if subject.inn:
            params["inn"] = subject.inn
        if subject.name:
            params["query"] = subject.name.full
        if subject.birth_date:
            params["birth_date"] = subject.birth_date.isoformat()

        records, raw = await self._vendor_client().fetch_records(params)
        parsed = [_to_relation(record) for record in records[:MAX_RECORDS]]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if parsed else ProviderStatus.NO_RESULTS,
            records=list(parsed),
            raw_response=raw if self._settings.store_raw_responses else None,
        )


def _to_relation(record: Mapping[str, Any]) -> BusinessRelation:
    role = _parse_role(record.get("role"))
    return BusinessRelation(
        inn=as_text(record.get("inn")),
        ogrn=as_text(record.get("ogrn")),
        name=as_text(record.get("name")),
        entity_type=(
            EntityType.SOLE_PROPRIETOR
            if role is BusinessRole.SOLE_PROPRIETOR
            else EntityType.LEGAL_ENTITY
        ),
        status=_parse_status(record),
        role=role,
        registration_date=parse_date(as_text(record.get("registration_date"))),
        termination_date=parse_date(as_text(record.get("termination_date"))),
        source_url=as_text(record.get("source_url")),
        fetched_at=utcnow(),
    )


def _parse_role(raw: Any) -> BusinessRole:
    token = (as_text(raw) or "").lower()
    for marker, role in _ROLE_TOKENS.items():
        if marker in token:
            return role
    return BusinessRole.OTHER


def _parse_status(record: Mapping[str, Any]) -> BusinessStatus:
    if as_text(record.get("termination_date")):
        return BusinessStatus.TERMINATED
    token = (as_text(record.get("status")) or "").lower()
    if any(marker in token for marker in _TERMINATED_TOKENS):
        return BusinessStatus.TERMINATED
    if any(marker in token for marker in _ACTIVE_TOKENS):
        return BusinessStatus.ACTIVE
    return BusinessStatus.UNKNOWN
