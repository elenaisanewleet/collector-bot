"""ФНС — ЕГРЮЛ / ЕГРИП business relations.

Registry data is public, but there is no single canonical free API for it, so
the concrete backend is a deployment choice (``FNS_PROVIDER``). The domain layer
knows only :class:`~app.domain.models.BusinessRelation`; swapping vendors is a
configuration change.

A business relation is a *hint* about ability to pay, never a conclusion. An
active sole proprietorship means the person is registered, not that they earn.

``FNS_PROVIDER=newdb`` serves ЕГРИП **and roles in legal entities** through the
NewDB ``egrul_ip`` method, on the key already configured for ФССП. The archived
documentation described this method in prose without a single key name, so it
used to be left out of the field map. A live response has since been read, and
the shape it turned out to have is one no flat map could express anyway:

``data[0].matches[]``            строки по самому человеку: ``ip`` — его ИП,
                                 ``upr`` и ``uchr`` — он сам как руководитель и
                                 как учредитель;
``data[0].affiliations.companies[]``  сами компании, с ИНН, статусом и ролями;
``data[0].affiliations.lookups[]``    как эти компании искались, включая ошибки.

Hence a hard-coded parser rather than a map entry, and a trap worth naming: the
``upr`` and ``uchr`` rows of ``matches`` carry **the debtor's own ИНН and the
debtor's own ФИО**. Turned into business relations they become "companies"
named after the person, which the identity matcher then confirms with a full
name match — a company in the report that does not exist. Only ``section: "ip"``
rows are relations here; the roles in legal entities come from the companies
themselves.

``bankruptcy_flag`` arrives in the same paid answer and is not decoration: a
company in bankruptcy with the debtor as its director is where subsidiary
liability gets asked about.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
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
from app.providers.mapping import as_text, dig
from app.providers.newdb import NewDBMethodProvider, individual_inn, inn_params
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

NEWDB_METHOD = "egrul_ip"


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


class NewDBBusinessProvider(NewDBMethodProvider):
    """ИП и роли в юрлицах через метод NewDB ``egrul_ip``."""

    name = ProviderName.FNS
    title = "ФНС"
    methods = (NEWDB_METHOD,)

    @property
    def is_configured(self) -> bool:
        # Карта не нужна: строки разбираются кодом по живому ответу.
        return self._settings.newdb_configured

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        inn = individual_inn(subject)
        if inn is None:
            # Раньше здесь был откат на ФИО с датой рождения. Живой сервис его
            # не принимает — ``Отсутствует обязательный параметр: innfiz``, —
            # так что откат давал бы не запасной путь, а отклонённый запрос.
            # Десятизначный ИНН отвергается там же: ``innfiz`` — двенадцать цифр.
            return self.insufficient_query(
                "Для проверки ИП нужен ИНН физлица (12 цифр) — источник ищет только по нему"
            )

        rows, raw = await self.raw_rows_for(NEWDB_METHOD, inn_params(inn))
        parsed = _parse_egrul(rows)[:MAX_RECORDS]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if parsed else ProviderStatus.NO_RESULTS,
            records=list(parsed),
            notes=_coverage_notes(rows),
            raw_response=self.raw_for(raw),
        )

    def planned_calls(self, subject: SearchSubject) -> int:
        if not self.is_configured or individual_inn(subject) is None:
            return 0
        return 1


# ---------------------------------------------------------------- egrul_ip


SOLE_PROPRIETOR_SECTION = "ip"
_COMPANY_ROLE_TOKENS: dict[str, BusinessRole] = {
    "director": BusinessRole.DIRECTOR,
    "upr": BusinessRole.DIRECTOR,
    "founder": BusinessRole.FOUNDER,
    "uchr": BusinessRole.FOUNDER,
}


def _parse_egrul(rows: Sequence[Any]) -> list[BusinessRelation]:
    """Sole proprietorships from ``matches``, companies from ``affiliations``."""
    relations: list[BusinessRelation] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for entry in _mappings(dig(row, "matches")):
            if as_text(entry.get("section")) != SOLE_PROPRIETOR_SECTION:
                # ``upr`` и ``uchr`` — это сам должник, с его ИНН и его ФИО.
                # Превращённые в «юрлицо» они дают компанию, которой нет.
                continue
            relations.append(_to_sole_proprietor(entry))
        for entry in _mappings(dig(row, "affiliations.companies")):
            relation = _to_company(entry)
            key = (relation.inn or "", relation.role.value)
            if key in seen:
                continue
            seen.add(key)
            relations.append(relation)
    return relations


def _to_sole_proprietor(entry: Mapping[str, Any]) -> BusinessRelation:
    return BusinessRelation(
        inn=as_text(entry.get("inn")),
        ogrn=as_text(entry.get("ogrn")),
        name=as_text(entry.get("name_short")) or as_text(entry.get("name_full")),
        entity_type=EntityType.SOLE_PROPRIETOR,
        role=BusinessRole.SOLE_PROPRIETOR,
        status=_registry_status(entry),
        registration_date=parse_date(
            as_text(entry.get("registration_date")) or as_text(entry.get("ogrn_date"))
        ),
        bankruptcy_flag=entry.get("bankruptcy_flag") is True,
        source_url=as_text(dig(entry, "links.gosreg")),
        fetched_at=utcnow(),
    )


def _to_company(entry: Mapping[str, Any]) -> BusinessRelation:
    return BusinessRelation(
        inn=as_text(entry.get("inn")),
        ogrn=as_text(entry.get("ogrn")),
        name=as_text(entry.get("name_short")) or as_text(entry.get("name_full")),
        entity_type=EntityType.LEGAL_ENTITY,
        role=_company_role(entry.get("roles")),
        status=_registry_status(entry),
        registration_date=parse_date(
            as_text(entry.get("registration_date")) or as_text(entry.get("ogrn_date"))
        ),
        bankruptcy_flag=entry.get("bankruptcy_flag") is True,
        # Поиск шёл по ИНН должника, и компания пришла из его же аффилиаций.
        linked_by_identifier=True,
        source_url=as_text(dig(entry, "links.business_card")),
        fetched_at=utcnow(),
    )


def _company_role(raw: Any) -> BusinessRole:
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        tokens = [as_text(item) or "" for item in raw]
    else:
        tokens = [as_text(raw) or ""]
    for token in tokens:
        role = _COMPANY_ROLE_TOKENS.get(token.strip().lower())
        if role is not None:
            return role
    return BusinessRole.OTHER


def _registry_status(entry: Mapping[str, Any]) -> BusinessStatus:
    """``status`` приходит только у компаний; у строк ``matches`` он ``null``.

    Поэтому в ход идут флаги, а при их отсутствии — ``UNKNOWN``, а не
    ``ACTIVE``: непрочитанный статус, поданный как действующий, — это выдуманный
    признак платёжеспособности.
    """
    if entry.get("liquidation_flag") is True or entry.get("invalid_flag") is True:
        return BusinessStatus.TERMINATED
    return _parse_status({"status": entry.get("status")})


def _coverage_notes(rows: Sequence[Any]) -> tuple[str, ...]:
    """Оговорки о полноте: «показаны N из M» и «часть веток не загрузилась».

    Живой ответ показал ровно этот случай: ветка учредителя вернула ошибку
    таймаута, компании по ней не пришли, а без оговорки список читался бы как
    «все компании должника».
    """
    notes: list[str] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        for code, section in _sections(dig(row, "affiliations.registry_sections")):
            if section.get("has_more") is True:
                label = as_text(section.get("label")) or code
                shown = section.get("row_count")
                total = section.get("items_count")
                notes.append(f"{label}: показаны {shown} из {total} — источник отдал не всё.")
        for lookup in _mappings(dig(row, "affiliations.lookups")):
            if as_text(lookup.get("error")) is None:
                continue
            role = as_text(dig(lookup, "person.role")) or "связь"
            notes.append(
                f"Ветка «{role}» не загрузилась у источника — список компаний неполон."
            )
    return tuple(dict.fromkeys(notes))


def _sections(node: Any) -> list[tuple[str, Mapping[str, Any]]]:
    if not isinstance(node, Mapping):
        return []
    return [(str(key), value) for key, value in node.items() if isinstance(value, Mapping)]


def _mappings(node: Any) -> list[Mapping[str, Any]]:
    if not isinstance(node, Sequence) or isinstance(node, (str, bytes)):
        return []
    return [item for item in node if isinstance(item, Mapping)]
