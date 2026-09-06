"""Чтение выгрузки из Excel.

Из 1С выгружают в Excel, а не в CSV: «Сохранить как» в списке должников даёт
xlsx, и заказчик присылает именно его. Требовать пересохранения в CSV — значит
переложить на оператора шаг, на котором ломаются кодировки и разделители, и
получить в ответ файл, который мы же и не прочтём.

Модуль ничего не разбирает по смыслу: он превращает лист в тот же CSV-текст,
который читает :mod:`csv_schema`. Вся валидация, синонимы колонок и подсчёт
ошибочных строк остаются в одном месте — иначе Excel и CSV разъедутся в
поведении, и правило про «не проверено ≠ не найдено» пришлось бы держать
дважды.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import zipfile
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.providers.internal.csv_schema import CsvFormatError, normalize_header

# xlsx — это zip-архив; xls — устаревший двоичный контейнер OLE2. Их путают
# постоянно, потому что 1С предлагает оба, а расширение пользователь правит
# руками. Различаем по сигнатуре, а не по имени файла.
_ZIP_MAGIC = b"PK\x03\x04"
_LEGACY_XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"

# Шапка в выгрузках 1С редко стоит первой строкой: сверху бывает название
# отчёта, период и пустые строки. Ищем строку с распознаваемыми колонками, но
# не бесконечно — иначе файл без шапки вовсе будет прочитан как данные.
_MAX_HEADER_SCAN_ROWS = 25

LEGACY_XLS_MESSAGE = (
    "Это файл старого формата Excel (.xls). Откройте его в Excel и сохраните "
    "как «Книга Excel (.xlsx)» или «CSV UTF-8», затем пришлите снова."
)


def looks_like_xlsx(payload: bytes) -> bool:
    """Похож ли загруженный файл на книгу Excel.

    Проверяем содержимое, а не расширение: Telegram отдаёт то имя, которое было
    на диске у отправителя, и «выгрузка.csv» с книгой внутри — обычное дело.
    """
    return payload[: len(_ZIP_MAGIC)] == _ZIP_MAGIC


def looks_like_legacy_xls(payload: bytes) -> bool:
    """Старый двоичный .xls, который openpyxl не читает принципиально."""
    return payload[: len(_LEGACY_XLS_MAGIC)] == _LEGACY_XLS_MAGIC


def _render_cell(value: Any) -> str:
    """Значение ячейки в том виде, в каком его ждёт разбор CSV.

    Excel хранит числа и даты типами, а не текстом, и наивный ``str()`` портит
    ровно те поля, по которым идёт поиск. Дата приезжает как ``datetime`` и без
    приведения станет «1985-03-12 00:00:00»; сумма долга — как ``float`` и
    станет «13792.0»; телефон, введённый без плюса, Excel считает числом, и
    ``str()`` даёт «89991234501.0», после чего нормализация телефона его не
    узнаёт и должник не находится по номеру — главному ключу заказчика.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        # Отдельно от int: bool — подкласс int, и «Да/Нет» в выгрузке иначе
        # превратились бы в «1/0».
        return "да" if value else "нет"
    if isinstance(value, dt.datetime):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, dt.date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, float):
        # Целое, записанное как float, обязано остаться целым: это и суммы, и
        # телефоны, и ИНН. Дробную часть сохраняем, если она есть.
        if value.is_integer():
            return str(int(value))
        return format(Decimal(str(value)).normalize(), "f")
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    return str(value).strip()


def _find_header(rows: list[list[str]]) -> int | None:
    """Индекс строки с шапкой либо ``None``.

    Берётся первая строка, где распознана хоть одна колонка. Название отчёта и
    период, которые 1С печатает сверху, распознанных колонок не содержат и
    отсеиваются сами.
    """
    for index, row in enumerate(rows[:_MAX_HEADER_SCAN_ROWS]):
        if any(cell and normalize_header(cell) for cell in row):
            return index
    return None


@dataclass(frozen=True, slots=True)
class XlsxSheet:
    """Прочитанный лист: имя — чтобы оператор знал, что именно импортировано."""

    name: str
    text: str


def xlsx_to_sheet(payload: bytes) -> XlsxSheet:
    """Лист с данными — в CSV-текст, который дальше читает разбор CSV.

    Лист выбирается по шапке, а не по номеру. Книга от заказчика — не одна
    таблица: рядом с выгрузкой лежат README, описание полей, справочники. В
    присланном образце данные были на втором листе, а первым шёл README, и
    правило «берём первый лист» отвергло бы совершенно годный файл.

    Склеивать листы нельзя: у справочников свои шапки, объединение дало бы
    строки вперемешку. Берётся первый подходящий.
    """
    try:
        import openpyxl
    except ModuleNotFoundError as exc:  # pragma: no cover - зависимость объявлена
        raise CsvFormatError(
            "Чтение Excel недоступно в этой сборке. Пришлите файл в формате CSV."
        ) from exc

    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(payload),
            read_only=True,
            data_only=True,  # формулы — их посчитанные значения, а не «=A1*2»
        )
    except zipfile.BadZipFile as exc:
        raise CsvFormatError("Файл повреждён и не читается как книга Excel.") from exc
    except Exception as exc:
        raise CsvFormatError(
            "Не удалось прочитать файл Excel. Пересохраните его как «Книга Excel "
            "(.xlsx)» или «CSV UTF-8»."
        ) from exc

    try:
        if not workbook.worksheets:
            raise CsvFormatError("В книге нет ни одного листа.")

        for worksheet in workbook.worksheets:
            rows = [
                [_render_cell(cell) for cell in raw]
                for raw in worksheet.iter_rows(values_only=True)
            ]
            # Хвостовые пустые строки Excel хранит наравне с заполненными: лист,
            # где однажды что-то стёрли, тянет за собой тысячи пустых.
            while rows and not any(cell.strip() for cell in rows[-1]):
                rows.pop()
            header_index = _find_header(rows) if rows else None
            if header_index is None:
                continue

            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            for row in rows[header_index:]:
                writer.writerow(row)
            return XlsxSheet(name=worksheet.title, text=buffer.getvalue())
    finally:
        # read_only держит открытым файловый дескриптор внутри zip.
        workbook.close()

    raise CsvFormatError(
        "Ни на одном листе не распознана шапка. Проверьте, что есть колонки, "
        "например: ФИО, телефон, договор."
    )


def xlsx_to_csv_text(payload: bytes) -> str:
    """Текст листа без его имени — для вызовов, которым имя не нужно."""
    return xlsx_to_sheet(payload).text
