"""Выбор источников на одну проверку.

Функция заведена по просьбе владельца: «делать запросы отдельными (которые не
дипсерч) и не получить ИНН, чтобы не расходовать запросы в NewDB». Обращение
стоит 2 ₽, полная проверка должника с паспортом — пять обращений, и четыре из
них лишние, когда смотришь, починился ли один источник.

Всё здесь стоит вокруг одного требования: **экономия не имеет права сделать
отчёт лживым**. Невыбранный источник не отвечает «ничего не найдено» — он не
отвечает вовсе, и отчёт говорит «не опрашивался». Это главный инвариант проекта
с новым поводом не спросить.
"""

from __future__ import annotations

from app.domain.enums import ProviderName
from app.domain.models import DebtorReport
from app.domain.source_plan import EVERYTHING, SourcePlan
from app.services import reporting

CONFIGURED = [
    ProviderName.FSSP,
    ProviderName.FEDRESURS,
    ProviderName.PROPERTY,
    ProviderName.INHERITANCE,
]


def test_the_default_plan_asks_everything() -> None:
    """Проверка без выбора обязана остаться той же, какой была."""
    assert EVERYTHING.is_selective is False
    for name in ProviderName:
        assert EVERYTHING.includes(name)


def test_a_plan_asks_only_what_it_names() -> None:
    plan = SourcePlan.only([ProviderName.PROPERTY])

    assert plan.is_selective is True
    assert plan.includes(ProviderName.PROPERTY)
    assert not plan.includes(ProviderName.FSSP)


def test_asking_nobody_is_a_legal_choice() -> None:
    """Пустой выбор — «посмотреть свою базу и мосты, не потратив рубля».

    Его нельзя путать с отсутствием выбора: ``None`` значит «всё», пустое
    множество — «никого», и разница здесь в деньгах.
    """
    plan = SourcePlan.only([])

    assert plan.is_selective is True
    assert not plan.includes(ProviderName.FSSP)
    assert plan.narrowed_to(CONFIGURED) == []


def test_refusing_to_buy_the_inn_is_selective_by_itself() -> None:
    """Даже со всеми источниками отказ от ИНН меняет полноту отчёта.

    Три источника ищут только по ИНН. Без него они скажут «недостаточно
    данных», и отчёт, не назвавший причину, будет прочитан как «у должника
    ничего нет».
    """
    plan = SourcePlan(buy_inn=False)

    assert plan.is_selective is True
    assert plan.includes(ProviderName.FEDRESURS)


def test_an_unconfigured_choice_is_not_promised() -> None:
    """Выбранный, но неподключённый источник в перечисление не попадает.

    Иначе строка «спрошены только …» обещала бы ответ, которого не будет.
    Порядок — как у подключённых, чтобы перечисление читалось одинаково.
    """
    plan = SourcePlan.only([ProviderName.PROPERTY, ProviderName.VEHICLE, ProviderName.FSSP])

    assert plan.narrowed_to(CONFIGURED) == [ProviderName.FSSP, ProviderName.PROPERTY]


# ------------------------------------------------- что говорит отчёт


def test_an_ordinary_report_says_nothing_about_a_choice() -> None:
    """Обычная проверка не печатает ни слова о выборе: его не было."""
    report = DebtorReport(subject=reporting_subject())

    assert reporting.selective_note(report) == ""


def test_a_selective_report_names_what_was_asked() -> None:
    """Читатель, не знающий про выбор, прочитает отчёт как полный.

    Одной строкой на всех, а не пометкой у каждого невыбранного: девять строк
    «вы его не выбрали» читаются как девять бед, хотя беда одна и она —
    осознанное решение оператора.
    """
    report = DebtorReport(subject=reporting_subject())
    report.queried_sources = (ProviderName.PROPERTY,)

    note = reporting.selective_note(report)

    assert reporting.SELECTIVE_LEAD in note
    assert "Объект по адресу (ЕГРН)" in note
    # Про невыбранные не перечисляется: их состояние и так печатается построчно.
    assert "ФССП" not in note


def test_a_report_that_asked_nobody_says_so() -> None:
    report = DebtorReport(subject=reporting_subject())
    report.queried_sources = ()

    assert reporting.SELECTIVE_NOBODY in reporting.selective_note(report)


def test_a_report_without_a_bought_inn_says_why_three_sources_are_silent() -> None:
    report = DebtorReport(subject=reporting_subject())
    report.bought_inn = False

    note = reporting.selective_note(report)

    assert reporting.SELECTIVE_LEAD in note
    assert reporting.SELECTIVE_NO_INN in note


def reporting_subject() -> object:
    from app.domain.enums import SearchType
    from app.domain.identity import PersonName, SearchSubject

    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
    )
