"""Кнопки под карточкой отчёта.

Одно правило и один тест на каждый способ его нарушить: **кнопка показывается,
только если она не соврёт**. «Узнать ИНН по паспорту» без ФИО или без даты
рождения обещает проверку, которой не будет — мост ``passport_fns`` требует все
три поля и без них отвечает ``insufficient_query``, не сделав ни одного вызова
и не потратив ни рубля.

Проверяется это не флагом и не списком полей, переписанным из провайдера, а
самим провайдером: ``will_query`` на субъекте с подставленным паспортом. Так
кнопка и мост не могут разойтись — это один и тот же код. Предыдущая попытка
(``passport_bridge_ready`` через атрибут ``resolves_inn_by_passport``) не
работала: атрибут никто не проставлял, функция возвращала False всегда, и кнопка
не появилась бы ни разу.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from aiogram.types import InlineKeyboardMarkup

from app.bot.report_actions import _report_label, passport_would_help, report_keyboard
from app.config import Settings
from app.domain.enums import SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import (
    DebtorReport,
    EnforcementProceeding,
    InternalDebtorRecord,
    SourcedFact,
)
from app.providers.identity_bridge import PassportInnProvider


@pytest.fixture
def bridge_settings(live_settings: Settings) -> Settings:
    """Живой мост с ключом и явно включённым флагом."""
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": "https://api.example.test",
            "inn_bridge_enabled": True,
        }
    )


@pytest.fixture
def subject() -> SearchSubject:
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
        birth_date=date(1985, 3, 12),
    )


def test_the_passport_offer_appears_when_it_would_actually_work(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    assert passport_would_help(subject, PassportInnProvider(bridge_settings))


def test_no_bridge_at_all_means_no_offer(subject: SearchSubject) -> None:
    assert not passport_would_help(subject, None)


def test_the_flag_being_off_means_no_offer(live_settings: Settings, subject: SearchSubject) -> None:
    """Без ``INN_BRIDGE_ENABLED`` мост отвечает NOT_CONFIGURED и не звонит никуда."""
    off = live_settings.model_copy(
        update={"newdb_api_key": "test-key", "inn_bridge_enabled": False}
    )
    assert not passport_would_help(subject, PassportInnProvider(off))


def test_an_existing_individual_inn_means_no_offer(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    with_inn = subject.model_copy(update={"inn": "770912345601"})
    assert not passport_would_help(with_inn, PassportInnProvider(bridge_settings))


@pytest.mark.parametrize("missing", ["name", "birth_date"])
def test_a_missing_field_the_bridge_needs_means_no_offer(
    bridge_settings: Settings, subject: SearchSubject, missing: str
) -> None:
    """Без ФИО или без даты мост ответит ``insufficient_query`` — обещать нечего."""
    crippled = subject.model_copy(update={missing: None})
    assert not passport_would_help(crippled, PassportInnProvider(bridge_settings))


def test_a_passport_already_in_hand_means_no_offer(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    with_passport = subject.model_copy(update={"passport": "4509123456"})
    assert not passport_would_help(with_passport, PassportInnProvider(bridge_settings))


def test_a_ten_digit_entity_inn_still_needs_the_bridge(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Десять цифр — идентификатор юрлица; три источника его отвергают."""
    entity = subject.model_copy(update={"inn": "7709123456"})
    assert passport_would_help(entity, PassportInnProvider(bridge_settings))


# ---------------------------------------------------------------- клавиатура


def button_texts(markup: object) -> list[str]:
    rows = getattr(markup, "inline_keyboard", [])
    return [button.text for row in rows for button in row]


def test_a_complete_subject_keeps_the_keyboard_as_short_as_before(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """ФИО, дата и ИНН — предлагать нечего, кроме ссылки, уточнения и повтора.

    Добор полей переехал в карточку запроса, а она теперь за кнопкой
    «Уточнить данные»: под каждым отчётом она приезжала сама и повторяла
    всё уже сказанное — двадцать строк и тринадцать кнопок.
    """
    complete = subject.model_copy(update={"inn": "770912345601"})
    markup = report_keyboard(
        url="https://reports.example.test/r/x",
        refresh_token="tok",
        subject=complete,
        bridge=PassportInnProvider(bridge_settings),
    )

    texts = button_texts(markup)
    # Кнопок-предложений нет: добавлять нечего. Проверяем по отсутствию слова
    # «Добавить», а не по эмодзи — их в подписях больше нет вовсе, бот должен
    # выглядеть ненавязчиво.
    assert not any(text.startswith("Добавить") for text in texts)
    assert "Открыть отчёт" in texts
    assert "Спросить заново" in texts
    # Тупиков нет: с карточки отчёта видно и следующего должника, и меню.
    assert "Новая проверка" in texts
    assert "В меню" in texts


def test_печать_и_файлом_живут_на_странице_а_не_в_чате(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Кнопок выгрузки под отчётом нет: обе ссылки уже стоят в шапке страницы.

    Они вели ровно туда же, куда «Открыть отчёт», — на тот же хост, тот же
    токен, тот же документ. Три адреса до одного места, один под другим, это
    не выбор, а шум, и именно на такие ряды показывали словами «одни кнопки».
    Печать и выгрузка никуда не делись: их печатает
    :func:`app.web.render._export_actions` в шапке самого отчёта.
    """
    markup = report_keyboard(
        url="https://reports.example.test/r/x",
        refresh_token="tok",
        subject=subject,
        bridge=PassportInnProvider(bridge_settings),
    )

    texts = button_texts(markup)
    assert "Печать" not in texts
    assert "Файлом" not in texts
    assert any(text.startswith("Открыть отчёт") for text in texts)


def test_without_a_link_the_report_still_offers_a_way_on(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Деплой без веба: ссылки нет, но экран не тупик."""
    markup = report_keyboard(
        url=None,
        refresh_token="tok",
        subject=subject,
        bridge=PassportInnProvider(bridge_settings),
    )

    texts = button_texts(markup)
    assert not any(text.startswith("Открыть отчёт") for text in texts)
    assert "Уточнить данные" in texts
    assert "Новая проверка" in texts


def test_narrowing_by_region_is_offered_only_when_it_would_change_something(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """Регион сужает поиск по ФССП — и предлагается, только когда есть что сужать.

    Раньше кнопка стояла под каждым отчётом по человеку, включая те, где
    производств не нашлось вовсе: нажатие вело к выбору региона и повторному
    поиску с тем же пустым результатом. Это то же правило, по которому здесь
    не показывают «Узнать ИНН по паспорту», — кнопка не должна обещать того,
    чего не будет.
    """

    def keyboard(*, narrowable: bool) -> InlineKeyboardMarkup:
        return report_keyboard(
            url="https://reports.example.test/r/x",
            refresh_token="tok",
            subject=subject,
            bridge=PassportInnProvider(bridge_settings),
            narrowable=narrowable,
        )

    assert "Сузить до одного региона" not in button_texts(keyboard(narrowable=False))
    assert "Сузить до одного региона" in button_texts(keyboard(narrowable=True))


def test_offers_never_appear_for_a_vehicle_search(bridge_settings: Settings) -> None:
    """Госномеру нечего добирать ИНН и датой рождения."""
    vehicle = SearchSubject(search_type=SearchType.VEHICLE_PLATE.value)
    markup = report_keyboard(
        url=None,
        refresh_token="tok",
        subject=vehicle,
        bridge=PassportInnProvider(bridge_settings),
    )

    assert not any(text.startswith(("➕", "📅", "🪪", "📍")) for text in button_texts(markup))


def test_the_callback_payload_fits_telegrams_limit(
    bridge_settings: Settings, subject: SearchSubject
) -> None:
    """64 байта — жёсткий предел Telegram, и токен субъекта его не переберёт."""
    markup = report_keyboard(
        url=None,
        refresh_token="A" * 22,
        subject=subject,
        bridge=PassportInnProvider(bridge_settings),
    )
    payloads = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]

    assert payloads
    assert all(len(payload.encode("utf-8")) <= 64 for payload in payloads)


# ------------------------------------------------------------------- мосты


def test_a_bridge_that_worked_never_reports_zero_records(subject: SearchSubject) -> None:
    """Мост, сделавший проверку возможной, не отчитывается нулём.

    Мосты записей не приносят и не должны: они переводят телефон в ФИО и
    паспорт в ИНН, чтобы остальные источники вообще можно было спросить. Общая
    ветка списка печатала им «✓ … — 0 зап.» — то есть успех выглядел ровно как
    пустой ответ. На телефонном мосту это уже стояло на проде.

    Проверяются оба места, где источник называется: строка в текстовом отчёте и
    подпись на веб-странице. Разъезжаются они молча.
    """
    from app.domain.enums import ProviderStatus
    from app.domain.models import DebtorReport, ProviderResult
    from app.services.reporting import BRIDGES, _source_line
    from app.web.render import _state_of

    report = DebtorReport(subject=subject)
    for provider in BRIDGES:
        result = ProviderResult(provider=provider, status=ProviderStatus.SUCCESS, records=[])

        assert "0 зап." not in _source_line(report, result)
        assert "зап." not in _state_of(report, result).label


# --------------------------------------------------- число записей на кнопке


def internal(subject: SearchSubject) -> InternalDebtorRecord:
    """Своя строка из выгрузки — то, с чего начинается любая проверка из базы."""
    assert subject.name is not None
    return InternalDebtorRecord(
        debtor_id="1",
        full_name=subject.name.full,
        debt_amount=Decimal("5000"),
        match_confidence=1.0,
    )


def test_our_own_row_is_not_counted_as_a_finding(subject: SearchSubject) -> None:
    """«Открыть отчёт (2 записи)» у должника с одним производством.

    Ровно эта подпись и пришла от владельца вопросом «что за 2 записи?».
    Второй записью были мы сами: строка из выгрузки считалась наравне с
    находками. Сложить двойку было не из чего — карточка прямо над кнопкой
    печатает «Исполнительные производства: 1» и шесть «нет», а наш долг стоит
    отдельной строкой выше.

    Хуже арифметики было то, что число не сравнивалось между проверками: у
    должника из базы оно всегда на единицу больше, чем у того же человека,
    проверенного не из базы.
    """
    report = DebtorReport(subject=subject)
    report.internal_records.append(internal(subject))
    report.enforcement_proceedings.append(
        EnforcementProceeding(proceeding_number="1234/56/78-ИП", match_confidence=1.0)
    )

    assert report.fact_count == 1
    assert _report_label(report.fact_count) == "Открыть отчёт (1 запись)"


def test_a_check_from_the_base_and_one_from_outside_count_the_same(
    subject: SearchSubject,
) -> None:
    """Одни и те же находки — одно и то же число, независимо от пути проверки."""

    def with_proceeding(*, from_base: bool) -> int:
        report = DebtorReport(subject=subject)
        if from_base:
            report.internal_records.append(internal(subject))
        report.enforcement_proceedings.append(
            EnforcementProceeding(proceeding_number="1234/56/78-ИП", match_confidence=1.0)
        )
        return report.fact_count

    assert with_proceeding(from_base=True) == with_proceeding(from_base=False)


def test_every_source_on_the_page_reaches_the_count(subject: SearchSubject) -> None:
    """Кнопка обещает содержимое страницы, а не часть его.

    Четыре источника, подключённые последними, в счёт не шли: должник в розыске
    с заблокированным счётом получал кнопку, которая о них молчала. Тест держит
    список списков: добавили раздел в отчёт — он обязан появиться и здесь.
    """
    report = DebtorReport(subject=subject)
    counted = (
        "enforcement_proceedings",
        "bankruptcies",
        "business_relations",
        "court_cases",
        "pledges",
        "inheritance_cases",
        "vehicles",
        "properties",
        "wanted",
        "account_blocks",
        "tax_debts",
        "self_employment",
    )
    for index, attribute in enumerate(counted, start=1):
        getattr(report, attribute).append(_one_record(attribute, subject))
        assert report.fact_count == index, f"{attribute} не попал в число на кнопке"


def _one_record(attribute: str, subject: SearchSubject) -> SourcedFact:
    """По одной записи каждого вида — минимально, лишь бы список не был пуст."""
    from app.domain.models import (
        AccountBlockRecord,
        BankruptcyRecord,
        BusinessRelation,
        CourtCase,
        InheritanceCase,
        PledgeRecord,
        PropertyRecord,
        SelfEmployedRecord,
        TaxDebtRecord,
        VehicleRecord,
        WantedRecord,
    )

    assert subject.name is not None
    records: dict[str, SourcedFact] = {
        "enforcement_proceedings": EnforcementProceeding(proceeding_number="1/2/3-ИП"),
        "bankruptcies": BankruptcyRecord(case_number="А40-1/2026"),
        "business_relations": BusinessRelation(name="ООО Тест"),
        "court_cases": CourtCase(case_number="2-1/2026"),
        "pledges": PledgeRecord(registration_number="П-1"),
        "inheritance_cases": InheritanceCase(case_number="Н-1"),
        "vehicles": VehicleRecord(plate="А001АА77"),
        "properties": PropertyRecord(cadastral_number="77:01:0001:1"),
        "wanted": WantedRecord(full_name=subject.name.full),
        "account_blocks": AccountBlockRecord(bank_bic="044525225"),
        "tax_debts": TaxDebtRecord(amount=Decimal("1200")),
        "self_employment": SelfEmployedRecord(is_active=True),
    }
    return records[attribute]


def test_the_button_counts_nothing_the_card_does_not_name(subject: SearchSubject) -> None:
    """Кнопка и карточка не имеют права разъезжаться.

    Разошлись они однажды и сразу дали жалобу «что за 2 записи?»: подпись
    называла число, которого в карточке не было видно. Замок держит обе стороны
    сразу — источник, чьи записи идут в :attr:`DebtorReport.fact_count`, обязан
    иметь строку в :data:`app.bot.view._FACT_TITLES`, иначе владельцу опять
    нечего будет складывать.
    """
    from app.bot.view import _FACT_TITLES

    named = {provider for provider, _ in _FACT_TITLES}
    counted = (
        "enforcement_proceedings",
        "bankruptcies",
        "business_relations",
        "court_cases",
        "pledges",
        "inheritance_cases",
        "vehicles",
        "properties",
        "wanted",
        "account_blocks",
        "tax_debts",
        "self_employment",
    )
    for attribute in counted:
        provider = _one_record(attribute, subject).provider
        assert provider in named, f"{attribute} считается на кнопке, но в карточке безымянен"
