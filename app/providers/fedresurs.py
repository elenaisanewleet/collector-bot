"""ЕФРСБ / Федресурс — сведения о банкротстве.

The critical rule for this source: *absence of a configured backend is not
absence of a bankruptcy*. When nothing is wired up the provider answers
``NOT_CONFIGURED`` and the report says "не проверено", never "банкротство не
обнаружено". Only a backend that actually answered can produce ``NO_RESULTS``.

Four backends are selectable through ``FEDRESURS_BACKEND``:

``none``          the default — the source is not connected
``demo``          deterministic fixtures, for running without credentials
``generic_json``  a licensed vendor's REST API, described by a field map
``newdb``         the NewDB ``bankrot_person`` method, on the key already
                  configured for ФССП, with its rows described in
                  ``NEWDB_FIELD_MAP``
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.config import FedresursBackend, Settings
from app.domain.enums import (
    BankruptcyStatus,
    EntityType,
    ProviderName,
    ProviderStatus,
)
from app.domain.identity import SearchSubject
from app.domain.models import BankruptcyRecord, ProviderResult
from app.providers.base import BaseProvider
from app.providers.http import RetryPolicy
from app.providers.mapping import as_text
from app.providers.newdb import NewDBMethodProvider, individual_inn, inn_params
from app.providers.vendor_http import VendorConfig, VendorJsonClient
from app.utils.dates import parse_date, utcnow

_ACTIVE_TOKENS = frozenset(
    {"active", "открыто", "введена", "в процедуре", "процедура", "действующее"}
)
_COMPLETED_TOKENS = frozenset(
    {"completed", "closed", "завершено", "завершена", "прекращено", "окончено"}
)
_INDIVIDUAL_TOKENS = frozenset({"individual", "фл", "физическое лицо", "гражданин"})
_SOLE_PROPRIETOR_TOKENS = frozenset({"ip", "ип", "sole_proprietor"})
MAX_RECORDS = 50

# Имя метода расходится с документацией намеренно: страница снимка от 07.02.2026
# озаглавлена ``fedresurs_bankrot`` и шлёт это имя в примере запроса, но живой
# API его отвергает, а в примере ОТВЕТА на той же странице и params.method, и
# секция results названы ``bankrot_person``. Прав код — чинить обратно не надо.
NEWDB_METHOD = "bankrot_person"
FEDRESURS_BANKRUPTCY_HOST = "https://bankrot.fedresurs.ru"


class FedresursProvider(BaseProvider):
    name = ProviderName.FEDRESURS
    title = "ЕФРСБ"

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: VendorJsonClient | None = None

    @property
    def is_configured(self) -> bool:
        return self._settings.fedresurs_configured

    def _vendor_client(self) -> VendorJsonClient:
        if self._client is None:
            self._client = VendorJsonClient(
                VendorConfig(
                    base_url=self._settings.fedresurs_base_url,
                    path=self._settings.fedresurs_search_path,
                    auth_style=self._settings.fedresurs_auth_style,
                    auth_name=self._settings.fedresurs_auth_name,
                    api_key=self._settings.fedresurs_api_key,
                    username=self._settings.fedresurs_username,
                    password=self._settings.fedresurs_password,
                    field_map_path=self._settings.fedresurs_field_map,
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
        if self._settings.fedresurs_backend is not FedresursBackend.GENERIC_JSON:
            # DEMO is served by the mock provider in the registry; reaching here
            # with any other backend means the wiring is incomplete.
            return self.not_configured("Бэкенд ЕФРСБ не настроен")
        if subject.name is None and not subject.inn:
            return self.insufficient_query("Для проверки банкротства нужно ФИО или ИНН")

        params: dict[str, Any] = {}
        if subject.name:
            params["name"] = subject.name.full
        if subject.birth_date:
            params["birth_date"] = subject.birth_date.isoformat()
        if subject.inn:
            params["inn"] = subject.inn

        records, raw = await self._vendor_client().fetch_records(params)
        parsed = [_to_bankruptcy(record) for record in records[:MAX_RECORDS]]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if parsed else ProviderStatus.NO_RESULTS,
            records=list(parsed),
            raw_response=raw if self._settings.store_raw_responses else None,
        )


def _to_bankruptcy(record: Mapping[str, Any]) -> BankruptcyRecord:
    return BankruptcyRecord(
        debtor_name=as_text(record.get("debtor_name")),
        debtor_type=_parse_entity_type(record.get("debtor_type")),
        inn=as_text(record.get("inn")),
        case_number=as_text(record.get("case_number")),
        procedure=as_text(record.get("procedure")),
        status=_parse_status(record),
        started_at=parse_date(as_text(record.get("started_at"))),
        completed_at=parse_date(as_text(record.get("completed_at"))),
        message_date=parse_date(as_text(record.get("message_date"))),
        source_url=as_text(record.get("source_url")),
        fetched_at=utcnow(),
    )


def _parse_entity_type(raw: Any) -> EntityType:
    token = (as_text(raw) or "").lower()
    if token in _SOLE_PROPRIETOR_TOKENS:
        return EntityType.SOLE_PROPRIETOR
    if token in _INDIVIDUAL_TOKENS or not token:
        return EntityType.INDIVIDUAL
    return EntityType.LEGAL_ENTITY


def _parse_status(record: Mapping[str, Any]) -> BankruptcyStatus:
    """A completion date is stronger evidence than a status string."""
    if as_text(record.get("completed_at")):
        return BankruptcyStatus.COMPLETED
    token = (as_text(record.get("status")) or "").lower()
    if any(marker in token for marker in _COMPLETED_TOKENS):
        return BankruptcyStatus.COMPLETED
    if any(marker in token for marker in _ACTIVE_TOKENS):
        return BankruptcyStatus.ACTIVE
    if as_text(record.get("started_at")) or as_text(record.get("procedure")):
        # A procedure that started and has no completion date is running.
        return BankruptcyStatus.ACTIVE
    return BankruptcyStatus.UNKNOWN


class NewDBBankruptcyProvider(NewDBMethodProvider):
    """Банкротство через метод NewDB ``bankrot_person``.

    Same source, same domain record, different carrier: the key is the one
    already paying for ФССП, so connecting bankruptcy costs a field-map entry
    rather than a second vendor contract.

    Unlike ФССП, this method is addressed by ИНН and not by name: the live
    endpoint rejects the person block with ``Отсутствует обязательный параметр:
    innfiz``. A debtor without ИНН — or with a ten-digit one, which is a legal
    entity's — therefore cannot be checked here at all, and saying so is the only
    honest answer: a rejected request reported as a clean register is exactly the
    failure this project exists to avoid.
    """

    name = ProviderName.FEDRESURS
    title = "ЕФРСБ"
    methods = (NEWDB_METHOD,)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        inn = individual_inn(subject)
        if inn is None:
            return self.insufficient_query(
                "Для проверки банкротства нужен ИНН физлица (12 цифр) — "
                "источник ищет только по нему"
            )

        records, raw = await self.rows_for(NEWDB_METHOD, inn_params(inn))
        parsed = [_searched_by_inn(_to_bankruptcy(record), inn) for record in records[:MAX_RECORDS]]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if parsed else ProviderStatus.NO_RESULTS,
            records=list(parsed),
            raw_response=self.raw_for(raw),
        )


def _searched_by_inn(record: BankruptcyRecord, inn: str) -> BankruptcyRecord:
    """Carry the ИНН we searched by into a record that has none of its own.

    The method's rows are cases, and a case carries a number and a status but
    not the debtor: the identity sits in a sibling block of the response that a
    flat field map cannot reach. Without this the record would arrive with no
    identifiers at all, the matcher would rate it a weak match, and a real
    bankruptcy found by the debtor's own ИНН would be dropped from the report as
    somebody else's.

    Claiming the ИНН is not a guess: it is the parameter the search was made
    with, and the source answers about that person. The debtor's *name* is not
    filled in for the same reason in reverse — nothing in the row asserts it.
    """
    if record.inn is None:
        record.inn = inn
    if record.source_url:
        record.source_url = _absolute_url(record.source_url)
    return record


def _absolute_url(url: str) -> str:
    """``/legalcases/<guid>`` -> a link that can actually be clicked.

    The vendor returns Федресурс paths, not URLs. The host is inferred from the
    path shape and is not verified; a wrong host and a bare path are equally
    broken, and a working link is worth the inference.
    """
    return f"{FEDRESURS_BANKRUPTCY_HOST}{url}" if url.startswith("/") else url
