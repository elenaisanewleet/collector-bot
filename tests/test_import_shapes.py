"""Формы файла, на которых импорт врал.

Все случаи здесь взяты не из головы: каждый воспроизведён на живом xlsx, и
каждый до правки давал «Импортировано: N, ошибок 0» с выдуманными или
потерянными должниками. Это худший из возможных исходов — «не проверено»,
неотличимое от «чисто», и вдобавок платные запросы на несуществующих людей.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from openpyxl import Workbook

from app.container import Container
from app.providers.internal.csv_schema import CsvFormatError
from app.providers.internal.xlsx import xlsx_to_sheet
from app.services.import_service import ImportService


@pytest.fixture
def import_service(container: Container) -> ImportService:
    return container.import_service


def build(sheets: dict[str, list[list[Any]]]) -> bytes:
    workbook = Workbook()
    default = workbook.active
    if default is not None:
        workbook.remove(default)
    for title, rows in sheets.items():
        worksheet = workbook.create_sheet(title=title)
        for row in rows:
            worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_the_parameter_block_above_the_header_is_not_the_header() -> None:
    """Выгрузка 1С начинается с блока параметров, и он похож на шапку.

    «Контрагент | Все» — после расширения словаря синонимов «Контрагент»
    распознаётся как ФИО, и строка проходила за шапку. В базу уезжали должники
    с именами «Код» и «1», отчёт рапортовал об успехе.
    """
    payload = build(
        {
            "TDSheet": [
                ["Отчет по должникам"],
                ["Период", "01.01.2026 - 31.12.2026"],
                ["Контрагент", "Все"],
                [],
                ["Код", "ФИО должника", "Договор", "Сумма долга"],
                ["1", "Иванов Иван Иванович", "ЭВ-1", "15000"],
                ["2", "Петров Пётр Петрович", "ЭВ-2", "20000"],
            ]
        }
    )

    sheet = xlsx_to_sheet(payload)

    assert sheet.text.splitlines()[0] == "Код,ФИО должника,Договор,Сумма долга"
    assert "Контрагент" not in sheet.text


def test_a_description_sheet_never_beats_the_export() -> None:
    """Лист описания полей выигрывал своей же строкой данных.

    В книге заказчика есть лист «Справочник_полей» с колонками «Поле | Группа |
    Тип», и в первой колонке его данных стоит слово «ФИО». Строка проходила за
    шапку, и книга давала 84 выдуманных должника — «Фамилия», «Имя», «Отчество» —
    вместо отказа. Каждый из них в прогоне стоит пяти платных запросов.
    """
    payload = build(
        {
            "Справочник_полей": [
                ["Поле", "Группа", "Тип"],
                ["ФИО", "Физлицо", "строка"],
                ["Телефон", "Физлицо", "строка"],
            ],
            "TDSheet": [
                ["ФИО должника", "Телефон мобильный", "Номер договора"],
                ["Иванов Иван Иванович", "89991234501", "ЭВ-1"],
            ],
        }
    )

    sheet = xlsx_to_sheet(payload)

    assert sheet.name == "TDSheet"
    assert "Иванов Иван Иванович" in sheet.text
    assert "Физлицо" not in sheet.text


def test_a_reference_sheet_never_beats_the_export() -> None:
    """Справочник адресов распознаётся колонками «Код» и «Адрес»."""
    payload = build(
        {
            "Справочник": [["Код", "Адрес"], ["1", "г. Москва"]],
            "TDSheet": [["ФИО", "Телефон"], ["Иванов Иван Иванович", "89991234501"]],
        }
    )

    assert xlsx_to_sheet(payload).name == "TDSheet"


def test_a_two_tier_header_does_not_leave_a_ghost_debtor() -> None:
    """Объединённые ячейки в шапке 1С — обычное дело.

    Верхний ярус выигрывал, нижний становился первой строкой данных, и в базе
    заводился должник по имени «ФИО» или «Полностью». В прогоне на него уходили
    платные запросы, а в очереди взыскания он стоял наравне с настоящими.
    """
    payload = build(
        {
            "Лист": [
                ["Должник", None, "Договор", None],
                ["ФИО", "Телефон", "Номер", "Дата"],
                ["Иванов Иван", "89991234501", "Д-1", "01.01.2020"],
            ]
        }
    )

    lines = xlsx_to_sheet(payload).text.splitlines()

    assert len(lines) == 2, "нижний ярус шапки уехал в данные"
    assert lines[1].startswith("Иванов Иван")


def test_a_two_tier_header_whose_lower_row_has_no_known_labels() -> None:
    """Нижний ярус может не нести ни одной знакомой подписи.

    Тогда его всё равно надо приклеить, а не считать данными: «Полностью» —
    не должник.
    """
    payload = build(
        {
            "Лист": [
                ["ФИО", None, "Договор", None],
                ["Полностью", "Мобильный", "Номер", "Дата"],
                ["Иванов Иван", "89991234501", "Д-1", "01.01.2020"],
            ]
        }
    )

    lines = xlsx_to_sheet(payload).text.splitlines()

    assert len(lines) == 2
    assert "Полностью" not in lines[1]


def test_a_plain_header_does_not_swallow_the_first_debtor() -> None:
    """Обратная сторона склейки ярусов: съесть строку данных хуже призрака."""
    payload = build(
        {
            "Лист": [
                ["ФИО", "Телефон"],
                ["Иванов Иван Иванович", "89991234501"],
                ["Петров Пётр Петрович", "89991234502"],
            ]
        }
    )

    lines = xlsx_to_sheet(payload).text.splitlines()

    assert len(lines) == 3
    assert "Иванов Иван Иванович" in lines[1]


def test_a_header_without_data_below_it_is_not_a_header() -> None:
    payload = build({"Лист": [["ФИО", "Телефон"]]})

    with pytest.raises(CsvFormatError):
        xlsx_to_sheet(payload)


@pytest.mark.asyncio
async def test_re_importing_with_one_more_column_updates_instead_of_doubling(
    import_service: ImportService,
) -> None:
    """Обещание модуля: повторный импорт обновляет, а не множит.

    Оно сломалось, когда ради однофамильцев в ключ свалили все опознавательные
    поля разом: та же выгрузка с дописанным телефоном давала вторую запись, хотя
    номер договора в обеих строках стоял один.
    """
    await import_service.import_text("ФИО,Договор,Телефон\nИванов Иван Иванович,ЭВ-1,")

    again = await import_service.import_text(
        "ФИО,Договор,Телефон\nИванов Иван Иванович,ЭВ-1,+79990001122"
    )

    assert again.created == 0
    assert again.updated == 1


@pytest.mark.asyncio
async def test_a_sparser_export_does_not_create_a_second_debtor(
    import_service: ImportService,
) -> None:
    await import_service.import_text("ФИО,Договор,Телефон\nПетров Пётр,ЭВ-2,+79161234500")

    again = await import_service.import_text("ФИО,Договор\nПетров Пётр,ЭВ-2")

    assert again.created == 0
    assert again.updated == 1


@pytest.mark.asyncio
async def test_namesakes_without_a_contract_are_still_kept_apart(
    import_service: ImportService,
) -> None:
    """Лестница ключа не должна вернуть склейку однофамильцев.

    Когда о человеке известно одно ФИО, различить «тот же с дописанным
    телефоном» и «полный тёзка» нечем. Выбор между видимой лишней строкой и
    невидимо слитыми людьми решается в пользу строки: слитый должник уносит в
    суд чужие долги.
    """
    report = await import_service.import_text(
        "ФИО,Телефон\nОдинаков Иван Иванович,+79990000001\nОдинаков Иван Иванович,+79990000002"
    )

    assert report.imported == 2
    assert report.skipped == 0
