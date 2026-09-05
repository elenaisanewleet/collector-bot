"""Fan-out across several internal sources.

Today that is the bootstrap CSV, the database and — once its map and credentials
exist — the customer's 1С. Nothing above this class changes when the list grows.

One failing internal source must not hide the others, and it must not hide
itself either. Swallowing the exception (which this class used to do) turned an
unreachable 1С into a clean internal base: the report printed "совпадений во
внутренней базе не найдено" about a source nobody managed to ask.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import date

from app.domain.enums import ProviderStatus
from app.domain.models import InternalDebtorRecord
from app.logging_setup import get_logger
from app.providers.base import ProviderError
from app.providers.internal.base import (
    InternalDebtorProvider,
    InternalRecords,
    InternalSourceFailure,
)
from app.utils.hashing import stable_hash

logger = get_logger(__name__)

Lookup = Callable[[InternalDebtorProvider], Awaitable[list[InternalDebtorRecord]]]


class CompositeInternalDebtorProvider(InternalDebtorProvider):
    source_label = "внутренние источники"

    def __init__(self, providers: Sequence[InternalDebtorProvider]) -> None:
        self._providers = list(providers)

    @property
    def sources(self) -> list[InternalDebtorProvider]:
        """The sources in play. Read-only: composition is the registry's job."""
        return list(self._providers)

    async def _gather(self, lookup: Lookup) -> InternalRecords:
        if not self._providers:
            return InternalRecords()
        results = await asyncio.gather(
            *(lookup(provider) for provider in self._providers),
            return_exceptions=True,
        )
        records: list[InternalDebtorRecord] = []
        failures: list[InternalSourceFailure] = []
        for provider, item in zip(self._providers, results, strict=True):
            if isinstance(item, asyncio.CancelledError):
                raise item
            if isinstance(item, BaseException):
                failures.append(_failure(provider, item))
                continue
            records.extend(item)
            # A nested composite carries its own failures; keep them.
            failures.extend(getattr(item, "failures", ()))
        # Dedup returns a plain list, so failures are attached afterwards —
        # otherwise the very thing this method exists for would be dropped.
        return InternalRecords(_dedupe(records), failures=failures)

    async def find_by_fio(
        self, fio: str, *, birth_date: date | None = None
    ) -> list[InternalDebtorRecord]:
        return await self._gather(lambda provider: provider.find_by_fio(fio, birth_date=birth_date))

    async def find_by_phone(self, phone: str) -> list[InternalDebtorRecord]:
        return await self._gather(lambda provider: provider.find_by_phone(phone))

    async def find_by_contract(self, contract_number: str) -> list[InternalDebtorRecord]:
        return await self._gather(lambda provider: provider.find_by_contract(contract_number))

    async def find_by_claim(self, claim_number: str) -> list[InternalDebtorRecord]:
        return await self._gather(lambda provider: provider.find_by_claim(claim_number))

    async def find_by_debtor_id(self, debtor_id: str) -> list[InternalDebtorRecord]:
        return await self._gather(lambda provider: provider.find_by_debtor_id(debtor_id))

    async def find_by_plate(self, plate: str) -> list[InternalDebtorRecord]:
        return await self._gather(lambda provider: provider.find_by_plate(plate))

    async def find_by_vin(self, vin: str) -> list[InternalDebtorRecord]:
        return await self._gather(lambda provider: provider.find_by_vin(vin))

    async def find_by_address(self, address: str) -> list[InternalDebtorRecord]:
        return await self._gather(lambda provider: provider.find_by_address(address))


def _failure(provider: InternalDebtorProvider, error: BaseException) -> InternalSourceFailure:
    if isinstance(error, ProviderError):
        return InternalSourceFailure(
            source=provider.source_label,
            status=error.status,
            error_code=error.code,
            error_message=error.message,
        )
    # A bug inside one source degrades one source, and says so.
    logger.error(
        "internal_source.unhandled",
        source=provider.source_label,
        error_type=type(error).__name__,
        exc_info=error,
    )
    return InternalSourceFailure(
        source=provider.source_label,
        status=ProviderStatus.ERROR,
        error_code="unhandled_exception",
        error_message=type(error).__name__,
    )


def _dedupe(records: Sequence[InternalDebtorRecord]) -> list[InternalDebtorRecord]:
    """Collapse the same debtor arriving from two internal sources.

    Uses the same identity rule as the CSV importer so the CSV file and the
    imported copy of it do not appear as two people.
    """
    seen: set[str] = set()
    unique: list[InternalDebtorRecord] = []
    for record in records:
        key = stable_hash(
            record.debtor_id or "",
            record.full_name,
            record.birth_date.isoformat() if record.birth_date else None,
            record.contract_number,
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique
