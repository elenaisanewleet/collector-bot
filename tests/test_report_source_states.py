"""Шесть состояний источника — шесть разных разделов, и одни и те же слова
в чате и на распечатке.

Главное правило продукта проверяется здесь не на одном разделе, а таблицей: для
каждой секции прогоняются все состояния :class:`ProviderResult`, и два
состояния, давшие один и тот же текст, — это дефект. Так был найден раздел
ЕГРН, печатавший «по указанному адресу объект не найден» на все шесть состояний
разом: в проде ``ROSREESTR_ENABLED`` не задан, источник NOT_CONFIGURED у каждого
должника, а лист «Скачать текстом» несут в суд.

Второй предмет проверки — согласие четырёх выводов. Текст (``reporting``) и
страница (``web.render``) описывают один и тот же результат, и расхождение между
ними означает, что один и тот же должник получил два разных ответа, причём
неправ обычно тот, который печатают. Выгрузка очереди (``services.export``) в
таблицу не входит намеренно: она не несёт состояний источников вовсе — в
``BatchItem`` их нет, — и утверждений об отсутствии записей не делает.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from decimal import Decimal
from html import unescape

import pytest

from app.domain.enums import (
    BusinessRole,
    BusinessStatus,
    EntityType,
    ProviderName,
    ProviderStatus,
    SearchType,
)
from app.domain.identity import SearchSubject
from app.domain.models import (
    BusinessRelation,
    DebtorReport,
    InternalDebtorRecord,
    PropertyRecord,
    ProviderResult,
)
from app.services import reporting
from app.web import render
from app.web.style import CSS

# Состояния источника ровно те, что различает NewDB и наш собственный слой.
# ``payment_required`` и ``unauthorized`` приезжают как ProviderStatus.ERROR
# (ProviderBadResponseError / ProviderAuthError), ``poll_timeout`` и
# ``rate_limited`` — как UNAVAILABLE; их разделение и есть предмет теста.
STATES: dict[str, dict[str, object]] = {
    "not_configured": {
        "status": ProviderStatus.NOT_CONFIGURED,
        "error_code": "not_configured",
        "error_message": "Источник не подключён",
    },
    "payment_required": {
        "status": ProviderStatus.ERROR,
        "error_code": "payment_required",
        "error_message": "недостаточно средств на счёте",
    },
    "unauthorized": {
        "status": ProviderStatus.ERROR,
        "error_code": "unauthorized",
        "error_message": "ключ отвергнут",
    },
    "poll_timeout": {"status": ProviderStatus.UNAVAILABLE, "error_code": "poll_timeout"},
    "rate_limited": {"status": ProviderStatus.UNAVAILABLE, "error_code": "rate_limited"},
    "insufficient": {
        "status": ProviderStatus.ERROR,
        "error_code": "insufficient_query",
        "error_message": "Для запроса в Росреестр нужен адрес или кадастровый номер",
    },
    # Тот же отказ, но поднятый из кэша: колонки под error_message в БД нет.
    "insufficient_cached": {
        "status": ProviderStatus.ERROR,
        "error_code": "insufficient_query",
    },
    "no_results": {"status": ProviderStatus.NO_RESULTS},
    "partial": {
        "status": ProviderStatus.NO_RESULTS,
        "is_partial": True,
        "notes": ("В реестре найдено 13 записей, сопоставить не удалось ни одну.",),
    },
}

# Секция → (провайдер, текстовый блок, секция страницы).
SECTIONS: dict[str, tuple[ProviderName, Callable[[DebtorReport], str], Callable[..., str]]] = {
    "internal": (ProviderName.INTERNAL, reporting._internal_block, render.internal_section),
    "fssp": (ProviderName.FSSP, reporting._enforcement_block, render.enforcement_section),
    "bankruptcy": (ProviderName.FEDRESURS, reporting._bankruptcy_block, render.bankruptcy_section),
    "business": (ProviderName.FNS, reporting._business_block, render.business_section),
    "pledge": (ProviderName.PLEDGE, reporting._pledge_block, render.pledge_section),
    "inheritance": (
        ProviderName.INHERITANCE,
        reporting._inheritance_block,
        render.inheritance_section,
    ),
    "property": (ProviderName.PROPERTY, reporting._property_block, render.property_section),
    "court": (ProviderName.COURT, reporting._court_block, render.court_section),
}

# Разделы страницы, которых в таблице выше нет намеренно: источника у них нет
# вовсе, и прогонять по ним состояния ``ProviderResult`` не из чего. Список
# закрытый: секция, забытая и тут, и в SECTIONS, выпала бы из главной проверки
# правила — три параметризованных теста ниже её просто не увидели бы, а pytest
# остался бы зелёным.
EXEMPT_SECTIONS = {"sources", "score", "bank"}

ADDRESS = "Саратовская обл., г. Ртищево, ул. Красная, д.22, кв.10"

# Что в разделе обязано быть и чего в нём быть не должно. Ключ — состояние,
# значение — (обязательные подстроки, запрещённые подстроки). Запрещённое здесь
# важнее обязательного: именно оно ловит подмену «не проверено» на «не найдено».
NOT_FOUND_WORDS = ("не найден", "не обнаружено", "Совпадений во внутренней базе")

EXPECTED: dict[str, dict[str, tuple[tuple[str, ...], tuple[str, ...]]]] = {
    "property": {
        "not_configured": (("Не проверено: источник не подключён.",), NOT_FOUND_WORDS),
        "payment_required": (
            ("у поставщика данных закончились оплаченные запросы", "payment_required"),
            NOT_FOUND_WORDS,
        ),
        "unauthorized": (
            ("поставщик данных отверг ключ доступа", "unauthorized"),
            NOT_FOUND_WORDS,
        ),
        "poll_timeout": (("источник не успел подготовить ответ", "poll_timeout"), NOT_FOUND_WORDS),
        "rate_limited": (
            ("поставщик ограничил частоту запросов", "rate_limited"),
            NOT_FOUND_WORDS,
        ),
        "insufficient": (
            ("Не проверено:", "нужен адрес или кадастровый номер"),
            NOT_FOUND_WORDS,
        ),
        # Адрес в карточке есть, а причина отказа не пережила кэш: догадываться
        # о ней нельзя, и раздел остаётся на общей формулировке. Случай «адреса
        # нет вовсе» разобран отдельным тестом ниже.
        "insufficient_cached": (("Не проверено: недостаточно данных.",), NOT_FOUND_WORDS),
        "no_results": ((reporting.NO_PROPERTY_FOUND, "Проверено:"), ("Не проверено",)),
        "partial": (
            ("В реестре найдено 13 записей",),
            (reporting.NO_PROPERTY_FOUND, "Не проверено"),
        ),
    },
    "fssp": {
        "not_configured": (("Не проверено: источник не подключён.",), NOT_FOUND_WORDS),
        "payment_required": (("payment_required",), NOT_FOUND_WORDS),
        "poll_timeout": (("poll_timeout",), NOT_FOUND_WORDS),
        "no_results": (("Активных исполнительных производств не найдено.",), ("Не проверено",)),
    },
    "internal": {
        "not_configured": (("Не проверено: источник не подключён.",), NOT_FOUND_WORDS),
        "payment_required": (("payment_required",), NOT_FOUND_WORDS),
        "poll_timeout": (("poll_timeout",), NOT_FOUND_WORDS),
        "no_results": ((reporting.INTERNAL_NOT_FOUND,), ("Не проверено",)),
        "partial": (("В реестре найдено 13 записей",), (reporting.INTERNAL_NOT_FOUND,)),
    },
}


def report_with(
    provider: ProviderName, state: str, *, address: str | None = None, **extra: object
) -> DebtorReport:
    """Отчёт, в котором один источник находится в заданном состоянии."""
    subject = SearchSubject(search_type=SearchType.PERSON.value, address=address)
    result = ProviderResult(provider=provider, **STATES[state])
    return DebtorReport(subject=subject, provider_results=[result], **extra)


def plain(html: str) -> str:
    """Текст страницы без разметки — для сверки формулировок с чатом."""
    return unescape(re.sub(r"<[^>]+>", " ", html))


@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_every_state_reads_differently(section: str) -> None:
    """Два состояния с одинаковым текстом — это состояние, о котором отчёт молчит.

    Раздел ЕГРН давал побайтово одну строку на девять прогонов ниже, и шесть из
    них означали «мы не смотрели».
    """
    provider, text_block, _ = SECTIONS[section]
    seen: dict[str, list[str]] = {}
    for state in STATES:
        text = text_block(report_with(provider, state, address=ADDRESS))
        # Минута проверки различает состояния случайно, а не по смыслу.
        body = "\n".join(line for line in text.split("\n") if not line.startswith("Проверено:"))
        seen.setdefault(body, []).append(state)

    collapsed = {tuple(states): body for body, states in seen.items() if len(states) > 1}
    assert not collapsed, f"{section}: состояния неразличимы — {sorted(collapsed)}"


@pytest.mark.parametrize(
    ("section", "state"),
    [(section, state) for section, table in EXPECTED.items() for state in table],
)
def test_section_says_what_it_must_and_nothing_more(section: str, state: str) -> None:
    provider, text_block, _ = SECTIONS[section]
    required, forbidden = EXPECTED[section][state]
    text = text_block(report_with(provider, state, address=ADDRESS))

    for fragment in required:
        assert fragment in text, f"{section}/{state}: нет «{fragment}»\n{text}"
    for fragment in forbidden:
        assert fragment not in text, f"{section}/{state}: есть запрещённое «{fragment}»\n{text}"


@pytest.mark.parametrize("state", sorted(STATES))
@pytest.mark.parametrize("section", sorted(SECTIONS))
def test_page_and_chat_describe_the_source_alike(section: str, state: str) -> None:
    """Строка состояния на странице — та же, что в чате, слово в слово.

    Лист несут в дело: страница, которая описывает непроверенный источник
    иначе, чем текст, делает документ противоречащим самому себе.
    """
    provider, text_block, web_section = SECTIONS[section]
    report = report_with(provider, state, address=ADDRESS)
    text = text_block(report)
    page = plain(web_section(report))

    line = reporting.unanswered_line(report.result_for(provider))
    if section == "property":
        line = reporting.property_unanswered_line(
            report.result_for(provider), address=report.subject.address
        )
    if line is None:
        pytest.skip("источник ответил — сверяются разделы, а не строка состояния")
    assert line in text
    assert line in page


def test_egrn_never_claims_a_check_it_did_not_make() -> None:
    """Тот самый лист «Скачать текстом» при незаданном ROSREESTR_ENABLED."""
    report = report_with(ProviderName.PROPERTY, "not_configured", address=ADDRESS)
    text = reporting._property_block(report)

    assert reporting.NOT_CONFIGURED_REPORT_LINE in text
    assert "не найден" not in text
    # Отметки о проверке быть не может: проверки не было.
    assert "Проверено:" not in text


def test_egrn_does_not_invent_an_address_it_was_never_given() -> None:
    """«По указанному адресу» при пустом адресе — ссылка на несказанное."""
    report = report_with(ProviderName.PROPERTY, "insufficient_cached")
    text = reporting._property_block(report)

    assert reporting.NO_ADDRESS_REPORT_LINE in text
    assert "адресу" in text
    assert reporting.NO_PROPERTY_FOUND not in text


def test_egrn_empty_answer_names_the_address_it_checked() -> None:
    report = report_with(ProviderName.PROPERTY, "no_results", address=ADDRESS)
    text = reporting._property_block(report)

    assert ADDRESS in text
    assert reporting.NO_PROPERTY_FOUND in text
    assert "Проверено:" in text


def test_egrn_empty_answer_without_an_address_says_so() -> None:
    """Пустой ответ есть, а адреса в карточке нет: сказать «по указанному
    адресу» не о чем, и раздел этого не говорит."""
    report = report_with(ProviderName.PROPERTY, "no_results")
    text = reporting._property_block(report)

    assert reporting.NO_PROPERTY_FOUND_WITHOUT_ADDRESS in text
    assert reporting.NO_PROPERTY_FOUND not in text


def test_egrn_scope_note_is_printed_in_every_branch() -> None:
    """Оговорка охвата — во всех ветках, как у залогов и судов."""
    for state in STATES:
        report = report_with(ProviderName.PROPERTY, state, address=ADDRESS)
        assert reporting.PROPERTY_SCOPE_NOTE in reporting._property_block(report), state


def test_egrn_section_exists_on_the_page() -> None:
    """Раздела ЕГРН на странице не было вовсе, хотя в тексте он есть.

    Кадастровый номер — единственное, что вписывается в ходатайство приставу, —
    не доходил ни до экрана, ни до распечатки.
    """
    record = PropertyRecord(
        property_type="Помещение, жилое",
        cadastral_number="64:47:040605:229",
        area="42.3",
        cadastral_cost=Decimal("1200000"),
        rights_count=4,
        encumbrances_checked=True,
    )
    subject = SearchSubject(search_type=SearchType.PERSON.value, address=ADDRESS)
    report = DebtorReport(
        subject=subject,
        properties=[record],
        provider_results=[
            ProviderResult(
                provider=ProviderName.PROPERTY,
                status=ProviderStatus.SUCCESS,
                records=[record],
            )
        ],
    )

    anchors = [block.anchor for block in render.build_blocks(report)]
    assert "property" in anchors
    page = plain(render.property_section(report))
    assert "64:47:040605:229" in page
    assert reporting.OWNERSHIP_DISCLAIMER in page
    assert "1 200 000" in page


def test_every_section_of_the_page_is_covered_by_the_state_table() -> None:
    """Секция, не попавшая ни в SECTIONS, ни в EXEMPT_SECTIONS, — дыра в правиле.

    Таблица SECTIONS ведётся руками, и три главных теста этого файла
    параметризуются от неё. Новый раздел, забытый в таблице, не проверяется
    вовсе: зелёный pytest перестаёт означать «инвариант проверен». Здесь это
    становится падением, а не тишиной.
    """
    report = DebtorReport(subject=SearchSubject(search_type=SearchType.PERSON.value))
    report.provider_results.extend(
        ProviderResult(provider=provider, status=ProviderStatus.NO_RESULTS)
        for provider, _text, _web in SECTIONS.values()
    )
    anchors = {block.anchor for block in render.build_blocks(report)}

    assert anchors <= set(SECTIONS) | EXEMPT_SECTIONS, sorted(
        anchors - set(SECTIONS) - EXEMPT_SECTIONS
    )


# ---------------------------------------------------------------- счета в банках


def test_bank_accounts_are_not_a_source_we_forgot_to_ask() -> None:
    """«Источника нет» обязано отличаться от «не подключено» и «не опрашивался».

    Это тот же инвариант, что и во всём файле, но в самом дорогом его виде: тут
    речь не о том, что мы не сходили, а о том, что сходить нельзя никому, кроме
    пристава и суда. Раздел, взявший чужую подпись, обещал бы подключение — а
    подключать нечего, и читающий строил бы на этом обещании план взыскания.
    """
    text = reporting._bank_block()
    page = plain(render.bank_section())

    for output in (text, page):
        assert reporting.NOT_CONFIGURED_LABEL not in output
        assert reporting.NOT_CONFIGURED_REPORT_LINE not in output
        assert reporting.EMPTY_LABEL not in output
        assert "не опрашивался" not in output
        # «Не найдено» здесь было бы утверждением о счетах, которого никто не
        # проверял: пустой ответ и отсутствие вопроса — разные вещи.
        for word in NOT_FOUND_WORDS:
            assert word not in output


def test_the_state_of_a_source_that_cannot_exist_stands_apart() -> None:
    """Подпись, знак и класс чипа — свои, и на бумаге тоже.

    Все непроверенные состояния красятся одним штрихованным классом, и новое
    состояние по умолчанию слилось бы с «не подключено» ровно там, где раздел и
    заводился, чтобы их развести, — на распечатке, которую несут в суд.
    """
    others = [
        reporting.source_state(None),
        *(
            reporting.source_state(ProviderResult(provider=ProviderName.FSSP, **state))
            for state in STATES.values()
        ),
    ]
    assert all(reporting.NO_SOURCE_STATE.label != state.label for state in others)
    assert all(reporting.NO_SOURCE_STATE.mark != state.mark for state in others)

    tag = render.state_tag(reporting.NO_SOURCE_STATE)
    assert "unchecked" not in tag
    assert 'class="tag nosource"' in tag
    assert ".tag.nosource{" in CSS
    assert ".tag.nosource{" in CSS[CSS.index("@media print{") :]


def test_no_provider_result_can_ever_claim_this_state() -> None:
    """``source_state`` это состояние не выдаёт — и не должна.

    У неё есть ветка ``case _`` в :func:`unanswered_line` и такая же в карточке
    чата: состояние, приехавшее туда через выдуманный ``ProviderResult``,
    напечаталось бы как «ошибка обращения к источнику (unknown)».
    """
    answers = (ProviderResult(provider=ProviderName.FSSP, status=s) for s in ProviderStatus)
    for result in (None, *answers):
        assert reporting.source_state(result).code is not reporting.SourceStateCode.NO_SOURCE


def test_bank_section_says_where_the_data_can_actually_be_obtained() -> None:
    """Раздел полезен, а не просто честен: он называет, что делать дальше.

    «Данных нет» — это не ответ взыскателю. Ответ — два законных пути и
    основание каждого; ими раздел и заканчивается.
    """
    for output in (reporting._bank_block(), plain(render.bank_section())):
        assert "суде" in output
        assert "пристава" in output
        assert "ст. 26" in output
        assert "ст. 69 ФЗ-229" in output


def test_bank_section_names_no_source_and_no_setting() -> None:
    """Правило 4: имён источников и настроек в тексте для оператора нет.

    Название закона — не имя источника, а основание, и оно как раз обязано
    стоять: без него «нет и не будет» это наше слово против его вопроса.
    """
    for output in (reporting._bank_block(), plain(render.bank_section())):
        for name in ("ЕГРН", "Федресурс", "ЕФРСБ", "Росреестр", "ФНП", "NewDB", "ENABLED"):
            assert name not in output


def test_bank_section_stays_short() -> None:
    """Владелица много раз возвращала лишний текст. Четыре строки — потолок."""
    body = reporting._bank_block().split("\n")[1:]
    assert len(body) <= 4


def test_bank_section_reads_the_same_on_the_page_and_in_the_file() -> None:
    """Раздел без провайдера легко завести только в одном из двух выводов.

    ``test_text_export_matches_the_report_sent_to_chat`` этого не поймает: обе
    его стороны берутся из ``reporting``, и веб-секцию он не видит вовсе. А в
    дело уходит именно файл.
    """
    report = DebtorReport(subject=SearchSubject(search_type=SearchType.PERSON.value))
    text = reporting.render_report(report)
    page = plain("".join(block.html for block in render.build_blocks(report)))

    assert reporting.BANK_TITLE.upper() in text
    assert reporting.BANK_TITLE in page
    for line in (reporting.BANK_NO_SOURCE_LINE, reporting.BANK_ACCESS_LINE):
        assert line in text
        assert line in page


def test_bank_section_stands_next_to_property() -> None:
    """В хвосте оглавления раздел читался бы как оговорка, а не как ответ."""
    report = DebtorReport(subject=SearchSubject(search_type=SearchType.PERSON.value))
    anchors = [block.anchor for block in render.build_blocks(report)]

    assert anchors.index("bank") == anchors.index("property") + 1


def test_pledge_and_court_keep_their_scope_note_when_unchecked() -> None:
    """Оговорку охвата страница в этой ветке печатала, а текст — нет."""
    pledge = reporting._pledge_block(report_with(ProviderName.PLEDGE, "not_configured"))
    court = reporting._court_block(report_with(ProviderName.COURT, "not_configured"))

    assert reporting.PLEDGE_SCOPE_NOTE in pledge
    assert reporting.COURT_SCOPE_NOTE in court


def test_own_database_failure_is_not_an_absence_of_the_debtor() -> None:
    """Упавшая БД и нечитаемая выгрузка 1С печатались как «совпадений нет»."""
    text = reporting._internal_block(report_with(ProviderName.INTERNAL, "poll_timeout"))

    assert reporting.INTERNAL_NOT_FOUND not in text
    assert "poll_timeout" in text


def test_own_database_still_shows_the_record_it_found() -> None:
    """Состояние источника не отменяет уже найденную запись."""
    record = InternalDebtorRecord(full_name="Тестов Андрей Сергеевич", debtor_id="42")
    report = report_with(ProviderName.INTERNAL, "partial", internal_records=[record])

    assert "Тестов Андрей Сергеевич" in reporting._internal_block(report)


def test_a_running_out_balance_does_not_read_like_a_timeout() -> None:
    """Кончившиеся оплаченные запросы чинит оператор, зависший источник — нет."""
    paid = reporting._enforcement_block(report_with(ProviderName.FSSP, "payment_required"))
    slow = reporting._enforcement_block(report_with(ProviderName.FSSP, "poll_timeout"))

    assert paid != slow
    assert "оплаченные запросы" in paid
    assert "не успел подготовить ответ" in slow


def test_a_company_found_by_inn_is_visible_on_the_page() -> None:
    """Название ООО не является именем человека, и по ФИО оно не сопоставляется.

    Страница отсеивала такую связь по ``is_usable`` и подписывала «сопоставить
    не удалось ни одну» — найденное, показанное как ненайденное.
    """
    relation = BusinessRelation(
        name="ООО РОМАШКА",
        inn="6446011111",
        entity_type=EntityType.LEGAL_ENTITY,
        status=BusinessStatus.ACTIVE,
        role=BusinessRole.FOUNDER,
        match_confidence=0.0,
        linked_by_identifier=True,
    )
    subject = SearchSubject(search_type=SearchType.PERSON.value)
    report = DebtorReport(
        subject=subject,
        business_relations=[relation],
        provider_results=[
            ProviderResult(
                provider=ProviderName.FNS,
                status=ProviderStatus.SUCCESS,
                records=[relation],
            )
        ],
    )

    page = plain(render.business_section(report))
    assert "ООО РОМАШКА" in page
    assert "сопоставить с должником не удалось" not in page
    assert "ООО РОМАШКА" in reporting._business_block(report)


def test_a_provider_out_of_money_says_so_in_words() -> None:
    """«unauthorized» — латинское слово там, где решают, верить ли отчёту.

    Найдено на проде: у поставщика кончился баланс, все источники ответили
    «недоступно (unauthorized)», и отчёт стал неотличим от честного «ничего не
    нашли». Это худший вид молчания — оплаченный: владелец видит пустые
    разделы и делает вывод о должнике, а вывод надо делать о своём счёте.

    Имени поставщика и переменных окружения в строке нет: правило 4 — оператор
    видит причину, а не наше устройство.
    """
    from app.domain.enums import ProviderName, ProviderStatus
    from app.domain.models import ProviderResult
    from app.services import reporting

    result = ProviderResult(
        provider=ProviderName.FSSP,
        status=ProviderStatus.UNAVAILABLE,
        error_code="unauthorized",
        error_message="Проверьте баланс и токен доступа (X-API-KEY)",
    )
    state = reporting.source_state(result)

    assert state.code is reporting.SourceStateCode.UNAVAILABLE
    assert "unauthorized" not in state.label
    assert "средств" in state.label or "доступа" in state.label
    # Ни ключа, ни имени поставщика, ни адреса поддержки наружу.
    for leak in ("X-API-KEY", "newdb", "NewDB", "@"):
        assert leak not in state.label


def test_a_slow_provider_is_told_apart_from_an_empty_one() -> None:
    """«Не успел ответить» и «ответил, ничего нет» — разные новости.

    Первая чинится повтором через несколько минут, вторая не чинится ничем.
    Сливать их в один код значит предлагать владельцу платить за повтор там,
    где повторять нечего, — и не предлагать там, где стоит.
    """
    from app.domain.enums import ProviderName, ProviderStatus
    from app.domain.models import ProviderResult
    from app.services import reporting

    slow = reporting.source_state(
        ProviderResult(
            provider=ProviderName.FSSP,
            status=ProviderStatus.UNAVAILABLE,
            error_code="poll_timeout",
        )
    )
    empty = reporting.source_state(
        ProviderResult(provider=ProviderName.FSSP, status=ProviderStatus.NO_RESULTS)
    )

    assert slow.label != empty.label
    assert "повтор" in slow.label
    assert not slow.answered and empty.answered
