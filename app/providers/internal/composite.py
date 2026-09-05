"""Fan-out across several internal sources.

Today that is the bootstrap CSV plus the database. Tomorrow a 1С adapter joins
the list and nothing above this class changes.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import date

from app.domain.models import InternalDebtorRecord
from app.logging_setup import get_logger
from app.providers.internal.base import InternalDebtorProvider, InternalSourceError
from app.utils.hashing import stable_hash

logger = get_logger(__name__)

Lookup = Callable[[InternalDebtorProvider], Awaitable[list[InternalDebtorRecord]]]


class CompositeInternalDebtorProvider(InternalDebtorProvider):
    def __init__(self, providers: Sequence[InternalDebtorProvider]) -> None:
        self._providers = list(providers)

    async def _gather(self, lookup: Lookup) -> list[InternalDebtorRecord]:
        if not self._providers:
            return []
        results = await asyncio.gather(
            *(lookup(provider) for provider in self._providers),
            return_exceptions=True,
        )
        records: list[InternalDebtorRecord] = []
        failures: list[BaseException] = []
        for item in results:
            # One failing internal source must not hide the others.
            if isinstance(item, BaseException):
                failures.append(item)
                continue
            records.extend(item)
        if failures:
            logger.warning(
                "internal.source_failed",
                failed=len(failures),
                total=len(self._providers),
                error=type(failures[0]).__name__,
            )
        if failures and not records:
            # Ни одной записи и хотя бы один упавший источник: сказать
            # «совпадений нет» здесь было бы утверждением, которого никто не
            # проверял. Пусть отчёт покажет это как ошибку источника.
            raise InternalSourceError(type(failures[0]).__name__)
        return _dedupe(records)

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
