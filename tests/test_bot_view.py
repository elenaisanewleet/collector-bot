"""Карточка в чате.

Главный инвариант проекта — «не проверено» ≠ «ничего не найдено» — здесь
превращается в конкретное требование к трём строкам. Пока «Не проверено» было
одной строкой на три разные беды, оператор не мог отличить своё упущение от
нашего: «дайте дату рождения» и «этого источника у нас нет» требуют совершенно
разных действий, а выглядели одинаково.

Второе требование — группировать источники машинно. Строка «ФССП, Залоги —
нужна дата рождения» собирается по :attr:`ProviderResult.missing_input`, а не
разбором собственного текста сообщения: группировка по подстроке развалилась бы
от первой же правки формулировки у провайдера.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.bot import view
from app.config import Settings
from app.domain.enums import MissingInput, ProviderName, ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import DebtorReport, ProviderResult
from app.services import reporting
from app.services.scoring import RecoveryScoreEngine
from app.services.verdict import VerdictEngine


def gap_result(provider: ProviderName, *missing: MissingInput, message: str = "") -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=ProviderStatus.ERROR,
        error_code="insufficient_query",
        error_message=message or "нечем было спросить",
        missing_input=tuple(item.value for item in missing),
    )


def person(**overrides: Any) -> SearchSubject:
    base: dict[str, Any] = {
        "search_type": SearchType.PERSON.value,
        "name": PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
    }
    return SearchSubject(**{**base, **overrides})


def card(report: DebtorReport, settings: Settings) -> str:
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    decision = VerdictEngine(settings).decide(report)
    return view.report_card(report, decision)


# ---------------------------------------------------------------- три причины


def test_the_card_separates_unqueried_from_unconnected(settings: Settings) -> None:
    """Две беды — две строки. Одну чинит оператор, вторую не чинит никто."""
    report = DebtorReport(subject=person())
    report.provider_results.extend(
        [
            gap_result(ProviderName.FSSP, MissingInput.BIRTH_DATE),
            ProviderResult(
                provider=ProviderName.VEHICLE,
                status=ProviderStatus.NOT_CONFIGURED,
                error_message="Источник не подключён",
            ),
        ]
    )

    text = card(report, settings)

    # Карточка называет, чего не хватает, но не перечисляет источники: заказчику
    # нужны телефон и сводка, а разбор по источникам стоит в отчёте. Позвать к
    # действию берут на себя кнопки под карточкой.
    assert "Чтобы проверить полнее — нужна дата рождения." in text
    assert "ФССП" not in text
    # Неподключённый источник обязан оставить след: иначе неполная проверка
    # читается как полная, и пошлина тратится по неверной картине.
    assert "Проверено не всё" in text
    assert "Авто" not in text


def test_sources_with_the_same_gap_are_grouped(settings: Settings) -> None:
    """«ЕФРСБ, ФНС, Суды — нужен ИНН» это одно действие, а не три проблемы."""
    report = DebtorReport(subject=person())
    report.provider_results.extend(
        [
            gap_result(ProviderName.FEDRESURS, MissingInput.INN),
            gap_result(ProviderName.FNS, MissingInput.INN),
            gap_result(ProviderName.COURT, MissingInput.INN),
            gap_result(ProviderName.FSSP, MissingInput.BIRTH_DATE),
            gap_result(ProviderName.PLEDGE, MissingInput.BIRTH_DATE),
        ]
    )

    text = card(report, settings)

    # Пять источников с двумя разными нехватками дают две причины, а не пять
    # строк: пять строк подряд про одно и то же читаются как пять проблем.
    assert "нужен ИНН физлица (12 цифр)" in text
    assert "нужна дата рождения" in text
    assert text.count("Чтобы проверить полнее") == 1
    for title in ("ЕФРСБ", "ФНС", "Суды", "ФССП", "Залоги"):
        assert title not in text


def test_a_failed_source_is_not_called_a_missing_field(settings: Settings) -> None:
    """Источник упал — это «не ответили», и лечится «Обновить», а не вводом."""
    report = DebtorReport(subject=person(birth_date=date(1985, 3, 12)))
    report.provider_results.append(
        ProviderResult(
            provider=ProviderName.FSSP,
            status=ProviderStatus.UNAVAILABLE,
            error_code="timeout",
            error_message="Источник недоступен",
        )
    )

    text = card(report, settings)

    # Упавший источник — не нехватка поля у оператора: добавлять ему нечего, и
    # предлагать это значит переложить на него нашу беду.
    assert "Часть источников не ответила" in text
    assert "Чтобы проверить полнее" not in text


def test_a_cached_result_without_the_field_falls_back_to_the_message(settings: Settings) -> None:
    """Старая запись поля не несёт — группировка теряется, честность нет."""
    report = DebtorReport(subject=person())
    report.provider_results.append(
        ProviderResult(
            provider=ProviderName.FSSP,
            status=ProviderStatus.ERROR,
            error_code="insufficient_query",
            error_message="Для поиска в ФССП нужна дата рождения",
        )
    )

    text = card(report, settings)

    # Целое предложение от источника идёт отдельной строкой, а не после тире:
    # «Чтобы проверить полнее — Для поиска в ФССП нужна дата рождения» — это
    # заглавная буква посреди фразы.
    assert "Для поиска в ФССП нужна дата рождения" in text
    assert "полнее — Для поиска" not in text


def test_an_answered_source_produces_no_line_at_all(settings: Settings) -> None:
    report = DebtorReport(subject=person(birth_date=date(1985, 3, 12)))
    report.provider_results.append(
        ProviderResult(provider=ProviderName.FSSP, status=ProviderStatus.NO_RESULTS)
    )

    text = card(report, settings)

    assert "Нечем спросить" not in text
    assert "Не подключено" not in text
    # С двоеточием: «Не ответили ключевые источники …» — это заголовок вердикта,
    # он про другое и приходит из :mod:`app.services.verdict`.
    assert "Не ответили: " not in text


def test_a_section_without_a_source_never_reaches_the_card(settings: Settings) -> None:
    """Раздел «Счета в банках» живёт в отчёте и в карточку не протекает.

    В карточке печатается только то, на что ответили, и строка «счетов нет» под
    каждым должником — ровно тот лишний текст, который владелица возвращала.
    Плюс это ловит выдуманного провайдера: он всплыл бы здесь репликой
    «Проверено не всё», то есть позвал бы оператора чинить нечинимое.
    """
    report = DebtorReport(subject=person(birth_date=date(1985, 3, 12)))
    report.provider_results.append(
        ProviderResult(provider=ProviderName.FSSP, status=ProviderStatus.NO_RESULTS)
    )

    text = card(report, settings)

    assert reporting.BANK_TITLE not in text
    assert reporting.BANK_NO_SOURCE_LINE not in text
    assert "счета" not in text.lower()
    assert view._facts(report) == ["Исполнительные производства: нет"]
    assert view._gaps(report) == []


# ---------------------------------------------------------------- эхо разбора


def test_the_echo_repeats_what_was_understood() -> None:
    subject = person(birth_date=date(1985, 3, 12), inn="770912345601")

    line = view.accepted_line(subject)

    assert line is not None
    assert "ФИО" in line
    assert "дата рождения 12.03.1985" in line
    assert "770912345601" in line


def test_the_echo_shows_the_documents_and_masks_the_phone() -> None:
    """Документы — целиком, телефон — маской, и это две разные причины.

    Документы печатаются потому, что с автопрогоном по номеру карточка не
    рисуется вовсе: эта строка осталась единственным местом, где владелец
    видит, ЧТО нашлось по номеру. Ради паспорта и СНИЛСа обращение и оплачено,
    и заявление подают с ними.

    Телефон остаётся маской по другой причине: его прислал сам оператор, он его
    знает наизусть, и печатать его обратно незачем.
    """
    subject = person(passport="4509123456", phone="+79990001122").model_copy(
        update={"snils": "11223344595", "passport_issued": date(2015, 1, 29)}
    )

    line = view.accepted_line(subject)

    assert line is not None
    assert "паспорт 4509123456" in line
    assert "выдан 29.01.2015" in line
    assert "СНИЛС 11223344595" in line
    assert "+79990001122" not in line


def test_the_echo_is_empty_when_nothing_was_parsed() -> None:
    assert view.accepted_line(SearchSubject(search_type=SearchType.PERSON.value)) is None


def test_parse_notes_survive_into_the_card(settings: Settings) -> None:
    """«Я выбросил часть вашего ввода» обязано остаться рядом с результатом.

    Сообщение прогресса правится на месте, поэтому оговорка, оставленная только
    в нём, исчезла бы вместе с ним.
    """
    report = DebtorReport(subject=person())
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    decision = VerdictEngine(settings).decide(report)

    text = view.report_card(report, decision, notes=["7709123456 — это ИНН организации."])

    assert "ИНН организации" in text


def test_the_demo_banner_stands_above_everything_about_the_person(settings: Settings) -> None:
    """Карточка на выдуманных данных не должна читаться как настоящая проверка.

    Баннер относится ко всему тексту, поэтому стоит первой строкой — до имени
    и оговорок, которые оба про субъект. Порядок наоборот дал бы первой
    строкой имя, то есть разбор реального человека раньше предупреждения о
    том, что человек выдуман.
    """
    from app.services.reporting import DEMO_BANNER

    report = DebtorReport(subject=person(inn="770912345601"))
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    decision = VerdictEngine(settings).decide(report)

    text = view.report_card(
        report, decision, notes=["7709123456 — это ИНН организации."], demo_mode=True
    )
    lines = text.splitlines()

    assert lines[0] == DEMO_BANNER
    assert lines[1] == ""
    assert lines[2] == report.subject.display_name
    # Под именем — идентификаторы, и только потом оговорки. Раньше здесь не было
    # ни того ни другого: считалось, что «Принял: …» из сообщения о ходе
    # проверки достаточно. Оказалось наоборот — отчёт ПРАВИТ то самое
    # сообщение, и всё, что в нём стояло, стирается.
    assert lines[3] == "ИНН 770912345601"
    assert lines[4] == "7709123456 — это ИНН организации."
    assert not any(line.startswith("Принял: ") for line in lines)
    # И наоборот: вне демо баннера быть не должно ни одной строкой.
    assert DEMO_BANNER not in view.report_card(report, decision)


# ---------------------------------------------------------------- заголовок


def test_a_search_by_inn_alone_is_titled_by_the_masked_inn() -> None:
    """Иначе такой отчёт назывался бы «—» и в чате, и в истории."""
    subject = SearchSubject(search_type=SearchType.PERSON.value, inn="770912345601")

    assert subject.display_name != "—"
    assert subject.display_name == "77********01"
