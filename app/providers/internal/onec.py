"""Внутренний источник «1С» поверх стандартного OData-интерфейса платформы.

Занимает место, которое было для него оставлено: реализует
:class:`~app.providers.internal.base.InternalDebtorProvider` и встаёт в
composite рядом с CSV и базой. Выше по стеку ничего не меняется — SearchService
по-прежнему видит только интерфейс.

Разделительная линия проходит внутри этого пакета:

*   :mod:`app.providers.onec.client` — протокол. Опубликован фирмой «1С»,
    одинаков у всех конфигураций, проверяется тестами без сети.
*   :mod:`app.providers.onec.lookup_map` — имена. Справочники, реквизиты,
    предикаты. Их у нас нет, они приходят из ``ONEC_FIELD_MAP``.

Поэтому поиск, не описанный в карте, не возвращает пустой список, а поднимает
``ProviderNotConfiguredError``: «мы туда не ходили» и «там никого нет» — разные
утверждения, и второе из них про должника.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config import Settings
from app.domain.models import InternalDebtorRecord
from app.logging_setup import get_logger
from app.providers.base import ProviderError, ProviderNotConfiguredError
from app.providers.http import ProviderBadResponseError, RetryPolicy
from app.providers.internal.base import InternalDebtorProvider
from app.providers.onec.client import ODataQuery, OneCODataClient
from app.providers.onec.lookup_map import LookupMap, OneCLookupMaps
from app.utils.masking import mask_phone

logger = get_logger(__name__)

# Короче этого запрос не отправляется: поиск по одному-двум символам по чужой
# рабочей базе — это сканирование справочника, а в отчёт он принесёт однофамильцев.
MIN_QUERY_LENGTH = 3
# Платформа отдаёт незаполненную дату как «начало времён». Пропущенная в модель,
# она делает вид, что дата рождения известна, и ломает сопоставление личности.
EMPTY_ONEC_DATE_PREFIX = "0001-01-01"


class OneCODataProvider(InternalDebtorProvider):
    """Учётная база заказчика как ещё один внутренний источник."""

    source_label = "1С"

    def __init__(
        self,
        settings: Settings,
        maps: OneCLookupMaps,
        client: OneCODataClient | None = None,
    ) -> None:
        self._settings = settings
        self._maps = maps
        self._store_phone = settings.store_sensitive_identifiers
        self._client = client or OneCODataClient(
            settings.onec_base_url,
            settings.onec_username,
            settings.onec_password.get_secret_value(),
            timeout_seconds=settings.request_timeout_seconds,
            verify=settings.onec_verify,
            page_size=settings.onec_page_size,
            max_pages=settings.onec_max_pages,
            concurrency=settings.onec_concurrency,
            cache_ttl_seconds=settings.onec_cache_ttl_seconds,
            retry=RetryPolicy(
                max_retries=settings.provider_max_retries,
                backoff_seconds=settings.provider_retry_backoff_seconds,
            ),
        )
        if settings.store_raw_responses:
            # У внешних вендоров сырой ответ — отладка. Здесь это дамп карточек
            # из базы заказчика: паспорта и адреса людей, которые не наши
            # должники и о проверке которых нас никто не просил.
            logger.warning("onec.raw_responses_ignored")

    # ---------------------------------------------------------------- lookups

    async def find_by_fio(
        self, fio: str, *, birth_date: date | None = None
    ) -> list[InternalDebtorRecord]:
        return await self._lookup("by_fio", fio, birth_date=birth_date)

    async def find_by_phone(self, phone: str) -> list[InternalDebtorRecord]:
        return await self._lookup("by_phone", phone)

    async def find_by_contract(self, contract_number: str) -> list[InternalDebtorRecord]:
        return await self._lookup("by_contract", contract_number)

    async def find_by_claim(self, claim_number: str) -> list[InternalDebtorRecord]:
        return await self._lookup("by_claim", claim_number)

    async def find_by_debtor_id(self, debtor_id: str) -> list[InternalDebtorRecord]:
        return await self._lookup("by_debtor_id", debtor_id)

    async def find_by_plate(self, plate: str) -> list[InternalDebtorRecord]:
        return await self._lookup("by_plate", plate)

    async def find_by_vin(self, vin: str) -> list[InternalDebtorRecord]:
        return await self._lookup("by_vin", vin)

    async def find_by_address(self, address: str) -> list[InternalDebtorRecord]:
        return await self._lookup("by_address", address)

    # ---------------------------------------------------------------- internals

    async def _lookup(
        self, lookup: str, value: str, *, birth_date: date | None = None
    ) -> list[InternalDebtorRecord]:
        mapping = self._maps.get(lookup)
        if mapping is None:
            # Не пустой список. Три из восьми поисков в базовом классе по
            # умолчанию возвращают [], и молчаливое «ничего не найдено» по
            # неописанному поиску — ровно то, чего этот проект не делает.
            raise ProviderNotConfiguredError(f"поиск {lookup} не описан в ONEC_FIELD_MAP")

        needle = (value or "").strip()
        if len(needle) < MIN_QUERY_LENGTH:
            raise ProviderError(
                "insufficient_query",
                f"для поиска в 1С по {lookup} нужно не меньше {MIN_QUERY_LENGTH} символов",
            )
        if not mapping.select:  # pragma: no cover - карта не грузится без select
            raise ProviderNotConfiguredError(f"для поиска {lookup} не из чего построить $select")

        rows = await self._client.fetch_rows(
            lookup,
            ODataQuery(
                collection=mapping.collection,
                filter_expr=mapping.filter_for(needle, birth_date=birth_date),
                select=mapping.select,
                orderby=mapping.orderby,
                expand=mapping.expand,
            ),
        )
        return self._to_records(mapping, rows)

    def _to_records(
        self, mapping: LookupMap, rows: list[Mapping[str, Any]]
    ) -> list[InternalDebtorRecord]:
        mapped = mapping.apply(rows)
        if mapped.unreadable and not mapped.records:
            # 1С ответила, и карта не прочитала в ответе ни одного поля.
            # Доложить это как «записей нет» значило бы объявить базу заказчика
            # чистой на основании файла с неверными путями.
            raise ProviderBadResponseError(
                "unexpected_schema",
                f"карта полей не разобрала ни одной строки ответа 1С ({mapping.lookup})",
            )
        if mapped.unreadable:
            logger.warning(
                "onec.unreadable_rows",
                lookup=mapping.lookup,
                unreadable=mapped.unreadable,
                parsed=len(mapped.records),
            )
        return [self._to_record(record) for record in mapped.records]

    def _to_record(self, record: Mapping[str, Any]) -> InternalDebtorRecord:
        phone = _text(record.get("phone"))
        return InternalDebtorRecord(
            debtor_id=_text(record.get("debtor_id")),
            full_name=_text(record.get("full_name")),
            birth_date=_as_date(record.get("birth_date")),
            # Сырой телефон — только когда деплой это разрешил; маска есть
            # всегда, и именно она попадает в отчёт. То же правило, что у
            # импорта: новый источник не заводит для себя исключения.
            phone=phone if self._store_phone else None,
            phone_masked=mask_phone(phone),
            contract_number=_text(record.get("contract_number")),
            claim_number=_text(record.get("claim_number")),
            debt_amount=_as_decimal(record.get("debt_amount")),
            address=_text(record.get("address")),
            vehicle_plate=_text(record.get("vehicle_plate")),
            vin=_text(record.get("vin")),
            created_at=_as_datetime(record.get("created_at")),
        )


# ---------------------------------------------------------------- normalization


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_date(value: Any) -> date | None:
    """``2024-05-01T00:00:00`` → дата; «начало времён» и пустая строка → ``None``."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = _text(value)
    if text is None or text.startswith(EMPTY_ONEC_DATE_PREFIX):
        return None
    parsed = _parse_iso(text)
    return parsed.date() if parsed else None


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    text = _text(value)
    if text is None or text.startswith(EMPTY_ONEC_DATE_PREFIX):
        return None
    return _parse_iso(text)


def _parse_iso(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _as_decimal(value: Any) -> Decimal | None:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    text = _text(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(" ", "").replace(",", "."))
    except InvalidOperation:
        return None


__all__ = ["OneCODataProvider"]
