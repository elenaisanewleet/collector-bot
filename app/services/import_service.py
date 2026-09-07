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
    TotalsRow,
    decode_csv_bytes,
    read_table,
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
# Сколько номеров строк показываем в примере к однотипному замечанию.
MAX_REPORTED_LINES = 3


@dataclass(slots=True)
class _Group:
    """Однотипные замечания: текст без конкретного значения — и все строки."""

    sample: str
    line_numbers: list[int] = field(default_factory=list)


def _split_message(message: str) -> tuple[str, str]:
    """Разделить замечание на вид и конкретное значение.

    «birth_date: не распознана дата «31.02.1980»» — это вид «не распознана
    дата»; иначе каждое кривое значение образовало бы свою группу, и триста
    испорченных дат снова выглядели бы как пять разных мелочей.
    """
    kind, separator, detail = message.partition(" «")
    return (kind, "«" + detail) if separator else (message, "")


def _render_groups(groups: dict[str, _Group]) -> list[str]:
    """Однотипные замечания — одной строкой со счётчиком.

    Показ первых пяти замечаний без счётчика врал: потеря телефона у трёхсот
    должников выглядела как пять мелких придирок. Частое идёт первым — именно
    оно означает, что сломана колонка, а не строка.
    """
    lines: list[str] = []
    ordered = sorted(groups.items(), key=lambda item: (-len(item[1].line_numbers), item[0]))
    for kind, group in ordered[:MAX_REPORTED_ERRORS]:
        line_numbers = group.line_numbers
        if len(line_numbers) == 1:
            lines.append(f"строка {line_numbers[0]}: {kind} {group.sample}".rstrip())
            continue
        if group.sample:
            example = f"строка {line_numbers[0]}: {group.sample}"
        else:
            listed = ", ".join(str(number) for number in line_numbers[:MAX_REPORTED_LINES])
            example = f"строки {listed}"
        lines.append(f"{kind} — строк: {len(line_numbers)} (например, {example})")
    hidden = len(ordered) - MAX_REPORTED_ERRORS
    if hidden > 0:
        lines.append(f"…и ещё видов замечаний: {hidden}")
    return lines


def _remember(groups: dict[str, _Group], line_number: int, message: str) -> None:
    kind, sample = _split_message(message)
    group = groups.setdefault(kind, _Group(sample=sample))
    group.line_numbers.append(line_number)


@dataclass(slots=True)
class ImportReport:
    """Outcome of one import, in the shape the operator is shown."""

    total_rows: int = 0
    imported: int = 0
    created: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    # Итоговые строки отчёта 1С: не должники и не ошибки.
    ignored_totals: int = 0
    # Заголовки, содержимое которых не импортировано. Главная строка отчёта:
    # словарь синонимов всегда неполон, и продукт обязан назвать, чего он не
    # понял, — иначе выброшенное ФИО читается как «300 строк, 0 ошибок».
    unknown_columns: list[str] = field(default_factory=list)
    # Строки, схлопнутые в одну запись при несовпадающих данных: вероятные
    # однофамильцы, а не дубли.
    collapsed_conflicts: list[str] = field(default_factory=list)
    # Повторные строки того же человека с другой машиной или суммой: у
    # эвакуатора это следующее задержание, а не тёзка и не копия. Считаются
    # отдельно, потому что «Пропущено: 263» без причины оператор читает как
    # потерю данных и присылает файл заново.
    merged_episodes: int = 0
    # Замечания к файлу целиком: имя листа, итоговые строки, вторые колонки.
    notes: list[str] = field(default_factory=list)
    error_groups: dict[str, _Group] = field(default_factory=dict)
    warning_groups: dict[str, _Group] = field(default_factory=dict)

    def add_error(self, line_number: int, message: str) -> None:
        self.failed += 1
        _remember(self.error_groups, line_number, message)

    def add_warning(self, line_number: int, message: str) -> None:
        _remember(self.warning_groups, line_number, message)

    @property
    def warning_count(self) -> int:
        return len(self.notes) + sum(
            len(group.line_numbers) for group in self.warning_groups.values()
        )

    @property
    def errors(self) -> list[str]:
        return _render_groups(self.error_groups)

    @property
    def warnings(self) -> list[str]:
        # Замечания по файлу идут первыми и не обрезаются: их единицы, и каждое
        # говорит, что импорт принял решение за оператора.
        return [*self.notes, *_render_groups(self.warning_groups)]


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
            report.notes.insert(0, f"прочитан лист «{sheet.name}»")
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
        table = read_table(text)
        report.unknown_columns = list(table.header.unknown)
        for label in table.header.duplicates:
            report.notes.append(f"колонка {label} — импортирована только первая")

        totals_lines: list[int] = []
        parsed: list[tuple[int, DebtorRow]] = []
        for line_number, item in table.rows:
            if isinstance(item, TotalsRow):
                # Итоговая строка отчёта — не должник: как запись она завышала
                # счётчик импортированных и уезжала в массовый прогон.
                report.ignored_totals += 1
                totals_lines.append(line_number)
                continue
            report.total_rows += 1
            if report.total_rows > self._settings.max_import_rows:
                report.add_error(line_number, "превышен лимит строк на импорт")
                break
            if isinstance(item, RowError):
                report.add_error(line_number, item.message)
                continue
            for warning in item.warnings:
                # Каждое замечание — отдельно, иначе однотипные не группируются.
                report.add_warning(line_number, warning)
            parsed.append((line_number, item))

        if totals_lines:
            example = ", ".join(str(number) for number in totals_lines[:MAX_REPORTED_LINES])
            label = "строка" if len(totals_lines) == 1 else "строки"
            report.notes.append(
                f"итоговых строк отчёта пропущено: {report.ignored_totals} ({label} {example})"
            )

        await self._store(parsed, report)

        if telegram_user_id is not None:
            async with self._database.session() as session:
                await AuditRepository(session).record(
                    telegram_user_id=telegram_user_id,
                    action="import.csv",
                    detail=(
                        f"всего {report.total_rows}, импортировано {report.imported}, "
                        f"пропущено {report.skipped}, ошибок {report.failed}, "
                        f"нераспознанных колонок {len(report.unknown_columns)}"
                    ),
                )

        logger.info(
            "import.finished",
            total=report.total_rows,
            imported=report.imported,
            skipped=report.skipped,
            failed=report.failed,
            unknown_columns=len(report.unknown_columns),
        )
        return report

    def _estimate_debt(self, row: DebtorRow) -> None:
        """Посчитать долг по тарифу, если выгрузка суммы не принесла.

        Считается ровно то, что взыскатель-эвакуатор и выставляет: перемещение
        плюс хранение. Хранение — за ПОЛНЫЕ сутки: почасовую оплату отменили, и
        неполные сутки не тарифицируются. Это не мелочь округления: на живой
        выгрузке медиана стоянки — десять часов, то есть у большинства
        должников суток хранения ноль и долг равен одной эвакуации. Считать их
        по началу суток значило бы завысить требование двум тысячам человек.

        Сумма из выгрузки всегда сильнее расчёта: документ важнее оценки.
        """
        if row.debt_amount is not None:
            return
        if not self._settings.tow_fee and not self._settings.storage_fee_per_day:
            return
        if row.impounded_at is None or row.released_at is None:
            return
        hours = (row.released_at - row.impounded_at).total_seconds() / 3600
        if hours < 0:
            # Выдали раньше, чем привезли: в выгрузке опечатка, и считать по ней
            # нельзя. Молчаливый ноль тут хуже пустого места.
            return
        full_days = int(hours // 24)
        row.debt_amount = self._settings.tow_fee + self._settings.storage_fee_per_day * full_days
        row.debt_is_estimated = True

    async def _store(self, rows: list[tuple[int, DebtorRow]], report: ImportReport) -> None:
        if not rows:
            return
        # Collapse duplicates inside the file itself first, so the last
        # occurrence wins deterministically rather than by insertion race.
        deduped: dict[str, tuple[int, DebtorRow]] = {}
        for line_number, row in rows:
            key = row.dedup_key
            previous = deduped.get(key)
            if previous is None:
                deduped[key] = (line_number, row)
                continue
            report.skipped += 1
            kept = previous[1]
            if kept.comparable == row.comparable:
                pass  # Полный повтор строки — поднимать тревогу не о чем.
            elif not row.debtor_id and (not row.identity_is_strong or kept.contradicts(row)):
                # Одинаковый ключ при разошедшихся данных — почти всегда тёзки, а
                # не повтор. «Пропущено: 1» об этом не говорит, а в суд с чужими
                # производствами идти нельзя. Совпадение по коду должника сюда не
                # относится: это ключ самого заказчика.
                report.collapsed_conflicts.append(
                    f"строки {previous[0]} и {line_number}: "
                    f"«{row.full_name or row.contract_number}»"
                )
            else:
                # Человек опознан надёжно, разошлись только машина или сумма: у
                # эвакуатора это следующее задержание того же должника. Считаем
                # отдельно — иначе владелец получает сотню ложных тревог про
                # однофамильцев и перестаёт читать настоящие.
                report.merged_episodes += 1
            # Дописываем, а не заменяем: строки одного должника дополняют друг
            # друга, и поздняя без ИНН не должна стирать ранний ИНН.
            row.absorb(kept)
            deduped[key] = (line_number, row)

        async with self._database.session() as session:
            repo = DebtorRepository(session)
            for key, (_line_number, row) in deduped.items():
                self._estimate_debt(row)
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
            # Одна машина — не список: строка «А123ВС777» в отдельной колонке
            # ничего не добавляет к ``vehicle_plate`` и только смотрится как
            # второй источник правды.
            vehicle_plates=", ".join(row.vehicle_plates) if len(row.vehicle_plates) > 1 else None,
            source_record_ids=", ".join(row.source_ids) or None,
            debt_is_estimated=row.debt_is_estimated,
            impounded_at=row.impounded_at,
            released_at=row.released_at,
            vin=row.vin,
            source="csv_import",
        )
