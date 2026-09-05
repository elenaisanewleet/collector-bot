"""CSV-backed implementation of :class:`InternalDebtorProvider`.

This is the MVP stand-in for the customer's own system. It reads an export from
disk, indexes it in memory and reloads when the file changes on disk, so
replacing the export does not require a restart.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import date
from pathlib import Path

from app.domain.models import InternalDebtorRecord
from app.logging_setup import get_logger
from app.providers.internal.base import InternalDebtorProvider, InternalSourceError
from app.providers.internal.csv_schema import (
    CsvFormatError,
    DebtorRow,
    RowError,
    decode_csv_bytes,
    iter_rows,
)
from app.utils.hashing import normalize_token
from app.utils.masking import mask_phone

logger = get_logger(__name__)


class CSVInternalDebtorProvider(InternalDebtorProvider):
    """Indexed, read-only view over a CSV export.

    Loading is lazy and guarded by a lock so concurrent searches at startup do
    not each parse the file.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._loaded_mtime: float | None = None
        self._rows: list[DebtorRow] = []
        self._by_name: dict[str, list[DebtorRow]] = defaultdict(list)
        self._by_phone: dict[str, list[DebtorRow]] = defaultdict(list)
        self._by_contract: dict[str, list[DebtorRow]] = defaultdict(list)
        self._by_claim: dict[str, list[DebtorRow]] = defaultdict(list)
        self._by_debtor_id: dict[str, list[DebtorRow]] = defaultdict(list)
        self._by_plate: dict[str, list[DebtorRow]] = defaultdict(list)
        self._by_vin: dict[str, list[DebtorRow]] = defaultdict(list)

    @property
    def path(self) -> Path:
        return self._path

    async def _ensure_loaded(self) -> None:
        if not self._path.is_file():
            return
        mtime = self._path.stat().st_mtime
        if self._loaded_mtime == mtime:
            return
        async with self._lock:
            if self._loaded_mtime == mtime:
                return
            await asyncio.to_thread(self._load, mtime)

    def _load(self, mtime: float) -> None:
        try:
            text = decode_csv_bytes(self._path.read_bytes())
            rows: list[DebtorRow] = []
            errors = 0
            for _line, item in iter_rows(text):
                if isinstance(item, RowError):
                    errors += 1
                else:
                    rows.append(item)
        except (OSError, CsvFormatError) as exc:
            # Нечитаемая выгрузка — это отказ источника, а не пустая выгрузка.
            # Проглоченная здесь ошибка выше по цепочке стала бы строкой
            # «совпадений во внутренней базе нет».
            logger.warning("internal_csv.load_failed", path=str(self._path), error=str(exc))
            raise InternalSourceError(str(exc)) from exc

        self._rows = rows
        self._reindex()
        self._loaded_mtime = mtime
        logger.info(
            "internal_csv.loaded",
            path=str(self._path),
            rows=len(rows),
            skipped=errors,
        )

    def _reindex(self) -> None:
        self._by_name = defaultdict(list)
        self._by_phone = defaultdict(list)
        self._by_contract = defaultdict(list)
        self._by_claim = defaultdict(list)
        self._by_debtor_id = defaultdict(list)
        self._by_plate = defaultdict(list)
        self._by_vin = defaultdict(list)
        for row in self._rows:
            if row.full_name:
                self._by_name[normalize_token(row.full_name)].append(row)
            if row.phone:
                self._by_phone[row.phone].append(row)
            if row.contract_number:
                self._by_contract[normalize_token(row.contract_number)].append(row)
            if row.claim_number:
                self._by_claim[normalize_token(row.claim_number)].append(row)
            if row.debtor_id:
                self._by_debtor_id[normalize_token(row.debtor_id)].append(row)
            if row.vehicle_plate:
                self._by_plate[row.vehicle_plate].append(row)
            if row.vin:
                self._by_vin[row.vin].append(row)

    async def find_by_fio(
        self, fio: str, *, birth_date: date | None = None
    ) -> list[InternalDebtorRecord]:
        await self._ensure_loaded()
        key = normalize_token(fio)
        candidates = list(self._by_name.get(key, ()))
        if not candidates:
            # Fall back to surname + given name, for records stored without a
            # patronymic.
            short = " ".join(key.split()[:2])
            candidates = [
                row
                for name, rows in self._by_name.items()
                if " ".join(name.split()[:2]) == short
                for row in rows
            ]
        if birth_date is not None:
            exact = [row for row in candidates if row.birth_date == birth_date]
            if exact:
                candidates = exact
        return [to_record(row) for row in candidates]

    async def find_by_phone(self, phone: str) -> list[InternalDebtorRecord]:
        await self._ensure_loaded()
        return [to_record(row) for row in self._by_phone.get(phone, ())]

    async def find_by_contract(self, contract_number: str) -> list[InternalDebtorRecord]:
        await self._ensure_loaded()
        key = normalize_token(contract_number)
        return [to_record(row) for row in self._by_contract.get(key, ())]

    async def find_by_claim(self, claim_number: str) -> list[InternalDebtorRecord]:
        await self._ensure_loaded()
        return [to_record(row) for row in self._by_claim.get(normalize_token(claim_number), ())]

    async def find_by_debtor_id(self, debtor_id: str) -> list[InternalDebtorRecord]:
        await self._ensure_loaded()
        return [to_record(row) for row in self._by_debtor_id.get(normalize_token(debtor_id), ())]

    async def find_by_plate(self, plate: str) -> list[InternalDebtorRecord]:
        await self._ensure_loaded()
        return [to_record(row) for row in self._by_plate.get(plate, ())]

    async def find_by_vin(self, vin: str) -> list[InternalDebtorRecord]:
        await self._ensure_loaded()
        return [to_record(row) for row in self._by_vin.get(vin, ())]

    async def find_by_address(self, address: str) -> list[InternalDebtorRecord]:
        await self._ensure_loaded()
        needle = normalize_token(address)
        if not needle:
            return []
        return [
            to_record(row)
            for row in self._rows
            if row.address and needle in normalize_token(row.address)
        ]


def to_record(row: DebtorRow) -> InternalDebtorRecord:
    return InternalDebtorRecord(
        debtor_id=row.debtor_id,
        full_name=row.full_name,
        birth_date=row.birth_date,
        phone=row.phone,
        phone_masked=mask_phone(row.phone),
        contract_number=row.contract_number,
        claim_number=row.claim_number,
        debt_amount=row.debt_amount,
        address=row.address,
        vehicle_plate=row.vehicle_plate,
        vin=row.vin,
        created_at=row.created_at,
    )
