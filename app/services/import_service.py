"""CSV import.

Robustness rules, in priority order:

1. A single malformed row never aborts the import.
2. Re-importing the same export updates rows instead of duplicating them.
3. A sparser export never blanks out data an earlier one supplied.
4. Nothing is stored that the privacy settings do not permit.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from app.config import Settings
from app.db.models import Debtor
from app.db.repository import AuditRepository, DebtorRepository, phone_hash
from app.db.session import Database
from app.logging_setup import get_logger
from app.providers.internal.csv_schema import (
    CsvFormatError,
    DebtorRow,
    RowError,
    decode_csv_bytes,
    iter_rows,
)
from app.providers.internal.xlsx import (
    LEGACY_XLS_MESSAGE,
    looks_like_legacy_xls,
    looks_like_xlsx,
    xlsx_to_sheet,
)
from app.utils.hashing import normalize_token
from app.utils.masking import mask_phone

logger = get_logger(__name__)

MAX_REPORTED_ERRORS = 5


@dataclass(slots=True)
class ImportReport:
    """Outcome of one import, in the shape the operator is shown."""

    total_rows: int = 0
    imported: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add_error(self, line_number: int, message: str) -> None:
        self.failed += 1
        if len(self.errors) < MAX_REPORTED_ERRORS:
            self.errors.append(f"строка {line_number}: {message}")


class ImportService:
    """Turns a CSV export into rows in the ``debtors`` table."""

    def __init__(self, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database

    async def import_bytes(
        self, payload: bytes, *, telegram_user_id: int | None = None
    ) -> ImportReport:
        if len(payload) > self._settings.max_import_file_bytes:
            limit_mb = self._settings.max_import_file_bytes / (1024 * 1024)
            raise CsvFormatError(f"Файл больше допустимых {limit_mb:.0f} МБ.")
        if looks_like_legacy_xls(payload):
            # Отдельная ветка ради внятного ответа: как CSV этот файл выглядит
            # набором двоичного мусора, и оператор получил бы «не распознана ни
            # одна колонка» вместо «пересохраните в другом формате».
            raise CsvFormatError(LEGACY_XLS_MESSAGE)
        if looks_like_xlsx(payload):
            # Разбор книги — CPU-bound и на сотнях строк ощутим: держим его вне
            # цикла событий, иначе импорт подвесит идущие поиски.
            sheet = await asyncio.to_thread(xlsx_to_sheet, payload)
            report = await self.import_text(sheet.text, telegram_user_id=telegram_user_id)
            # В книге обычно несколько листов, и выбор делаем мы, а не оператор.
            # Молчаливый выбор — способ импортировать справочник вместо выгрузки
            # и не узнать об этом; название листа делает выбор проверяемым.
            report.warnings.insert(0, f"прочитан лист «{sheet.name}»")
            return report
        text = decode_csv_bytes(payload)
        return await self.import_text(text, telegram_user_id=telegram_user_id)

    async def import_file(self, path: Path, *, telegram_user_id: int | None = None) -> ImportReport:
        # Read off the event loop: a multi-megabyte export would otherwise stall
        # every other in-flight search.
        payload = await asyncio.to_thread(path.read_bytes)
        return await self.import_bytes(payload, telegram_user_id=telegram_user_id)

    async def import_text(self, text: str, *, telegram_user_id: int | None = None) -> ImportReport:
        report = ImportReport()
        # Rows are collected before touching the database so a format error in
        # the header fails the whole file cleanly, before any partial write.
        parsed: list[DebtorRow] = []
        for line_number, item in iter_rows(text):
            report.total_rows += 1
            if report.total_rows > self._settings.max_import_rows:
                report.add_error(line_number, "превышен лимит строк на импорт")
                break
            if isinstance(item, RowError):
                report.add_error(line_number, item.message)
                continue
            if item.warnings and len(report.warnings) < MAX_REPORTED_ERRORS:
                report.warnings.append(f"строка {line_number}: {'; '.join(item.warnings)}")
            parsed.append(item)

        await self._store(parsed, report)

        if telegram_user_id is not None:
            async with self._database.session() as session:
                await AuditRepository(session).record(
                    telegram_user_id=telegram_user_id,
                    action="import.csv",
                    detail=(
                        f"всего {report.total_rows}, импортировано {report.imported}, "
                        f"пропущено {report.skipped}, ошибок {report.failed}"
                    ),
                )

        logger.info(
            "import.finished",
            total=report.total_rows,
            imported=report.imported,
            skipped=report.skipped,
            failed=report.failed,
        )
        return report

    async def _store(self, rows: list[DebtorRow], report: ImportReport) -> None:
        if not rows:
            return
        # Collapse duplicates inside the file itself first, so the last
        # occurrence wins deterministically rather than by insertion race.
        deduped: dict[str, DebtorRow] = {}
        for row in rows:
            key = row.dedup_key
            if key in deduped:
                report.skipped += 1
            deduped[key] = row

        async with self._database.session() as session:
            repo = DebtorRepository(session)
            for key, row in deduped.items():
                _, created = await repo.upsert(self._to_model(row, key))
                report.imported += 1
                if created:
                    report.created += 1
                else:
                    report.updated += 1

    def _to_model(self, row: DebtorRow, dedup_key: str) -> Debtor:
        """Map a parsed row onto the table, honouring the privacy settings.

        The full phone number is stored only when
        ``STORE_SENSITIVE_IDENTIFIERS`` is on; the masked form and a hash are
        always stored, which is enough to search by phone and to display a
        recognizable value.
        """
        store_raw = self._settings.store_sensitive_identifiers
        return Debtor(
            dedup_key=dedup_key,
            external_debtor_id=row.debtor_id,
            fio=row.full_name,
            fio_normalized=normalize_token(row.full_name) or None,
            birth_date=row.birth_date,
            phone=row.phone if store_raw else None,
            phone_masked=mask_phone(row.phone),
            phone_hash=phone_hash(row.phone),
            inn=row.inn,
            contract_number=row.contract_number,
            claim_number=row.claim_number,
            debt_amount=row.debt_amount,
            address=row.address,
            vehicle_plate=row.vehicle_plate,
            vin=row.vin,
            source="csv_import",
        )
