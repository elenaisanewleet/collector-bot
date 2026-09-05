"""ФНС — ЕГРЮЛ / ЕГРИП business relations.

Registry data is public, but there is no single canonical free API for it, so
the concrete backend is a deployment choice (``FNS_PROVIDER``). The domain layer
knows only :class:`~app.domain.models.BusinessRelation`; swapping vendors is a
configuration change.

A business relation is a *hint* about ability to pay, never a conclusion. An
active sole proprietorship means the person is registered, not that they earn.

``FNS_PROVIDER=newdb`` serves this through the NewDB ``egrul_ip`` method on the
key already configured for ФССП.

**Что этот метод отдаёт на самом деле.** Его имя и архивная документация
обещают ЕГРИП и только его — «ролей в юрлицах не возвращает». Живой ответ от
05.09.2026 это опровергает: ``matches[]`` — плоское объединение регистраций ИП
и всех разделов реестра ФНС, и в нём приходят ``section: "upr"`` (руководитель
ЮЛ) и ``section: "uchr"`` (учредитель) наравне с ``section: "ip"``. См.
``tests/data/newdb_live_egrul_ip.json``: три записи на одного человека — ИП,
руководитель и учредитель.

Чего он всё равно не отдаёт в ``matches[]`` — того, к какому юрлицу относится
роль: в строке ``upr``/``uchr`` стоят ИНН и ФИО ЧЕЛОВЕКА, а ОГРН пустой.
Название и ИНН компании живут отдельно, в ``affiliations.companies[]``, и один
``records_path`` на метод их не достаёт. Поэтому строка отчёта «руководитель ЮЛ:
Иванов И.И.» называет человека, а не компанию, и отсутствие компании в отчёте —
предел механизма карты, а не ответ источника.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from app.config import FNSBackend, Settings
from app.domain.enums import (
    BusinessRole,
    BusinessStatus,
    EntityType,
    MissingInput,
    ProviderName,
    ProviderStatus,
)
from app.domain.identity import SearchSubject
from app.domain.models import BusinessRelation, ProviderResult
from app.providers.base import BaseProvider
from app.providers.http import RetryPolicy
from app.providers.mapping import as_text
from app.providers.newdb import (
    MappedRows,
    NewDBMethodProvider,
    container_flag,
    container_int,
    individual_inn,
    inn_params,
)
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

    def missing_input_for(self, subject: SearchSubject) -> tuple[MissingInput, ...]:
        return () if (subject.name is not None or subject.inn) else (MissingInput.NAME,)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if self._settings.fns_provider is not FNSBackend.GENERIC_JSON:
            return self.not_configured("Бэкенд ФНС не настроен")
        missing = self.missing_input_for(subject)
        if missing:
            return self.insufficient_query("Для проверки в ФНС нужно ФИО или ИНН", missing=missing)

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
        # Заполняется только там, где строка источника описывает человека, а не
        # компанию. У ``egrul_ip`` это все живые секции: ip, docip, upr, uchr —
        # в ``name_short`` там ФИО. Без этого поля сопоставление шло вслепую:
        # ``_strip_business_prefix`` отдаёт ФИО лишь у имён с приставкой «ИП »,
        # а живое ``name_short`` приходит голым, и полное совпадение ФИО
        # выбрасывалось — запись держалась на одном ИНН.
        person_name=as_text(record.get("person_name")),
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
    """Регистрации ИП и роли в юрлицах через метод NewDB ``egrul_ip``."""

    name = ProviderName.FNS
    title = "ФНС"
    methods = (NEWDB_METHOD,)

    def missing_input_for(self, subject: SearchSubject) -> tuple[MissingInput, ...]:
        return () if individual_inn(subject) else (MissingInput.INN,)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        inn = individual_inn(subject)
        if inn is None:
            # Раньше здесь был откат на ФИО с датой рождения. Живой сервис его
            # не принимает — ``Отсутствует обязательный параметр: innfiz``, —
            # так что откат давал бы не запасной путь, а отклонённый запрос.
            # Десятизначный ИНН отвергается там же: ``innfiz`` — двенадцать цифр.
            return self.insufficient_query(
                "Для проверки ИП нужен ИНН физлица (12 цифр) — источник ищет только по нему",
                missing=self.missing_input_for(subject),
            )

        mapped, raw = await self.mapped_for(NEWDB_METHOD, inn_params(inn))
        parsed = _dedupe(_to_relation(record) for record in mapped.records[:MAX_RECORDS])
        notes = _truncation_notes(mapped, shown=len(mapped.records))
        if len(mapped.records) > MAX_RECORDS:
            # Срез — тоже потеря, и тихой она быть не должна.
            notes.append(
                f"Показаны первые {MAX_RECORDS} связей из {len(mapped.records)}, "
                "полученных от источника"
            )
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if parsed else ProviderStatus.NO_RESULTS,
            records=list(parsed),
            is_partial=bool(notes),
            notes=tuple(notes),
            raw_response=self.raw_for(raw),
        )


def _truncation_notes(mapped: MappedRows, *, shown: int) -> list[str]:
    """Сказал ли источник, что нашёл больше, чем прислал.

    ``total_items`` живого ответа считает записи ``matches[]``, а ``has_more``
    стоит у каждого раздела реестра. На всех живых ответах они сходятся
    (3 = 3, has_more везде false), но поля существуют — а укороченный
    ``matches[]`` от полного ничем не отличается, и недостающая роль просто не
    появится в отчёте.
    """
    notes: list[str] = []
    total = container_int(mapped.containers, "total_items")
    if total is not None and total > shown:
        notes.append(
            f"ФНС сообщила о {total} записях реестра, разобрано {shown} — список ролей неполный"
        )
    if container_flag(mapped.containers, "has_more"):
        notes.append("ФНС отдала не все записи реестра (has_more) — список ролей неполный")
    return notes


def _dedupe(relations: Iterable[BusinessRelation]) -> list[BusinessRelation]:
    """Одна регистрация, названная дважды, — это одна регистрация.

    ``matches[]`` — плоское объединение регистраций ИП и разделов реестра, и
    одно и то же ОГРНИП приходит в нём и строкой ``ip``, и строкой ``docip``
    («документы на государственную регистрацию ИП»). Проверено на живом ответе:
    ``total_items: 3`` = две регистрации ИП + её же документ. Без склейки отчёт
    печатает два одинаковых ИП, а скоринг считает их за две связи.
    """
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[BusinessRelation] = []
    for relation in relations:
        key = (
            relation.role.value,
            (relation.ogrn or "").strip(),
            (relation.inn or "").strip(),
            (relation.name or "").strip().casefold(),
        )
        if any(part for part in key[1:]) and key in seen:
            continue
        seen.add(key)
        unique.append(relation)
    return unique
