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
from app.bot.markup import strip_tags
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


def facts(report: DebtorReport) -> list[str]:
    """Строки фактов без разметки: тесты про слова, а не про оформление."""
    return [strip_tags(line) for line in view._facts(report)]


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
    assert facts(report) == ["Исполнительные производства: нет"]
    assert view._gaps(report) == []


# ---------------------------------------------------------------- эхо разбора


def test_the_echo_repeats_what_was_understood() -> None:
    subject = person(birth_date=date(1985, 3, 12), inn="770912345601")

    line = view.accepted_line(subject)

    assert line is not None
    assert "ФИО" in line
    assert "дата рождения 12.03.1985" in line
    assert "770912345601" in line


def test_the_echo_shows_everything_that_was_found() -> None:
    """Строка «Принял» печатает всё найденное целиком — включая адрес.

    С автопрогоном по номеру карточка не рисуется вовсе: эта строка осталась
    единственным местом, где владелец видит, ЧТО нашлось по номеру. Ради
    паспорта и СНИЛСа обращение и оплачено, и заявление подают с ними.

    Адрес стоит здесь не для полноты: он единственный открывает ЕГРН, и его
    отсутствие — причина, по которой раздел про недвижимость пишет
    «недостаточно данных». Видно это должно быть сразу, а не после отчёта.

    Телефон печатается целиком и по-человечески. Маска на нём была рефлексом:
    бот закрыт списком допуска, номер прислал сам оператор, и скрывать от
    человека то, что он минуту назад ввёл, — не приватность.
    """
    subject = person(passport="4509123456", phone="+79990001122").model_copy(
        update={
            "snils": "11223344595",
            "passport_issued": date(2015, 1, 29),
            "address": "г. Москва, Петровско-Разумовский проезд, д. 8",
        }
    )

    line = view.accepted_line(subject)

    assert line is not None
    assert "паспорт 4509123456" in line
    assert "выдан 29.01.2015" in line
    assert "СНИЛС 11223344595" in line
    assert "Петровско-Разумовский" in line
    assert "телефон +7 (999) 000-11-22" in line


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
    # Разметку снимаем: тест про ПОРЯДОК строк, а не про оформление.
    assert strip_tags(lines[2]) == report.subject.display_name
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


# ------------------------------------------------ строки четырёх новых источников


def found(provider: ProviderName, records: list[Any]) -> ProviderResult:
    return ProviderResult(provider=provider, status=ProviderStatus.SUCCESS, records=list(records))


def test_a_namesake_in_the_wanted_registry_is_not_called_a_finding(
    settings: Settings,
) -> None:
    """«Розыск МВД: 1» под человеком, которого никто не ищет.

    МВД ищет по строке имени, и полный тёзка попадает в выдачу наравне с
    должником. Числом такая запись читается как «должник в розыске» — то есть
    как «подавать бессмысленно», — и владелец закрыл бы дело, по которому можно
    было взыскать. Это самая дорогая ошибка, какую карточка способна сделать,
    поэтому числом печатаются только записи, подтверждённые датой рождения.
    """
    from app.domain.models import WantedRecord

    namesake = WantedRecord(full_name="Тестов Андрей Сергеевич", match_confidence=0.5)
    report = DebtorReport(subject=person(birth_date=date(1985, 3, 12)))
    report.wanted.append(namesake)
    report.provider_results.append(found(ProviderName.WANTED, [namesake]))

    assert facts(report) == ["Розыск МВД: однофамилец, не должник"]
    assert "Розыск МВД: 1" not in card(report, settings)


def test_a_confirmed_wanted_record_is_counted(settings: Settings) -> None:
    """Совпала дата рождения — это должник, и число здесь на месте."""
    from app.domain.models import WantedRecord

    debtor = WantedRecord(
        full_name="Тестов Андрей Сергеевич",
        birth_date=date(1985, 3, 12),
        birth_date_match=True,
        match_confidence=1.0,
    )
    report = DebtorReport(subject=person(birth_date=date(1985, 3, 12)))
    report.wanted.append(debtor)
    report.provider_results.append(found(ProviderName.WANTED, [debtor]))

    assert facts(report) == ["Розыск МВД: 1"]


def test_the_tax_debt_line_prints_the_sum_not_the_row_count(settings: Settings) -> None:
    """«Долг по налогам: 1» не говорит ни о чём, сумма говорит всё."""
    from decimal import Decimal

    from app.domain.models import TaxDebtRecord

    debt = TaxDebtRecord(amount=Decimal("12500"), match_confidence=1.0)
    report = DebtorReport(subject=person(birth_date=date(1985, 3, 12), inn="770912345601"))
    report.tax_debts.append(debt)
    report.provider_results.append(found(ProviderName.TAX_DEBT, [debt]))

    (line,) = facts(report)
    assert line.startswith("Долг по налогам: ")
    assert "12" in line and "500" in line
    assert line != "Долг по налогам: 1"


def test_a_zero_tax_debt_is_a_real_answer_and_says_so(settings: Settings) -> None:
    """Ноль — утверждение «проверено, долгов нет», и печатается словом.

    Записью он при этом остаётся: источник ответил, строка есть, и молча
    выбросить её значило бы отдать читателю пустое место вместо ответа.
    """
    from decimal import Decimal

    from app.domain.models import TaxDebtRecord

    debt = TaxDebtRecord(amount=Decimal("0"), match_confidence=1.0)
    report = DebtorReport(subject=person(birth_date=date(1985, 3, 12), inn="770912345601"))
    report.tax_debts.append(debt)
    report.provider_results.append(found(ProviderName.TAX_DEBT, [debt]))

    # «Долгов нет», а не «нет»: пустое «нет» в этом списке значит «источник
    # ответил пусто», и утверждение сливалось бы с отсутствием записей.
    assert facts(report) == ["Долг по налогам: долгов нет"]


def test_a_tax_answer_without_a_sum_is_not_a_zero(settings: Settings) -> None:
    """Источник ответил, суммы не назвал. Это не ноль: молчание — не утверждение."""
    from app.domain.models import TaxDebtRecord

    debt = TaxDebtRecord(amount=None, match_confidence=1.0)
    report = DebtorReport(subject=person(birth_date=date(1985, 3, 12), inn="770912345601"))
    report.tax_debts.append(debt)
    report.provider_results.append(found(ProviderName.TAX_DEBT, [debt]))

    assert facts(report) == ["Долг по налогам: сумма не названа"]


def test_self_employment_is_a_status_and_never_a_number(settings: Settings) -> None:
    """Три состояния статуса, и все три названы по-разному.

    ``is_active`` равен ``None``, пока источник не сказал, и это НЕ «не
    самозанятый» — так написано в самой модели. Все три состояния печатались
    одним словом «нет», и «источник статуса не назвал» было не отличить от
    «источник ответил, что статуса нет».
    """
    from app.domain.models import SelfEmployedRecord

    def status(is_active: bool | None) -> list[str]:
        record = SelfEmployedRecord(is_active=is_active, match_confidence=1.0)
        report = DebtorReport(subject=person(birth_date=date(1985, 3, 12), inn="770912345601"))
        report.self_employment.append(record)
        report.provider_results.append(found(ProviderName.SELF_EMPLOYED, [record]))
        return facts(report)

    assert status(True) == ["Самозанятость: да"]
    assert status(False) == ["Самозанятость: снят с учёта"]
    assert status(None) == ["Самозанятость: статус не назван"]


ADDRESS = "г Москва, проезд Тестовый,8,139"


def test_the_card_keeps_every_field_the_echo_showed(settings: Settings) -> None:
    """Карточка обязана донести то, что стояло в «Принял», — и адрес прежде всего.

    Жалоба владельца: «вообще не все поля». Карточка правит то же сообщение, в
    котором стояло эхо разбора, поэтому всё, чего в ней нет, из чата исчезает.
    Паспорт, СНИЛС и ИНН она доносила, а адрес, телефон, госномер и VIN теряла.

    Дороже всех терялся адрес: он единственный открывает ЕГРН, и по нему уходит
    ПЛАТНЫЙ запрос. Владелец поймал подставленный чужой адрес только по
    сообщению о ходе проверки — то есть единственный способ это заметить жил до
    конца ожидания и стирался вместе с ним.
    """
    subject = person(
        birth_date=date(1985, 3, 12),
        inn="770912345601",
        passport="4510123456",
        passport_issued=date(2015, 1, 29),
        snils="11223344595",
        address=ADDRESS,
        phone="+79990000000",
    )
    report = DebtorReport(subject=subject)

    text = strip_tags(card(report, settings))

    for expected in ("дата рождения", "ИНН 770912345601", "паспорт 4510123456", "выдан 29.01.2015"):
        assert expected in text
    assert ADDRESS in text, "адрес, по которому уходит платный запрос, не доехал до карточки"
    assert "телефон" in text


def test_only_a_real_finding_is_set_in_bold(settings: Settings) -> None:
    """Выделение значит «находка» — и не имеет права стоять на слове «нет».

    Жалоба владельца дословно: «почему-то только у самозанятости жирным
    выделено Нет». Выделение в этот список введено, чтобы находка не тонула
    среди девяти «нет», — а стояло на слове, которое говорит обратное.

    Причина была в том, что «источник ответил» и «источник нашёл повод»
    считались одним и тем же: запись о снятом с учёта — это состояние
    ``FOUND``, значение «нет», и выделялось оно наравне с настоящей находкой.
    """
    from decimal import Decimal

    from app.domain.models import SelfEmployedRecord, TaxDebtRecord

    def line(record: Any, provider: ProviderName, field: str) -> str:
        report = DebtorReport(subject=person(birth_date=date(1985, 3, 12), inn="770912345601"))
        getattr(report, field).append(record)
        report.provider_results.append(found(provider, [record]))
        return view._facts(report)[0]

    former = SelfEmployedRecord(is_active=False, match_confidence=1.0)
    active = SelfEmployedRecord(is_active=True, match_confidence=1.0)
    no_debt = TaxDebtRecord(amount=Decimal("0"), match_confidence=1.0)
    owes = TaxDebtRecord(amount=Decimal("12500"), match_confidence=1.0)

    assert "<b>" not in line(former, ProviderName.SELF_EMPLOYED, "self_employment")
    assert "<b>" not in line(no_debt, ProviderName.TAX_DEBT, "tax_debts")
    # А настоящая находка выделена по-прежнему: правило не отменено, а сужено.
    assert "<b>" in line(active, ProviderName.SELF_EMPLOYED, "self_employment")
    assert "<b>" in line(owes, ProviderName.TAX_DEBT, "tax_debts")


def test_account_blocks_are_counted_as_decisions(settings: Settings) -> None:
    """Блокировки — это решения ФНС, и число решений здесь значит именно число."""
    from app.domain.models import AccountBlockRecord

    blocks = [
        AccountBlockRecord(bank_bic="044525225", decision_number="1", match_confidence=1.0),
        AccountBlockRecord(bank_bic="044030653", decision_number="2", match_confidence=1.0),
    ]
    report = DebtorReport(subject=person(birth_date=date(1985, 3, 12), inn="770912345601"))
    report.account_blocks.extend(blocks)
    report.provider_results.append(found(ProviderName.ACCOUNT_BLOCK, blocks))

    assert facts(report) == ["Блокировки счетов: 2"]


# --------------------------------- ИНН, который бот добудет сам


def test_the_card_says_the_bot_will_fetch_the_inn_itself(settings: Settings) -> None:
    """«Нужен ИНН» — единственная просьба, которую оператор выполнить НЕ может.

    ИНН физлица он ниоткуда не возьмёт, зато его добывает мост по паспорту. И
    когда мосту не хватает только даты рождения, две строки — «нужен ИНН» и
    «нужна дата рождения» — это одно действие.

    Связи между ними видно не было, и владелец дважды прочитал результат
    одинаково: «опять отключён мост получения ИНН по паспорту?». Мост был
    включён и настроен — ему не хватало даты рождения, и сказать об этом было
    некому.
    """
    report = DebtorReport(subject=person(passport="4514964173"))
    report.provider_results.extend(
        (
            gap_result(ProviderName.INN_BRIDGE, MissingInput.BIRTH_DATE),
            gap_result(ProviderName.FEDRESURS, MissingInput.INN),
            gap_result(ProviderName.FNS, MissingInput.INN),
        )
    )

    gaps = "\n".join(view._gaps(report))

    assert "ИНН добуду сам по паспорту" in gaps
    assert "нужна дата рождения" in gaps
    # И названо, ЧТО откроется: иначе просьба выглядит просьбой ни за чем.
    assert "банкротство" in gaps and "статус ИП" in gaps


def test_no_promise_to_fetch_the_inn_when_the_bridge_cannot_run(settings: Settings) -> None:
    """Мост не подключён — обещать «добуду сам» нельзя.

    Это ровно та подмена, от которой заведён весь раздел: обещание проверки,
    которой не будет, хуже честного «нужен ИНН».
    """
    report = DebtorReport(subject=person(passport="4514964173"))
    report.provider_results.extend(
        (
            ProviderResult(
                provider=ProviderName.INN_BRIDGE,
                status=ProviderStatus.NOT_CONFIGURED,
                error_code="not_configured",
            ),
            gap_result(ProviderName.FEDRESURS, MissingInput.INN),
        )
    )

    gaps = "\n".join(view._gaps(report))

    assert "добуду сам" not in gaps


def test_no_promise_when_the_bridge_itself_waits_for_the_inn(settings: Settings) -> None:
    """Мост, которому нужен тот же ИНН, — не мост, а ещё один источник в очереди."""
    report = DebtorReport(subject=person())
    report.provider_results.extend(
        (
            gap_result(ProviderName.INN_BRIDGE, MissingInput.INN),
            gap_result(ProviderName.FEDRESURS, MissingInput.INN),
        )
    )

    assert "добуду сам" not in "\n".join(view._gaps(report))
