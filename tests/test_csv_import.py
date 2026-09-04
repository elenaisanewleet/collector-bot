"""CSV parsing and import."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.config import Settings
from app.container import Container
from app.db.repository import DebtorRepository
from app.providers.internal.csv_schema import (
    CsvFormatError,
    DebtorRow,
    RowError,
    decode_csv_bytes,
    iter_rows,
)
from app.services.import_service import ImportService

HEADER = (
    "debtor_id,fio,birth_date,phone,contract_number,claim_number,"
    "debt_amount,address,vehicle_plate,vin,created_at"
)


def csv_text(*rows: str) -> str:
    return "\n".join([HEADER, *rows])


def parse(text: str) -> tuple[list[DebtorRow], list[RowError]]:
    rows: list[DebtorRow] = []
    errors: list[RowError] = []
    for _line, item in iter_rows(text):
        (errors if isinstance(item, RowError) else rows).append(item)  # type: ignore[arg-type]
    return rows, errors


# ---------------------------------------------------------------- parsing


def test_valid_row_is_fully_parsed() -> None:
    rows, errors = parse(
        csv_text(
            "DEM-1,Тестов Андрей Сергеевич,12.03.1985,+7 (999) 123-45-01,"
            'EV-1,ZA-1,38400,"Москва, ул. Тест",А123ВС77,XW8ZZZ61ZKG011111,2026-01-15'
        )
    )
    assert not errors
    row = rows[0]
    assert row.debtor_id == "DEM-1"
    assert row.full_name == "Тестов Андрей Сергеевич"
    assert row.birth_date == date(1985, 3, 12)
    assert row.phone == "+79991234501"
    assert row.debt_amount == Decimal("38400")
    assert row.vehicle_plate == "А123ВС77"
    assert row.vin == "XW8ZZZ61ZKG011111"


def test_blank_lines_are_skipped_silently() -> None:
    """A trailing or interleaved blank line is noise, not an error."""
    rows, errors = parse(
        csv_text("DEM-1,Тестов Андрей Сергеевич,,,EV-1,,100,,,,", ",,,,,,,,,,", "")
    )
    assert len(rows) == 1
    assert not errors


def test_a_malformed_row_does_not_abort_the_file() -> None:
    """One unusable line costs one line, not the import."""
    rows, errors = parse(
        csv_text(
            "DEM-1,Тестов Андрей Сергеевич,,,EV-1,,1000,,,,",
            ",,,,,,1000,Москва,,,",  # has data but no name and no contract
            "DEM-3,Демов Максим Игоревич,,,EV-3,,3000,,,,",
        )
    )
    assert len(rows) == 2
    assert len(errors) == 1


def test_row_with_only_fio_is_accepted() -> None:
    rows, errors = parse(csv_text("...,Тестов Андрей Сергеевич,,,,,,,,,".replace("...", "")))
    assert not errors
    assert rows[0].full_name == "Тестов Андрей Сергеевич"


def test_row_with_only_contract_is_accepted() -> None:
    rows, errors = parse(csv_text(",,,,EV-999,,,,,,"))
    assert not errors
    assert rows[0].contract_number == "EV-999"


def test_row_without_name_or_contract_is_rejected() -> None:
    _rows, errors = parse(csv_text(",,,,,,5000,Москва,,,"))
    assert len(errors) == 1
    assert "fio" in errors[0].message


def test_invalid_amount_is_warned_not_fatal() -> None:
    rows, errors = parse(csv_text("DEM-1,Тестов Андрей Сергеевич,,,EV-1,,не-сумма,,,,"))
    assert not errors
    assert rows[0].debt_amount is None
    assert any("debt_amount" in warning for warning in rows[0].warnings)


def test_negative_amount_is_ignored() -> None:
    rows, _ = parse(csv_text("DEM-1,Тестов Андрей Сергеевич,,,EV-1,,-500,,,,"))
    assert rows[0].debt_amount is None


def test_invalid_optional_fields_are_warned() -> None:
    rows, errors = parse(
        csv_text("DEM-1,Тестов Андрей Сергеевич,31.02.1990,не-телефон,EV-1,,1,,XX999XX,SHORT,")
    )
    assert not errors
    row = rows[0]
    assert row.birth_date is None
    assert row.phone is None
    assert row.vehicle_plate is None
    assert row.vin is None
    assert len(row.warnings) == 4


def test_unparseable_name_is_kept_with_a_warning() -> None:
    rows, errors = parse(csv_text("DEM-1,ООО Рога и Копыта,,,EV-1,,1000,,,,"))
    assert not errors
    assert rows[0].full_name == "ООО Рога и Копыта"
    assert any("fio" in warning for warning in rows[0].warnings)


def test_column_aliases_are_recognized() -> None:
    text = "id;фио;телефон;договор;долг\nDEM-1;Тестов Андрей Сергеевич;+79991234501;EV-1;5000"
    rows, errors = parse(text)
    assert not errors
    assert rows[0].debtor_id == "DEM-1"
    assert rows[0].debt_amount == Decimal("5000")


def test_semicolon_delimiter_is_detected() -> None:
    rows, _ = parse(HEADER.replace(",", ";") + "\nDEM-1;Тестов Андрей Сергеевич;;;EV-1;;100;;;;")
    assert rows[0].debtor_id == "DEM-1"


def test_empty_file_is_rejected() -> None:
    with pytest.raises(CsvFormatError):
        list(iter_rows("   "))


def test_file_without_usable_columns_is_rejected() -> None:
    with pytest.raises(CsvFormatError):
        list(iter_rows("alpha,beta\n1,2"))


def test_file_without_fio_or_contract_column_is_rejected() -> None:
    with pytest.raises(CsvFormatError):
        list(iter_rows("debtor_id,address\nDEM-1,Москва"))


def test_cp1251_is_decoded() -> None:
    payload = csv_text("DEM-1,Тестов Андрей Сергеевич,,,EV-1,,100,,,,").encode("cp1251")
    assert "Тестов" in decode_csv_bytes(payload)


def test_undecodable_bytes_are_rejected() -> None:
    # 0x98 is invalid as UTF-8 and undefined in CP1251.
    with pytest.raises(CsvFormatError):
        decode_csv_bytes(b"\x98\x98\x98")


# ---------------------------------------------------------------- dedup keys


def test_debtor_id_drives_the_dedup_key() -> None:
    first = DebtorRow(debtor_id="DEM-1", full_name="Тестов Андрей Сергеевич")
    second = DebtorRow(debtor_id="DEM-1", full_name="Совсем Другой Человек")
    assert first.dedup_key == second.dedup_key


def test_composite_dedup_key_without_debtor_id() -> None:
    first = DebtorRow(
        full_name="Тестов Андрей Сергеевич",
        birth_date=date(1985, 3, 12),
        contract_number="EV-1",
    )
    same = DebtorRow(
        full_name="Тестов Андрей Сергеевич",
        birth_date=date(1985, 3, 12),
        contract_number="EV-1",
    )
    different = DebtorRow(
        full_name="Тестов Андрей Сергеевич",
        birth_date=date(1985, 3, 12),
        contract_number="EV-2",
    )
    assert first.dedup_key == same.dedup_key
    assert first.dedup_key != different.dedup_key


# ---------------------------------------------------------------- import


async def test_import_stores_rows(container: Container) -> None:
    report = await container.import_service.import_text(
        csv_text(
            "DEM-1,Тестов Андрей Сергеевич,12.03.1985,+79991234501,EV-1,ZA-1,100,,,,",
            "DEM-2,Демов Максим Игоревич,03.11.1990,,EV-2,ZA-2,200,,,,",
        )
    )
    assert report.total_rows == 2
    assert report.imported == 2
    assert report.created == 2
    assert report.failed == 0

    async with container.database.session() as session:
        assert await DebtorRepository(session).count() == 2


async def test_reimport_updates_instead_of_duplicating(container: Container) -> None:
    text = csv_text("DEM-1,Тестов Андрей Сергеевич,12.03.1985,,EV-1,ZA-1,100,,,,")
    await container.import_service.import_text(text)
    second = await container.import_service.import_text(text)

    assert second.created == 0
    assert second.updated == 1
    async with container.database.session() as session:
        assert await DebtorRepository(session).count() == 1


async def test_duplicates_within_one_file_collapse(container: Container) -> None:
    report = await container.import_service.import_text(
        csv_text(
            "DEM-1,Тестов Андрей Сергеевич,,,EV-1,,100,,,,",
            "DEM-1,Тестов Андрей Сергеевич,,,EV-1,,150,,,,",
        )
    )
    assert report.total_rows == 2
    assert report.skipped == 1
    async with container.database.session() as session:
        assert await DebtorRepository(session).count() == 1


async def test_import_counts_failures_without_stopping(container: Container) -> None:
    report = await container.import_service.import_text(
        csv_text(
            "DEM-1,Тестов Андрей Сергеевич,,,EV-1,,100,,,,",
            ",,,,,,777,Москва,,,",
            "DEM-2,Демов Максим Игоревич,,,EV-2,,200,,,,",
        )
    )
    assert report.imported == 2
    assert report.failed == 1
    assert report.errors


async def test_sparse_reimport_does_not_blank_existing_data(
    container: Container,
) -> None:
    """A later export missing a column must not erase what an earlier one had."""
    await container.import_service.import_text(
        csv_text("DEM-1,Тестов Андрей Сергеевич,12.03.1985,+79991234501,EV-1,ZA-1,100,Москва,,,")
    )
    await container.import_service.import_text(csv_text("DEM-1,,,,,,,,,,"))

    async with container.database.session() as session:
        rows = await DebtorRepository(session).find_by_external_id("DEM-1")
    assert rows[0].fio == "Тестов Андрей Сергеевич"
    assert rows[0].address == "Москва"


async def test_oversized_file_is_rejected(container: Container, settings: Settings) -> None:
    service = ImportService(
        settings.model_copy(update={"max_import_file_bytes": 1024}), container.database
    )
    with pytest.raises(CsvFormatError):
        await service.import_bytes(b"x" * 2048)


async def test_row_limit_is_enforced(container: Container, settings: Settings) -> None:
    service = ImportService(settings.model_copy(update={"max_import_rows": 2}), container.database)
    report = await service.import_text(
        csv_text(*[f"DEM-{i},Тестов Андрей Сергеевич,,,EV-{i},,100,,,," for i in range(5)])
    )
    assert report.imported <= 2
    assert report.failed >= 1


async def test_demo_csv_imports_cleanly(container: Container) -> None:
    report = await container.import_service.import_file(Path("data/demo_debtors.csv"))
    assert report.failed == 0
    assert report.imported == report.total_rows


async def test_messy_demo_csv_is_survivable(container: Container) -> None:
    """The messy fixture exercises blank lines, duplicates and bad values."""
    report = await container.import_service.import_file(Path("data/demo_debtors_messy.csv"))
    assert report.imported > 0
    assert report.skipped >= 1  # the duplicated row
    assert report.warnings  # the row with a bad date/phone/VIN
