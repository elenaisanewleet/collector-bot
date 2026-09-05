"""Parsing and validation of the internal debtor CSV export.

Real exports are messy: columns get renamed, encodings vary, amounts arrive with
currency symbols, and some rows are simply broken. The rules here are permissive
about shape and strict about identity — a row is accepted when it carries at
least a name or a contract number, and rejected otherwise, because a row with
neither cannot be matched to anything.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.domain.identity import (
    INN_INDIVIDUAL_LENGTH,
    NameParseError,
    normalize_address,
    normalize_inn,
    normalize_phone,
    normalize_plate,
    normalize_vin,
    parse_fio,
)
from app.utils.dates import parse_date
from app.utils.hashing import stable_hash
from app.utils.money import parse_amount

CANONICAL_COLUMNS = (
    "debtor_id",
    "fio",
    "birth_date",
    "phone",
    "inn",
    "contract_number",
    "claim_number",
    "debt_amount",
    "inn",
    "address",
    "vehicle_plate",
    "vin",
    "created_at",
)

# Aliases seen in practice, so an export does not have to be renamed by hand.
COLUMN_ALIASES: dict[str, str] = {
    "id": "debtor_id",
    "debtorid": "debtor_id",
    "external_id": "debtor_id",
    "код": "debtor_id",
    "fio": "fio",
    "name": "fio",
    "full_name": "fio",
    "фио": "fio",
    "birthdate": "birth_date",
    "dob": "birth_date",
    "birth": "birth_date",
    "дата_рождения": "birth_date",
    "phone": "phone",
    "tel": "phone",
    "телефон": "phone",
    # ИНН физлица. Ради него колонка и заводится: без него банкротство, статус
    # ИП и арбитраж не проверяются вовсе — эти источники ищут только по нему.
    # Выгрузка из 1С его обычно содержит, а импорт до сих пор молча выбрасывал.
    "inn": "inn",
    "инн": "inn",
    "innfiz": "inn",
    "contract": "contract_number",
    "contract_no": "contract_number",
    "договор": "contract_number",
    "claim": "claim_number",
    "заявка": "claim_number",
    "amount": "debt_amount",
    "debt": "debt_amount",
    "долг": "debt_amount",
    "sum": "debt_amount",
    "address": "address",
    "адрес": "address",
    "plate": "vehicle_plate",
    "gosnomer": "vehicle_plate",
    "госномер": "vehicle_plate",
    "vin": "vin",
    "created": "created_at",
}

SUPPORTED_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")
SUPPORTED_DELIMITERS = ",;\t"
MAX_FIELD_LENGTH = 512


class CsvFormatError(ValueError):
    """The file as a whole cannot be read as a debtor export."""


@dataclass(slots=True)
class DebtorRow:
    """One validated row, normalized and ready to store."""

    debtor_id: str | None = None
    full_name: str | None = None
    birth_date: date | None = None
    phone: str | None = None
    inn: str | None = None
    contract_number: str | None = None
    claim_number: str | None = None
    debt_amount: Decimal | None = None
    address: str | None = None
    vehicle_plate: str | None = None
    vin: str | None = None
    created_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def dedup_key(self) -> str:
        """Deterministic identity for upserts.

        ``debtor_id`` wins when present — it is the customer's own key. Otherwise
        the normalized name, date of birth and contract number form a stable
        composite, so re-importing the same export updates rows instead of
        multiplying them.
        """
        if self.debtor_id:
            return stable_hash("id", self.debtor_id)
        return stable_hash(
            "composite",
            self.full_name,
            self.birth_date.isoformat() if self.birth_date else None,
            self.contract_number,
        )


@dataclass(slots=True)
class RowError:
    line_number: int
    message: str


def decode_csv_bytes(payload: bytes) -> str:
    """Decode an upload, trying the encodings Russian exports actually use."""
    for encoding in SUPPORTED_ENCODINGS:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError(
        "Не удалось определить кодировку файла. Поддерживаются UTF-8 и Windows-1251."
    )


def _detect_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=SUPPORTED_DELIMITERS).delimiter
    except csv.Error:
        # Sniffing fails on single-column or unusual files; comma is the
        # documented default.
        return ","


def normalize_header(name: str) -> str | None:
    key = name.strip().lower().lstrip("﻿").replace(" ", "_")
    if key in CANONICAL_COLUMNS:
        return key
    return COLUMN_ALIASES.get(key)


def iter_rows(text: str) -> Iterator[tuple[int, DebtorRow | RowError]]:
    """Yield ``(line_number, row_or_error)`` for every data line.

    A malformed row produces a :class:`RowError` and the iteration continues —
    one bad line must never abort an import of several hundred good ones.
    """
    if not text.strip():
        raise CsvFormatError("Файл пуст.")

    sample = text[:4096]
    reader = csv.reader(io.StringIO(text), delimiter=_detect_delimiter(sample))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CsvFormatError("В файле нет заголовка.") from exc

    mapping = {index: normalize_header(name) for index, name in enumerate(header)}
    known = {value for value in mapping.values() if value}
    if not known:
        raise CsvFormatError(
            "Не распознана ни одна колонка. Ожидаются, например: "
            + ", ".join(CANONICAL_COLUMNS[:5])
        )
    if not ({"fio", "contract_number"} & known):
        raise CsvFormatError("Нужна хотя бы одна из колонок: fio или contract_number.")

    for line_number, raw_row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw_row):
            continue
        try:
            yield line_number, _build_row(mapping, raw_row)
        except ValueError as exc:
            yield line_number, RowError(line_number=line_number, message=str(exc))


def _build_row(mapping: dict[int, str | None], raw_row: list[str]) -> DebtorRow:
    values: dict[str, str] = {}
    for index, cell in enumerate(raw_row):
        column = mapping.get(index)
        if column:
            values[column] = cell.strip()[:MAX_FIELD_LENGTH]

    row = DebtorRow()
    row.debtor_id = values.get("debtor_id") or None
    row.full_name = _clean_name(values.get("fio"), row)
    row.contract_number = values.get("contract_number") or None
    row.claim_number = values.get("claim_number") or None

    if not row.full_name and not row.contract_number:
        raise ValueError("Нужно указать fio или contract_number")

    row.birth_date = _parse_optional_date(values.get("birth_date"), "birth_date", row)
    row.phone = _parse_optional_phone(values.get("phone"), row)
    row.inn = _parse_optional_inn(values.get("inn"), row)
    row.debt_amount = _parse_optional_amount(values.get("debt_amount"), row)
    row.inn = _parse_optional_inn(values.get("inn"), row)
    row.address = normalize_address(values.get("address"))
    row.vehicle_plate = _parse_optional_plate(values.get("vehicle_plate"), row)
    row.vin = _parse_optional_vin(values.get("vin"), row)
    row.created_at = _parse_created_at(values.get("created_at"))
    return row


def _clean_name(raw: str | None, row: DebtorRow) -> str | None:
    if not raw:
        return None
    try:
        return parse_fio(raw).full
    except NameParseError:
        # Keep the raw value: an unparseable name is still worth storing and
        # displaying, it just cannot participate in structured name matching.
        row.warnings.append("fio: не разобрано в формате «Фамилия Имя Отчество»")
        return " ".join(raw.split()) or None


def _parse_optional_date(raw: str | None, label: str, row: DebtorRow) -> date | None:
    if not raw:
        return None
    parsed = parse_date(raw)
    if parsed is None:
        row.warnings.append(f"{label}: не распознана дата «{raw}»")
    return parsed


def _parse_optional_inn(raw: str | None, row: DebtorRow) -> str | None:
    """ИНН физлица из выгрузки — двенадцать цифр, и только они.

    Десятизначный ИНН принадлежит юрлицу, и подставлять его в проверку человека
    нельзя: источники отвергнут запрос, а оператор увидит «не проверено» без
    объяснимой причины. Непохожее значение не молчит, а становится замечанием
    к строке — выгрузка чинится один раз, а неверный ИНН тянулся бы в каждый
    отчёт по этому должнику.
    """
    if not raw:
        return None
    normalized = normalize_inn(raw)
    if normalized is None or len(normalized) != INN_INDIVIDUAL_LENGTH:
        row.warnings.append(f"inn: не похоже на ИНН физлица «{raw}»")
        return None
    return normalized


def _parse_optional_phone(raw: str | None, row: DebtorRow) -> str | None:
    if not raw:
        return None
    normalized = normalize_phone(raw)
    if normalized is None:
        row.warnings.append("phone: не распознан российский номер")
    return normalized


def _parse_optional_amount(raw: str | None, row: DebtorRow) -> Decimal | None:
    if not raw:
        return None
    amount = parse_amount(raw)
    if amount is None:
        row.warnings.append("debt_amount: не распознана сумма")
        return None
    if amount < 0:
        row.warnings.append("debt_amount: отрицательная сумма проигнорирована")
        return None
    return amount


def _parse_optional_plate(raw: str | None, row: DebtorRow) -> str | None:
    if not raw:
        return None
    normalized = normalize_plate(raw)
    if normalized is None:
        row.warnings.append("vehicle_plate: не распознан госномер")
    return normalized


def _parse_optional_vin(raw: str | None, row: DebtorRow) -> str | None:
    if not raw:
        return None
    normalized = normalize_vin(raw)
    if normalized is None:
        row.warnings.append("vin: не распознан VIN (нужно 17 символов)")
    return normalized


def _parse_created_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = parse_date(raw)
        return datetime.combine(parsed, datetime.min.time()) if parsed else None
