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


#: Колонки, без которых строка не шапка выгрузки должников. Ровно те же, что
#: требует :mod:`csv_schema`: пройти сюда и упасть там — худший из исходов,
#: потому что до настоящего листа дело уже не дойдёт.
_IDENTITY_COLUMNS = frozenset({"fio", "contract_number"})


@dataclass(frozen=True, slots=True)
class _Header:
    """Кандидат в шапку: где начинается, сколько строк занимает, чего стоит."""

    index: int
    height: int
    cells: list[str]
    recognized: int
    data_rows: int

    @property
    def rank(self) -> tuple[int, int]:
        """Чем больше распознано и чем больше данных под ним, тем лучше."""
        return (self.recognized, self.data_rows)


def _merge_tiers(rows: list[list[str]], index: int) -> tuple[list[str], int]:
    """Склеить двухъярусную шапку 1С в одну строку.

    Объединённые ячейки openpyxl отдаёт значением только в левой верхней, а
    остальные — пустыми, поэтому верхний ярус выглядит как «Должник, , Договор,
    ». Нижний ярус при этом несёт настоящие подписи колонок, и если его не
    приклеить, он становится первой строкой данных: в базе заводится должник по
    имени «Полностью», и на него в прогоне уходят платные запросы.

    Склеиваем только когда в верхнем ярусе есть пустые ячейки: заполненная
    целиком строка — это обычная шапка, и следующая за ней строка настоящие
    данные. Съесть их было бы хуже призрака.
    """
    top = rows[index]
    if index + 1 >= len(rows) or all(cell.strip() for cell in top):
        return top, 1

    below = rows[index + 1]
    if not any(cell.strip() for cell in below):
        return top, 1
    merged = [
        top[column] if column < len(top) and top[column].strip() else _at(below, column)
        for column in range(max(len(top), len(below)))
    ]
    return merged, 2


def _at(row: list[str], column: int) -> str:
    return row[column] if column < len(row) else ""


def _candidate(rows: list[list[str]], index: int) -> _Header | None:
    """Годится ли строка в шапку — и насколько.

    Три условия, и каждое стоит на конкретном разборе живого файла.

    **Есть ФИО или договор.** Без этого разбор всё равно откажет, но откажет
    поздно — когда лист уже выбран и настоящая выгрузка на соседнем листе
    осталась непрочитанной.

    **Распознано больше одной колонки** — если строка вообще не одноколоночная.
    Выгрузка 1С начинается блоком параметров, и строка «Контрагент | Все» с
    расширением словаря синонимов стала выглядеть шапкой: в базу уезжали
    должники с именами «Код» и «1», отчёт рапортовал «Импортировано: 3,
    ошибок 0». Одна распознанная подпись среди прочего текста — совпадение, а не
    шапка.

    **Под шапкой есть данные.** Лист описания полей — «Поле | Группа | Тип» —
    иначе выигрывал своей же строкой данных, где в первой колонке стоит слово
    «ФИО», и книга заказчика давала 84 выдуманных должника вместо отказа.
    """
    cells, height = _merge_tiers(rows, index)
    recognized = {normalize_header(cell) for cell in cells if cell.strip()}
    recognized.discard(None)
    filled = sum(1 for cell in cells if cell.strip())
    if not recognized & _IDENTITY_COLUMNS:
        return None
    if len(recognized) < 2 and filled > 1:
        return None
    data_rows = sum(1 for row in rows[index + height :] if any(cell.strip() for cell in row))
    if not data_rows:
        return None
    return _Header(
        index=index,
        height=height,
        cells=cells,
        recognized=len(recognized),
        data_rows=data_rows,
    )


def _find_header(rows: list[list[str]]) -> _Header | None:
    """Лучший кандидат в шапку, а не первый попавшийся.

    «Первый, где распозналась хоть одна колонка» ломался с обеих сторон:
    служебная строка выше настоящей шапки перехватывала её, а верхний ярус
    двухъярусной шапки — нижний. Оценка сравнивает кандидатов между собой, и при
    равенстве побеждает нижний: группирующий ярус 1С всегда стоит сверху.
    """
    best: _Header | None = None
    for index in range(min(len(rows), _MAX_HEADER_SCAN_ROWS)):
        candidate = _candidate(rows, index)
        if candidate is None:
            continue
        if best is None or candidate.rank >= best.rank:
            best = candidate
    return best


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

        # Лист тоже выбирается по лучшей оценке, а не по первому подходящему.
        # Рядом с выгрузкой в книге лежат справочники и описание полей, и любой
        # из них, оказавшись первым, забирал книгу себе.
        best: tuple[tuple[int, int], str, list[list[str]], _Header] | None = None
        for worksheet in workbook.worksheets:
            rows = [
                [_render_cell(cell) for cell in raw]
                for raw in worksheet.iter_rows(values_only=True)
            ]
            # Хвостовые пустые строки Excel хранит наравне с заполненными: лист,
            # где однажды что-то стёрли, тянет за собой тысячи пустых.
            while rows and not any(cell.strip() for cell in rows[-1]):
                rows.pop()
            header = _find_header(rows) if rows else None
            if header is None:
                continue
            if best is None or header.rank > best[0]:
                best = (header.rank, worksheet.title, rows, header)

        if best is not None:
            _, title, rows, header = best
            buffer = io.StringIO()
            writer = csv.writer(buffer, lineterminator="\n")
            writer.writerow(header.cells)
            for row in rows[header.index + header.height :]:
                writer.writerow(row)
            return XlsxSheet(name=title, text=buffer.getvalue())
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
