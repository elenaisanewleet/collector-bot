"""Database-backed internal provider.

Serves whatever has been loaded through ``/import`` or the seed script. Paired
with the CSV provider it means the bootstrap export works immediately while
later imports accumulate in the database.
"""

from __future__ import annotations

from datetime import date

from app.db.repository import DebtorRepository, debtor_to_record, phone_hash
from app.db.session import Database
from app.domain.models import InternalDebtorRecord
from app.providers.internal.base import InternalDebtorProvider


class DatabaseInternalDebtorProvider(InternalDebtorProvider):
    source_label = "внутренняя база"

    def __init__(self, database: Database) -> None:
        self._database = database

    async def find_by_fio(
        self, fio: str, *, birth_date: date | None = None
    ) -> list[InternalDebtorRecord]:
        async with self._database.session() as session:
            repo = DebtorRepository(session)
            rows = await repo.find_by_fio(fio)
            if not rows:
                short = " ".join(fio.split()[:2])
                rows = await repo.find_by_fio_prefix(short)
            if birth_date is not None:
                exact = [row for row in rows if row.birth_date == birth_date]
                if exact:
                    rows = exact
            return [debtor_to_record(row) for row in rows]

    async def find_by_phone(self, phone: str) -> list[InternalDebtorRecord]:
        digest = phone_hash(phone)
        if not digest:
            return []
        async with self._database.session() as session:
            rows = await DebtorRepository(session).find_by_phone_hash(digest)
            return [debtor_to_record(row) for row in rows]

    async def find_by_contract(self, contract_number: str) -> list[InternalDebtorRecord]:
        async with self._database.session() as session:
            rows = await DebtorRepository(session).find_by_contract(contract_number)
            return [debtor_to_record(row) for row in rows]

    async def find_by_claim(self, claim_number: str) -> list[InternalDebtorRecord]:
        async with self._database.session() as session:
            rows = await DebtorRepository(session).find_by_claim(claim_number)
            return [debtor_to_record(row) for row in rows]

    async def find_by_debtor_id(self, debtor_id: str) -> list[InternalDebtorRecord]:
        async with self._database.session() as session:
            rows = await DebtorRepository(session).find_by_external_id(debtor_id)
            return [debtor_to_record(row) for row in rows]

    async def find_by_plate(self, plate: str) -> list[InternalDebtorRecord]:
        async with self._database.session() as session:
            rows = await DebtorRepository(session).find_by_plate(plate)
            return [debtor_to_record(row) for row in rows]

    async def find_by_vin(self, vin: str) -> list[InternalDebtorRecord]:
        async with self._database.session() as session:
            rows = await DebtorRepository(session).find_by_vin(vin)
            return [debtor_to_record(row) for row in rows]

    async def find_by_address(self, address: str) -> list[InternalDebtorRecord]:
        async with self._database.session() as session:
            rows = await DebtorRepository(session).find_by_address(address)
            return [debtor_to_record(row) for row in rows]
