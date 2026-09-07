"""Разбор свободной строки, которую оператор набрал про должника.

Юнит-тесты без контейнера, БД и провайдеров: :mod:`app.bot.identifiers` — чистая
функция от строки, и именно поэтому её можно проверить на всех опасных случаях
разом.

Опасных здесь три рода, и каждый из них однажды уже ломал разбор.

*   **Границы слова.** «Котельников» содержит «тел», «Иннокентьев» — «инн».
    Метка, которую ищут подстрокой, отдаёт паспорт под видом телефона.
*   **Счёт цифр по всей строке, а не по группе.** «Иванов Иван Иванович
    01.01.1985 770912345601» — четыре слова, и строгий разбор ФИО отвергал всю
    строку целиком.
*   **Молчаливое угадывание.** «15.13.1985» проглатывалось как «даты нет», а
    десять цифр с девятки записывались телефоном — и вход в мост «паспорт →
    ИНН» закрывался навсегда, без единого слова оператору.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.bot import identifiers
from app.bot.identifiers import ProblemKind, TenDigits, parse_query

# ---------------------------------------------------------------- цельные строки


def test_name_and_birth_date() -> None:
    parsed = parse_query("Иванов Иван Иванович 01.01.1985")

    assert parsed.name is not None
    assert parsed.name.full == "Иванов Иван Иванович"
    assert parsed.birth_date == date(1985, 1, 1)
    assert parsed.problems == ()


def test_name_birth_date_and_inn() -> None:
    """Случай из жалобы: раньше падал с «Слишком много слов»."""
    parsed = parse_query("Иванов Иван Иванович 01.01.1985 770912345601")

    assert parsed.name is not None
    assert parsed.name.full == "Иванов Иван Иванович"
    assert parsed.birth_date == date(1985, 1, 1)
    assert parsed.inn == "770912345601"
    assert parsed.name_error is None


def test_name_birth_date_and_phone() -> None:
    parsed = parse_query("Иванов Иван Иванович 01.01.1985 +79161234567")

    assert parsed.name is not None
    assert parsed.birth_date == date(1985, 1, 1)
    assert parsed.phone == "+79161234567"


def test_two_word_name_with_inn() -> None:
    """Отчество необязательно: в NewDB ``secondname`` — опциональное поле."""
    parsed = parse_query("Петров Пётр 770912345601")

    assert parsed.name is not None
    assert parsed.name.full == "Петров Пётр"
    assert parsed.name.middle_name is None
    assert parsed.inn == "770912345601"


def test_inn_alone_is_a_valid_query_not_a_missing_name() -> None:
    """По двенадцати цифрам ищут банкротство, ИП и арбитраж.

    Требовать к ним ещё и фамилию — вернуть тот самый допрос, поэтому пустое
    имя здесь не ошибка, а законный результат.
    """
    parsed = parse_query("ИНН 770912345601")

    assert parsed.inn == "770912345601"
    assert parsed.name is None
    assert parsed.name_error is None
    assert parsed.runnable


def test_bare_inn_without_a_label() -> None:
    assert parse_query("770912345601").inn == "770912345601"


def test_name_alone() -> None:
    parsed = parse_query("Сидоров Сидор Сидорович")

    assert parsed.name is not None
    assert parsed.name.full == "Сидоров Сидор Сидорович"
    assert parsed.inn is None
    assert parsed.runnable


# ---------------------------------------------------------------- метки и группы


def test_two_labelled_identifiers() -> None:
    parsed = parse_query("инн 770912345601 паспорт 4515384710")

    assert parsed.inn == "770912345601"
    assert parsed.passport == "4515384710"


def test_a_run_of_digits_splits_when_the_join_means_nothing() -> None:
    """Двадцать две цифры подряд — это ИНН и паспорт, а не одно число."""
    parsed = parse_query("770912345601 4515384710")

    assert parsed.inn == "770912345601"
    assert parsed.passport == "4515384710"


@pytest.mark.parametrize(
    ("line", "field", "value"),
    [
        ("Котельников Иван 4515384710", "passport", "4515384710"),
        ("Иннокентьев Пётр Иванович 4515384710", "passport", "4515384710"),
        ("Линник Иван Петрович 89160000000", "phone", "+79160000000"),
    ],
)
def test_labels_are_matched_as_whole_words(line: str, field: str, value: str) -> None:
    """«тел» внутри «Котельников» и «инн» внутри «Иннокентьев» — не метки."""
    parsed = parse_query(line)

    assert getattr(parsed, field) == value
    assert parsed.name is not None


def test_entity_inn_under_a_label_is_refused_out_loud() -> None:
    parsed = parse_query("ИНН 7709123456")

    assert parsed.inn is None
    problem = parsed.problem(ProblemKind.ENTITY_INN)
    assert problem is not None
    assert "7709123456" in problem.text


def test_eleven_digits_from_seven_read_as_a_phone() -> None:
    """ИНН с потерянной цифрой неотличим от мобильного — принятый риск.

    Различить нечем: одиннадцать цифр с семёрки — валидный номер. Спасает эхо
    «Принял: … телефон» в карточке, а не догадка здесь.
    """
    assert parse_query("77091234560").phone == "+77091234560"


# ---------------------------------------------------------------- даты


@pytest.mark.parametrize("token", ["15.13.1985", "01.01.2030", "20250101"])
def test_a_broken_date_is_never_swallowed(token: str) -> None:
    """Дату оператор дал. Выбросить её молча — соврать дважды.

    Без даты рождения ФССП и залоги не ищут вовсе, и отчёт вышел бы с двумя
    пустыми разделами по вине опечатки, которую оператор считает исправленной.
    """
    parsed = parse_query(f"Иванов Иван Иванович {token}")

    assert parsed.birth_date is None
    problem = parsed.problem(ProblemKind.BAD_DATE)
    assert problem is not None
    assert problem.token == token
    assert parsed.name is not None


def test_bad_dates_are_explained_by_their_own_reason() -> None:
    assert "месяца" in identifiers.describe_bad_date("15.13.1985")
    assert "будущем" in identifiers.describe_bad_date("01.01.2030")
    assert "1900" in identifiers.describe_bad_date("01.01.1800")


def test_single_digit_day_and_month() -> None:
    assert parse_query("Иванов Иван Иванович 1.1.1985").birth_date == date(1985, 1, 1)


def test_the_birth_date_marker_is_stripped_and_does_not_land_in_the_name() -> None:
    parsed = parse_query("Тестов Андрей Сергеевич 01.01.1985 г.р.")

    assert parsed.birth_date == date(1985, 1, 1)
    assert parsed.name is not None
    assert parsed.name.full == "Тестов Андрей Сергеевич"


def test_a_name_starting_with_gr_survives_the_marker() -> None:
    """«01.01.1985 Гришин» не должно терять «Гр» из фамилии."""
    parsed = parse_query("01.01.1985 Гришин Иван")

    assert parsed.name is not None
    assert parsed.name.last_name == "Гришин"


def test_commas_and_semicolons_are_noise() -> None:
    parsed = parse_query("Иванов, Иван, Иванович; 01.01.1985")

    assert parsed.name is not None
    assert parsed.name.full == "Иванов Иван Иванович"
    assert parsed.birth_date == date(1985, 1, 1)


# ---------------------------------------------------------------- десять цифр


def test_ten_digits_from_nine_are_asked_about_not_guessed() -> None:
    """Угадать здесь — необратимо.

    Угаданный «телефон» закрывает единственный вход в мост «паспорт → ИНН»,
    угаданный «паспорт» отправляет мобильный в ФНС за деньги.
    """
    parsed = parse_query("9204384710")

    assert parsed.ambiguity is not None
    assert parsed.ambiguity.token == "9204384710"
    assert parsed.passport is None
    assert parsed.phone is None


def test_a_passport_grouping_resolves_itself() -> None:
    parsed = parse_query("4515 384710")

    assert parsed.passport == "4515384710"
    assert parsed.ambiguity is None


def test_the_operators_answer_resolves_the_ambiguity() -> None:
    assert parse_query("9204384710", ten_digits_as=TenDigits.PASSPORT).passport == "9204384710"
    assert parse_query("9204384710", ten_digits_as=TenDigits.PHONE).phone == "+79204384710"


@pytest.mark.parametrize("line", ["8 (916) 000-00-00", "+7 916 000 00 00", "8-916-000-00-00"])
def test_phone_shapes_normalize(line: str) -> None:
    assert parse_query(line).phone == "+79160000000"


# ---------------------------------------------------------------- машина и мусор


def test_plate_and_vin() -> None:
    assert parse_query("А123ВС77").plate == "А123ВС77"
    assert parse_query("XW8ZZZ61ZKG011111").vin == "XW8ZZZ61ZKG011111"


def test_an_unparseable_name_is_reported_not_raised() -> None:
    """Четыре слова без известной частицы разбор не берёт. Это ограничение, а не
    падение: ФИО уходит в остаток, и оператор видит, что именно не понято."""
    parsed = parse_query("Тестов Андрей Сергеевич Петрович")

    assert parsed.name is None
    assert parsed.name_error
    assert parsed.leftover == ("Тестов", "Андрей", "Сергеевич", "Петрович")


@pytest.mark.parametrize("raw", ["", None, "   ", "asdf", "?", "🙂🙂", "!!!", "—"])
def test_garbage_never_raises(raw: str | None) -> None:
    parsed = parse_query(raw)

    assert not parsed.runnable
    assert parsed.birth_date is None
    assert parsed.ambiguity is None


def test_empty_input_is_not_an_error() -> None:
    parsed = parse_query("")

    assert parsed.name_error is None
    assert parsed.problems == ()
    assert parsed.leftover == ()


def test_unknown_digits_are_named_rather_than_dropped() -> None:
    parsed = parse_query("Иванов Иван Иванович 12345")

    assert parsed.name is not None
    problem = parsed.problem(ProblemKind.UNKNOWN_DIGITS)
    assert problem is not None
    assert "12345" in problem.text


# ---------------------------------------------------------------- регрессии


def test_the_dead_bridge_flag_stays_dead() -> None:
    """``passport_bridge_ready`` возвращала False всегда — флаг никто не ставил.

    Кнопка «узнать ИНН по паспорту» на ней не появлялась бы никогда. Источник
    истины — сам мост (``InnBridgeProvider.will_query``), и воскрешать флаг при
    слиянии нельзя.
    """
    assert not hasattr(identifiers, "passport_bridge_ready")
    assert not hasattr(identifiers, "PASSPORT_BRIDGE_ATTR")


def test_the_parser_does_not_import_the_provider_layer() -> None:
    """Разбор строки не должен тянуть конфиг, БД и провайдеров ради юнит-теста."""
    import inspect

    source = inspect.getsource(identifiers)
    assert "app.providers" not in source
    assert "app.container" not in source


# ---------------------------------------------------------------- дата пробелами


@pytest.mark.parametrize(
    "token",
    ["24.11.1994", "24111994", "24-11-1994", "1994-11-24", "24 11 1994"],
)
def test_every_accepted_date_form_gives_the_same_day(token: str) -> None:
    """«24 11 1994» — дословный пример владелицы, и он читался тремя оговорками.

    Пробел :data:`_DATE_SHAPE` не ловит: три числа приезжают тремя токенами и до
    разбора дат не доходят вовсе. Остальные четыре формы работали и раньше и
    стоят здесь, чтобы починка не развела их между собой.
    """
    assert parse_query(token).birth_date == date(1994, 11, 24)


def test_a_two_digit_year_is_not_a_date_and_the_century_is_not_guessed() -> None:
    """Ошибка в веке стоит пустого ответа ФССП, который читается как «чисто»."""
    assert parse_query("24 11 94").birth_date is None
    fragment = identifiers.classify_fragment("24 11 94")
    assert fragment.kind is identifiers.FragmentKind.BAD_DATE
    assert "век угадывать не буду" in fragment.reason


@pytest.mark.parametrize(
    "line",
    ["4515 38 4710", "770912345601 12 34"],
    ids=["паспорт разбит на три группы", "ИНН и хвост"],
)
def test_the_spaced_date_branch_does_not_eat_other_numbers(line: str) -> None:
    """Условие по форме групп — единственное, что отделяет дату от чужих цифр."""
    assert parse_query(line).birth_date is None


# ---------------------------------------------------------------- фрагменты


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Клочкова Елена Николаевна", identifiers.FragmentKind.FIO),
        ("Клочкова", identifiers.FragmentKind.NAME_WORD),
        ("asdf", identifiers.FragmentKind.UNKNOWN),
        ("770912345601", identifiers.FragmentKind.INN12),
        ("инн 7709123456", identifiers.FragmentKind.INN10),
        ("9160000000", identifiers.FragmentKind.AMBIGUOUS_TEN),
        ("(916) 000-00-00", identifiers.FragmentKind.PHONE),
        ("4515 384710", identifiers.FragmentKind.PASSPORT),
        ("О123АА777", identifiers.FragmentKind.PLATE),
        ("XW8ZZZ61ZKG011111", identifiers.FragmentKind.VIN),
        ("15.13.1985", identifiers.FragmentKind.BAD_DATE),
        ("   ", identifiers.FragmentKind.EMPTY),
    ],
)
def test_a_fragment_is_classified_by_its_shape(text: str, kind: identifiers.FragmentKind) -> None:
    assert identifiers.classify_fragment(text).kind is kind


def test_a_latin_word_is_garbage_while_a_russian_one_is_a_name() -> None:
    """Оба проходят алфавит имени — и без кириллицы «asdf» уехало бы в фамилию.

    Когда поле названо кнопкой, требование снимается: там оператор уже сказал,
    что это имя, и спорить не с чем.
    """
    assert identifiers.classify_fragment("asdf").kind is identifiers.FragmentKind.UNKNOWN
    asked = identifiers.classify_fragment("asdf", expect=identifiers.Field.FIO)
    assert asked.kind is identifiers.FragmentKind.NAME_WORD


def test_a_named_field_refuses_a_wrong_shape_instead_of_moving_it() -> None:
    fragment = identifiers.classify_fragment("Клочкова", expect=identifiers.Field.INN)
    assert fragment.kind is identifiers.FragmentKind.UNKNOWN
    assert "12 цифр" in fragment.reason


def test_a_phone_alone_is_not_something_to_run_a_check_on() -> None:
    """Ни один внешний реестр по телефону не ищет — платить за него нечем."""
    assert not parse_query("+79161234567").runnable
    assert parse_query("Иванов Иван 01.01.1985").runnable
