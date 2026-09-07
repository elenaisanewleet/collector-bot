"""Шапка выгрузки 1С: что распознано, что выброшено и что об этом сказано.

Проверяемое свойство одно и то же во всех тестах файла: импорт не имеет права
терять данные молча. Непонятая колонка, схлопнутые однофамильцы, три сотни
одинаковых замечаний и итоговая строка отчёта — всё это должно быть видно в
отчёте оператору, потому что по нему решают, идти ли в суд.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.bot.handlers.import_csv import render_import_report
from app.container import Container
from app.db.repository import DebtorRepository
from app.providers.internal.csv_schema import normalize_header, parse_header

# ---------------------------------------------------------------- синонимы


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        # Развёрнутые названия — самые частые в выгрузке из 1С.
        ("ФИО должника", "fio"),
        ("Должник", "fio"),
        ("Контрагент", "fio"),
        ("Сумма долга", "debt_amount"),
        ("Сумма задолженности", "debt_amount"),
        ("Номер договора", "contract_number"),
        ("Номер телефона", "phone"),
        # Сокращения с точкой и «№».
        ("Ф.И.О.", "fio"),
        ("Гос. номер", "vehicle_plate"),
        ("№ договора", "contract_number"),
        ("Договор №", "contract_number"),
        ("Тел.", "phone"),
        # Слитная запись 1С и пунктуация внутри.
        ("СуммаДолга", "debt_amount"),
        ("ФИОДолжника", "fio"),
        ("ДатаРождения", "birth_date"),
        # Пробелы: повторные и неразрывный.
        ("ФИО  должника", "fio"),
        ("ФИО\xa0должника", "fio"),
        (" Долг ", "debt_amount"),
    ],
)
def test_1c_spellings_are_recognized(header: str, expected: str) -> None:
    assert normalize_header(header) == expected


def test_yo_and_case_do_not_change_the_column() -> None:
    """«ё» в шапке — типографика, а не другое поле."""
    assert normalize_header("АДРЕС ПРОЖИВАНИЯ") == "address"
    assert normalize_header("Сумма задолженности") == normalize_header("СУММА ЗАДОЛЖЕННОСТИ")


def test_ambiguous_sum_is_not_guessed() -> None:
    """Голая «Сумма» — это и оплата, и госпошлина, и начисление.

    Взять её за долг молча — принести в суд чужую цифру. Не взять — показать
    «Сумма» в списке нераспознанных, где оператор её увидит.
    """
    assert normalize_header("Сумма") is None
    assert normalize_header("СуммаЭвакуации") is None


# ---------------------------------------------------------------- шапка


def test_unrecognized_headers_are_listed_not_dropped() -> None:
    mapping = parse_header(["ФИО должника", "Сумма долга", "Комментарий", "Ответственный"])
    assert mapping.known == {"fio", "debt_amount"}
    assert mapping.unknown == ("Комментарий", "Ответственный")


def test_second_column_for_the_same_field_loses_to_the_first() -> None:
    """Побеждала последняя, и «Адрес регистрации» затирал «Адрес» без следа."""
    mapping = parse_header(["ФИО", "Адрес", "Адрес регистрации"])
    assert mapping.columns == {0: "fio", 1: "address", 2: None}
    assert mapping.duplicates == ("«Адрес регистрации» дублирует «Адрес»",)


async def test_dropped_columns_reach_the_operator(container: Container) -> None:
    """Главная починка: словарь синонимов всегда неполон, и это должно быть видно.

    Раньше непонятая колонка исчезала, а отчёт рапортовал «импортировано 1,
    ошибок 0» — по этому отчёту нельзя было заметить, что должник уехал в базу
    без имени.
    """
    report = await container.import_service.import_text(
        "Договор,Наниматель,Начислено всего\nЭВ-1,Тестов Андрей Сергеевич,5000"
    )
    assert report.imported == 1
    assert report.unknown_columns == ["Наниматель", "Начислено всего"]

    message = render_import_report(report)
    assert "Не распознаны колонки (2)" in message
    assert "Наниматель" in message


async def test_expanded_names_bring_fio_and_debt_into_the_database(
    container: Container,
) -> None:
    """«ФИО должника» и «Сумма долга» раньше выбрасывались целиком.

    Импорт при этом не падал — распознавался «Договор», — и массовый прогон
    строил поиск по договору вместо поиска по человеку.
    """
    report = await container.import_service.import_text(
        "Номер договора;ФИО должника;Сумма долга;Номер телефона\n"
        "ЭВ-1;Тестов Андрей Сергеевич;38 400,00;+7 (999) 123-45-01"
    )
    assert report.imported == 1
    assert not report.unknown_columns

    async with container.database.session() as session:
        rows = await DebtorRepository(session).find_by_fio("Тестов Андрей Сергеевич")
    assert rows[0].fio == "Тестов Андрей Сергеевич"
    assert rows[0].debt_amount == Decimal("38400")
    assert rows[0].contract_number == "ЭВ-1"


# ---------------------------------------------------------------- однофамильцы


async def test_namesakes_are_not_merged_into_one_debtor(container: Container) -> None:
    """Два разных человека с одним ФИО — две записи, а не «Пропущено: 1»."""
    report = await container.import_service.import_text(
        "ФИО,Телефон,Сумма долга\n"
        "Тестов Андрей Сергеевич,+79991234501,1000\n"
        "Тестов Андрей Сергеевич,+79991234502,2000"
    )
    assert report.imported == 2
    assert report.skipped == 0
    async with container.database.session() as session:
        assert await DebtorRepository(session).count() == 2


async def test_indistinguishable_namesakes_collapse_but_are_named(
    container: Container,
) -> None:
    """Различить нечем — схлопываем, но не молча: это может быть тёзка."""
    report = await container.import_service.import_text(
        "ФИО,Сумма долга\nТестов Андрей Сергеевич,1000\nТестов Андрей Сергеевич,2000"
    )
    assert report.imported == 1
    assert report.skipped == 1
    assert report.collapsed_conflicts == ["строки 2 и 3: «Тестов Андрей Сергеевич»"]
    assert "не однофамильцы ли это" in render_import_report(report)


async def test_a_real_duplicate_stays_a_quiet_duplicate(container: Container) -> None:
    """Полностью совпавшая строка — обычный повтор, поднимать тревогу не о чем."""
    report = await container.import_service.import_text(
        "ФИО,Телефон,Сумма долга\n"
        "Тестов Андрей Сергеевич,+79991234501,1000\n"
        "Тестов Андрей Сергеевич,+79991234501,1000"
    )
    assert report.skipped == 1
    assert report.collapsed_conflicts == []


# ---------------------------------------------------------------- счётчики


async def test_repeated_warnings_are_grouped_and_counted(container: Container) -> None:
    """Потеря телефона у всех должников — не «пять мелких замечаний»."""
    rows = "\n".join(
        f"Тестов Андрей Сергеевич,ЭВ-{index},абонент недоступен" for index in range(12)
    )
    report = await container.import_service.import_text(f"ФИО,Договор,Телефон\n{rows}")

    assert report.warning_count == 12
    message = render_import_report(report)
    assert "Предупреждения (всего 12)" in message
    assert "Телефон: не распознан российский номер — строк: 12" in message


async def test_error_section_shows_the_total_not_the_first_five(
    container: Container,
) -> None:
    rows = "\n".join(",,1000" for _ in range(9))
    report = await container.import_service.import_text(f"ФИО,Договор,Сумма долга\n{rows}")

    assert report.failed == 9
    message = render_import_report(report)
    assert "Ошибочные строки (всего 9)" in message
    assert "строк: 9" in message


# ---------------------------------------------------------------- итоги отчёта


async def test_totals_row_is_not_a_debtor(container: Container) -> None:
    """«Итого» из отчёта 1С — не должник и не ошибка, но и не тишина."""
    report = await container.import_service.import_text(
        "ФИО,Договор,Сумма долга\n"
        "Тестов Андрей Сергеевич,ЭВ-1,1000\n"
        "Демов Максим Игоревич,ЭВ-2,2000\n"
        "ИТОГО:,,3000"
    )
    assert report.total_rows == 2
    assert report.imported == 2
    assert report.failed == 0
    assert report.ignored_totals == 1
    assert any("итоговых строк" in note for note in report.notes)

    async with container.database.session() as session:
        assert await DebtorRepository(session).count() == 2


async def test_totals_row_is_recognized_in_the_first_filled_column(
    container: Container,
) -> None:
    """В выгрузке «Итого» стоит не в первой колонке, а в первой заполненной."""
    report = await container.import_service.import_text(
        "Код,ФИО,Сумма долга\nDEM-1,Тестов Андрей Сергеевич,1000\n,Всего,1000"
    )
    assert report.imported == 1
    assert report.ignored_totals == 1
