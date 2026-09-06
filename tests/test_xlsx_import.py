"""Импорт выгрузки из Excel.

Проверяемое свойство: книга Excel даёт ровно тот же результат, что и CSV с теми
же данными. Заказчик выгружает из 1С в Excel, и расхождение между двумя путями
означало бы, что импорт «работает» на нашем файле и врёт на его.
"""

from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal
from typing import Any

import pytest
from openpyxl import Workbook

from app.container import Container
from app.providers.internal.csv_schema import CsvFormatError
from app.providers.internal.xlsx import (
    looks_like_legacy_xls,
    looks_like_xlsx,
    xlsx_to_csv_text,
    xlsx_to_sheet,
)
from app.services.import_service import ImportService


@pytest.fixture
def import_service(container: Container) -> ImportService:
    return container.import_service


def build_workbook(sheets: dict[str, list[list[Any]]]) -> bytes:
    workbook = Workbook()
    default_sheet = workbook.active
    if default_sheet is not None:
        workbook.remove(default_sheet)
    for title, rows in sheets.items():
        worksheet = workbook.create_sheet(title=title)
        for row in rows:
            worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_book_is_recognized_by_content_not_by_name() -> None:
    """Расширение приходит от отправителя и врёт; сигнатура — нет."""
    payload = build_workbook({"Лист": [["ФИО"], ["Иванов Иван Иванович"]]})
    assert looks_like_xlsx(payload)
    assert not looks_like_xlsx(b"fio,phone\n")


def test_legacy_xls_is_named_rather_than_parsed_as_garbage() -> None:
    """Старый .xls читать нечем — важно сказать это, а не «нет колонок»."""
    assert looks_like_legacy_xls(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32)
    assert not looks_like_legacy_xls(b"PK\x03\x04")


def test_data_sheet_is_chosen_by_header_not_by_position() -> None:
    """Выгрузка редко лежит первым листом.

    В присланном образце первым шёл README, а данные — вторым. Правило «берём
    первый лист» отвергло бы годный файл целиком.
    """
    payload = build_workbook(
        {
            "README": [["Назначение"], ["Синтетические данные для проверки"]],
            "Выгрузка_1С": [["ФИО", "Телефон"], ["Иванов Иван Иванович", "+79161234567"]],
            "Справочник": [["Ключ", "Значение"], ["x", "y"]],
        }
    )
    sheet = xlsx_to_sheet(payload)
    assert sheet.name == "Выгрузка_1С"
    assert "Иванов Иван Иванович" in sheet.text


def test_title_rows_above_the_header_are_skipped() -> None:
    """1С печатает сверху название отчёта и период — это не данные."""
    payload = build_workbook(
        {
            "Лист": [
                ["Реестр должников на 01.09.2026"],
                [],
                ["ФИО", "Телефон"],
                ["Иванов Иван Иванович", "+79161234567"],
            ]
        }
    )
    text = xlsx_to_csv_text(payload)
    assert text.splitlines()[0].startswith("ФИО")


def test_numbers_and_dates_keep_the_shape_the_parser_expects() -> None:
    """Типы Excel нельзя отдавать в str() как есть.

    Дата стала бы «1958-01-19 00:00:00», сумма — «26997.0», а телефон,
    введённый без плюса, — «89161234567.0», после чего поиск по номеру, главному
    ключу заказчика, перестал бы находить должника.
    """
    payload = build_workbook(
        {
            "Лист": [
                ["ФИО", "Дата_рождения", "Телефон", "Долг"],
                [
                    "Иванов Иван Иванович",
                    dt.datetime(1958, 1, 19),
                    89161234567,
                    Decimal("26997.00"),
                ],
            ]
        }
    )
    row = xlsx_to_csv_text(payload).splitlines()[1]
    assert "19.01.1958" in row
    assert "89161234567" in row
    assert "89161234567.0" not in row
    assert "26997" in row


def test_float_that_is_whole_does_not_grow_a_decimal_tail() -> None:
    payload = build_workbook({"Лист": [["ФИО", "Долг"], ["Иванов Иван", 13792.0]]})
    assert xlsx_to_csv_text(payload).splitlines()[1].endswith("13792")


def test_workbook_without_any_recognizable_sheet_is_refused() -> None:
    payload = build_workbook({"Один": [["альфа", "бета"], ["1", "2"]]})
    with pytest.raises(CsvFormatError, match="шапка"):
        xlsx_to_sheet(payload)


def test_corrupted_file_fails_with_words_not_a_traceback() -> None:
    with pytest.raises(CsvFormatError):
        xlsx_to_sheet(b"PK\x03\x04" + b"\x00" * 64)


@pytest.mark.asyncio
async def test_excel_and_csv_import_to_the_same_rows(import_service: ImportService) -> None:
    """Два пути ввода обязаны сойтись в одном результате."""
    rows: list[list[Any]] = [
        ["ФИО", "Телефон", "Дата_рождения", "Долг"],
        ["Иванов Иван Иванович", "+79161234567", dt.datetime(1958, 1, 19), 26997],
        ["Петрова Мария Сергеевна", "+79162110336", dt.datetime(1979, 7, 3), 13792],
    ]
    excel_report = await import_service.import_bytes(build_workbook({"Выгрузка": rows}))

    csv_text = "\n".join(
        [
            "ФИО,Телефон,Дата_рождения,Долг",
            "Иванов Иван Иванович,+79161234567,19.01.1958,26997",
            "Петрова Мария Сергеевна,+79162110336,03.07.1979,13792",
        ]
    )
    csv_report = await import_service.import_text(csv_text)

    assert excel_report.total_rows == csv_report.total_rows == 2
    assert excel_report.imported == csv_report.imported == 2
    assert excel_report.failed == csv_report.failed == 0


@pytest.mark.asyncio
async def test_import_report_names_the_sheet_it_read(import_service: ImportService) -> None:
    """При шести листах выбор делаем мы — оператор должен его видеть."""
    payload = build_workbook(
        {
            "README": [["Назначение"], ["описание"]],
            "Выгрузка_1С": [["ФИО", "Телефон"], ["Иванов Иван Иванович", "+79161234567"]],
        }
    )
    report = await import_service.import_bytes(payload)
    assert any("Выгрузка_1С" in warning for warning in report.warnings)


@pytest.mark.asyncio
async def test_legacy_xls_upload_explains_how_to_fix_it(import_service: ImportService) -> None:
    payload = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
    with pytest.raises(CsvFormatError, match="xlsx"):
        await import_service.import_bytes(payload)
