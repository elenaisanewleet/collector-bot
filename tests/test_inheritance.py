"""Реестр наследственных дел ФНП.

Все тесты стоят вокруг одного свойства источника: **он ищет только по ФИО и
возвращает всех однофамильцев разом**. Живьём «Иванов Иван Иванович» — это 1730
дел в одном ответе, дата рождения запрос не фильтрует, а в самих записях её
часто нет. Поэтому здесь закреплены не «поля разобрались», а границы:

*   совпавшая дата рождения — и только она — делает запись подтверждённой;
*   запись без даты рождения показывается возможным совпадением, а не фактом,
    и в оценку не попадает ни плюсом, ни минусом;
*   сотни однофамильцев отчёт называет числом и словами, а не списком;
*   недоступность, 403, страница без csrf и незнакомое тело — это «не
    проверено», а не «дел не найдено».

Живых вызовов нет и быть не может: сервис отвечает только с российских
адресов. Фикстуры собраны по контракту и обезличены — ФИО, адреса, номера
актов и телефоны вымышленные, форма и длины сохранены.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import (
    MatchLevel,
    MissingInput,
    ProviderName,
    ProviderStatus,
    SearchType,
)
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import DebtorReport, InheritanceCase, ProviderResult
from app.domain.verdict import Verdict
from app.providers.inheritance import (
    API_PATH,
    SEARCH_PAGE_PATH,
    NotariatInheritanceProvider,
    _parse_compact_date,
)
from app.services.aggregation import Aggregator
from app.services.scoring import RecoveryScoreEngine
from app.services.verdict import VerdictEngine
from app.utils.dates import parse_date

BASE_URL = "https://notariat.test"
PAGE_URL = f"{BASE_URL}{SEARCH_PAGE_PATH}"
API_URL = f"{BASE_URL}{API_PATH}"
DATA = Path(__file__).parent / "data"

SESSION_COOKIE = "notariat_session=abc123def456; Path=/; HttpOnly"


def fixture(name: str) -> Any:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def page_html() -> str:
    return (DATA / "notariat_probate_page.html").read_text(encoding="utf-8")


@pytest.fixture
def inheritance_settings(live_settings: Settings) -> Settings:
    return live_settings.model_copy(
        update={
            "inheritance_enabled": True,
            "inheritance_base_url": BASE_URL,
            "request_timeout_seconds": 5,
            # Сырые ответы включены НАМЕРЕННО: провайдер обязан их не сохранять
            # в любом случае — см. test_raw_response_is_never_stored.
            "store_raw_responses": True,
        }
    )


@pytest.fixture
def provider(inheritance_settings: Settings) -> NotariatInheritanceProvider:
    return NotariatInheritanceProvider(inheritance_settings)


def subject(*, birth_date: date | None = date(1985, 3, 12)) -> SearchSubject:
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
        birth_date=birth_date,
    )


def mock_page(*, html: str | None = None) -> respx.Route:
    return respx.get(PAGE_URL).mock(
        return_value=httpx.Response(
            200,
            html=html if html is not None else page_html(),
            headers={"Set-Cookie": SESSION_COOKIE},
        )
    )


def mock_api(payload: Any, *, status: int = 200) -> respx.Route:
    return respx.post(API_URL).mock(return_value=httpx.Response(status, json=payload))


def build_report(subject_: SearchSubject, result: Any) -> DebtorReport:
    report = Aggregator().build(subject_, [result])
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


# ---------------------------------------------------------------- гейты входа


async def test_disabled_source_makes_no_request(live_settings: Settings) -> None:
    """Выключенный источник молчит и не ходит в сеть — и это не «не найдено»."""
    disabled = NotariatInheritanceProvider(live_settings)
    assert not disabled.is_configured

    with respx.mock:
        page = mock_page()
        api = mock_api({"count": 0, "records": []})
        result = await disabled.fetch(subject())

    assert result.status is not ProviderStatus.NO_RESULTS
    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert page.call_count == 0
    assert api.call_count == 0


@respx.mock
async def test_missing_name_is_not_an_empty_register(
    provider: NotariatInheritanceProvider,
) -> None:
    page = mock_page()
    nameless = SearchSubject(search_type=SearchType.CONTRACT.value, contract_number="EV-1")

    result = await provider.fetch(nameless)

    assert result.error_code == "insufficient_query"
    assert result.status is not ProviderStatus.NO_RESULTS
    assert result.missing_input == (MissingInput.NAME.value,)
    assert page.call_count == 0


def test_source_is_free_and_never_inflates_the_batch_estimate(
    provider: NotariatInheritanceProvider,
) -> None:
    """Смету оператор подтверждает перед тратой денег; этот источник бесплатный."""
    assert provider.planned_calls(subject()) == 0
    assert provider.max_planned_calls(subject()) == 0


# ---------------------------------------------------------------- контракт запроса


@respx.mock
async def test_request_carries_csrf_cookie_and_referer(
    provider: NotariatInheritanceProvider,
) -> None:
    """Без cookie, X-CSRFToken и Referer сервис отвечает 400 или 403."""
    page = mock_page()
    api = mock_api(fixture("notariat_probate_cases.json"))

    await provider.fetch(subject())

    assert page.call_count == 1
    assert api.call_count == 1
    request = api.calls[0].request
    assert json.loads(request.content) == {"name": "Тестов Андрей Сергеевич", "args": {}}
    assert request.headers["X-CSRFToken"] == "TEST0CSRF0TOKEN0000000000000000000000000"
    assert request.headers["Referer"] == PAGE_URL
    # Кука, выданная страницей, доехала до запроса: сессия одна на проверку.
    assert "notariat_session=abc123def456" in request.headers["Cookie"]
    assert "Mozilla/5.0" in request.headers["User-Agent"]


@respx.mock
async def test_only_one_pair_of_requests_per_check(
    provider: NotariatInheritanceProvider,
) -> None:
    """Чужой сайт, а не API по договору: одна проверка — один запрос."""
    page = mock_page()
    api = mock_api(fixture("notariat_probate_cases_many.json"))

    await provider.fetch(subject())

    assert (page.call_count, api.call_count) == (1, 1)


# ---------------------------------------------------------------- разбор дат


def test_death_date_is_read_as_year_month_day() -> None:
    """«19760330» — это 30.03.1976, а общий парсер молча отдаёт None.

    ``parse_date`` считает восьмизначную строку ДДММГГГГ, и на этом источнике
    она превращается в ``None``: дата рождения пропадает — запись никогда не
    подтвердится, дата смерти пропадает — факт исчезает из отчёта.
    """
    assert parse_date("19760330") is None
    assert _parse_compact_date("19760330") == date(1976, 3, 30)
    assert _parse_compact_date("19850312") == date(1985, 3, 12)
    assert _parse_compact_date(None) is None
    assert _parse_compact_date("") is None
    # Не восемь цифр — отдаём общему парсеру, он же отвергнет невозможное.
    assert _parse_compact_date("2024-02-06") == date(2024, 2, 6)
    assert _parse_compact_date("19761332") is None


# ---------------------------------------------------------------- подтверждённое дело


@respx.mock
async def test_matching_birth_date_confirms_the_case(
    provider: NotariatInheritanceProvider,
) -> None:
    mock_page()
    mock_api(fixture("notariat_probate_cases.json"))

    result = await provider.fetch(subject())

    assert result.status is ProviderStatus.SUCCESS
    assert len(result.records) == 1
    record = result.records[0]
    assert isinstance(record, InheritanceCase)
    assert record.case_number == "112/2024"
    assert record.deceased_birth_date == date(1985, 3, 12)
    assert record.death_date == date(2024, 1, 15)
    assert record.case_date == date(2024, 2, 6)
    assert record.notary_name == "Образцова Мария Ивановна"
    assert record.chamber_name == "Московская городская нотариальная палата"
    assert record.is_open


@respx.mock
async def test_other_peoples_records_never_reach_the_report(
    provider: NotariatInheritanceProvider,
) -> None:
    """Чужая дата рождения и смерть до рождения должника — не наши записи.

    Ни в отчёт, ни в базу: это персональные данные посторонних людей, и матчер
    их всё равно дисквалифицирует.
    """
    mock_page()
    mock_api(fixture("notariat_probate_cases.json"))

    result = await provider.fetch(subject())

    numbers = {
        record.case_number for record in result.records if isinstance(record, InheritanceCase)
    }
    assert "90/2010" not in numbers  # другая дата рождения
    assert "90/1976" not in numbers  # умер за девять лет до рождения должника


@respx.mock
async def test_confirmed_case_is_matched_scored_and_reaches_the_verdict(
    provider: NotariatInheritanceProvider, settings: Settings
) -> None:
    mock_page()
    mock_api(fixture("notariat_probate_cases.json"))
    person = subject()

    report = build_report(person, await provider.fetch(person))

    case = report.inheritance_cases[0]
    assert case.match_level is MatchLevel.CONFIRMED
    assert report.confirmed_inheritance_cases

    score = report.recovery_score
    assert score is not None
    factor = next(f for f in score.factors if f.name == "confirmed_probate_case")
    assert factor.delta == -35
    assert factor.source is ProviderName.INHERITANCE
    assert "112/2024" in factor.reason

    decision = VerdictEngine(settings).decide(report)
    # REVIEW, а не DROP: долг переходит к наследникам в пределах стоимости
    # наследства, и следующий шаг — запрос нотариусу, а не списание.
    assert decision.verdict is Verdict.REVIEW
    assert "наследник" in decision.headline.lower()
    assert any("112/2024" in reason.text for reason in decision.reasons)


# ---------------------------------------------------------------- однофамильцы


@respx.mock
async def test_record_without_a_birth_date_is_never_confirmed(
    provider: NotariatInheritanceProvider,
) -> None:
    """Запись без даты рождения — «возможное совпадение», и только.

    Уточняющего идентификатора нет, поэтому потолок 0.55 — ровно порог
    «возможного совпадения». Цифра закреплена намеренно: она стоит НА границе,
    и любая правка штрафа за отсутствие различителя перевернёт видимость всего
    раздела разом.
    """
    mock_page()
    mock_api(fixture("notariat_probate_cases_namesakes.json"))
    person = subject()

    result = await provider.fetch(person)
    report = build_report(person, result)

    assert report.inheritance_cases
    for case in report.inheritance_cases:
        assert case.deceased_birth_date is None
        assert case.match_confidence == pytest.approx(0.55)
        assert case.match_level is MatchLevel.PROBABLE
        assert not case.is_confirmed
    assert not report.confirmed_inheritance_cases


@respx.mock
async def test_unconfirmed_namesake_moves_the_score_by_nothing(
    provider: NotariatInheritanceProvider, settings: Settings
) -> None:
    """Ни плюса, ни минуса. −35 за чужую смерть обнулили бы живого должника."""
    mock_page()
    mock_api(fixture("notariat_probate_cases_namesakes.json"))
    person = subject()

    report = build_report(person, await provider.fetch(person))

    score = report.recovery_score
    assert score is not None
    assert not [f for f in score.factors if f.source is ProviderName.INHERITANCE]
    assert VerdictEngine(settings).decide(report).verdict is not Verdict.DROP


@respx.mock
async def test_a_few_namesakes_are_shown_partial_not_empty(
    provider: NotariatInheritanceProvider,
) -> None:
    """«Не нашли» и «нашли, но не сопоставили» — разные ответы."""
    mock_page()
    mock_api(fixture("notariat_probate_cases_namesakes.json"))

    result = await provider.fetch(subject())

    assert result.status is not ProviderStatus.NO_RESULTS
    assert result.is_partial
    assert len(result.records) == 3
    assert "найдено 3 дела" in result.notes[0]


@respx.mock
async def test_hundreds_of_namesakes_are_counted_not_listed(
    provider: NotariatInheritanceProvider,
) -> None:
    """Показать двадцать и промолчать про остальные — запрещено.

    Сотни однофамильцев отчёт обязан назвать числом и словами: ни одна из них
    не сопоставлена, и различить их можно только запросом нотариусу.
    """
    mock_page()
    mock_api(fixture("notariat_probate_cases_many.json"))

    result = await provider.fetch(subject())

    assert result.is_partial
    assert result.records == []
    joined = " ".join(result.notes)
    assert "1730" in joined
    assert "сопоставить по дате рождения не удалось" in joined
    assert "https://notariat.ru/ru-ru/help/probate-cases/" in joined


@respx.mock
async def test_hundreds_of_namesakes_are_named_in_the_report(
    provider: NotariatInheritanceProvider,
) -> None:
    """То же самое — в тексте отчёта и на веб-странице, а не только в notes."""
    from app.services.reporting import INHERITANCE_SCOPE_NOTE, render_report
    from app.web.render import inheritance_section

    mock_page()
    mock_api(fixture("notariat_probate_cases_many.json"))
    person = subject()

    report = build_report(person, await provider.fetch(person))
    text = render_report(report)
    html = inheritance_section(report)

    assert "1730" in text
    assert "не найдено" not in text.split("НАСЛЕДСТВЕННЫЕ ДЕЛА")[1].split("\n\n")[0]
    # Оговорка охвата печатается и здесь: без неё «нашли 1730» читается как
    # «1730 дел должника».
    assert INHERITANCE_SCOPE_NOTE in text
    assert "1730" in html
    assert "только по ФИО" in html


@respx.mock
async def test_report_calls_a_confirmed_case_by_its_name(
    provider: NotariatInheritanceProvider,
) -> None:
    from app.services.reporting import INHERITANCE_SCOPE_NOTE, render_report

    mock_page()
    mock_api(fixture("notariat_probate_cases.json"))
    person = subject()

    text = render_report(build_report(person, await provider.fetch(person)))

    assert "Должник умер" in text
    assert "112/2024" in text
    assert "Образцова Мария Ивановна" in text
    assert INHERITANCE_SCOPE_NOTE in text


def test_death_before_birth_disqualifies_the_record() -> None:
    """Единственный различитель, работающий при пустой дате рождения.

    Проверяется на самом матчере, а не только через фильтр провайдера: запись,
    восстановленная из кэша, проходит тот же путь, и умерший за девять лет до
    рождения должника не должен получить «возможное совпадение».
    """
    from app.services.identity import IdentityMatcher

    person = subject()
    long_dead = InheritanceCase(
        deceased_name="Тестов Андрей Сергеевич",
        deceased_birth_date=None,
        death_date=date(1976, 3, 30),
        case_number="90/1976",
    )
    alive_enough = InheritanceCase(
        deceased_name="Тестов Андрей Сергеевич",
        deceased_birth_date=None,
        death_date=date(2022, 2, 14),
        case_number="221/2022",
    )

    matcher = IdentityMatcher()
    assert matcher.assess(person, long_dead).confidence == pytest.approx(0.05)
    assert matcher.assess(person, alive_enough).confidence == pytest.approx(0.55)
    # Без даты рождения должника сравнивать не с чем — дисквалификации нет.
    nameless_birth = subject(birth_date=None)
    assert matcher.assess(nameless_birth, long_dead).confidence == pytest.approx(0.55)


@respx.mock
async def test_without_a_birth_date_nothing_can_be_confirmed(
    provider: NotariatInheritanceProvider,
) -> None:
    """Дату рождения не дали — подтвердить нечем, и «совпало» сказать не о чем."""
    mock_page()
    mock_api(fixture("notariat_probate_cases.json"))
    person = subject(birth_date=None)

    result = await provider.fetch(person)
    report = build_report(person, result)

    assert result.is_partial
    assert not report.confirmed_inheritance_cases
    assert all(not case.is_confirmed for case in report.inheritance_cases)


# ---------------------------------------------------------------- пустой ответ


@respx.mock
async def test_empty_register_is_the_only_no_results(
    provider: NotariatInheritanceProvider,
) -> None:
    mock_page()
    mock_api(fixture("notariat_probate_cases_empty.json"))

    result = await provider.fetch(subject())

    assert result.status is ProviderStatus.NO_RESULTS
    assert not result.is_partial
    assert result.notes == ()


@respx.mock
async def test_empty_answer_earns_no_bonus(
    provider: NotariatInheritanceProvider,
) -> None:
    """Дело заводится по заявлению наследника: пустой ответ не значит «жив».

    Поэтому положительного фактора у источника нет вовсе — ни при каком ответе.
    """
    mock_page()
    mock_api(fixture("notariat_probate_cases_empty.json"))
    person = subject()

    report = build_report(person, await provider.fetch(person))

    score = report.recovery_score
    assert score is not None
    assert not [f for f in score.factors if f.source is ProviderName.INHERITANCE]


# ---------------------------------------------------------------- отказы источника


@respx.mock
async def test_unreachable_service_is_not_an_empty_register(
    provider: NotariatInheritanceProvider,
) -> None:
    respx.get(PAGE_URL).mock(side_effect=httpx.ConnectError("dns failure"))

    result = await provider.fetch(subject())

    assert result.status is not ProviderStatus.NO_RESULTS
    assert result.status is ProviderStatus.UNAVAILABLE


@respx.mock
async def test_unreachable_service_earns_no_score_factor(
    provider: NotariatInheritanceProvider, settings: Settings
) -> None:
    respx.get(PAGE_URL).mock(side_effect=httpx.ConnectError("dns failure"))
    person = subject()

    report = build_report(person, await provider.fetch(person))

    score = report.recovery_score
    assert score is not None
    assert not [f for f in score.factors if f.source is ProviderName.INHERITANCE]
    # И источник назван в «Ограничениях оценки», а не пропущен молча.
    assert any("Наследственные дела" in note for note in score.confidence_notes)


@respx.mock
async def test_page_without_csrf_token_is_unavailable(
    provider: NotariatInheritanceProvider,
) -> None:
    """Редирект, капча или переверстанная страница — самый вероятный путь лжи.

    GET прошёл, токена нет — и если бы это стало пустым результатом, отчёт
    сказал бы «наследственных дел не найдено», не спросив реестр вовсе.
    """
    mock_page(html="<html><head><title>Ошибка</title></head><body></body></html>")
    api = mock_api({"count": 0, "records": []})

    result = await provider.fetch(subject())

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "csrf_missing"
    assert api.call_count == 0


@respx.mock
async def test_rejected_request_is_an_error(provider: NotariatInheritanceProvider) -> None:
    mock_page()
    respx.post(API_URL).mock(return_value=httpx.Response(403, text="forbidden"))

    result = await provider.fetch(subject())

    assert result.status is not ProviderStatus.NO_RESULTS
    assert result.status is ProviderStatus.UNAVAILABLE
    # Не «unauthorized»: ключа мы этому источнику не предъявляем, отвергать
    # нечего. Код отказа здесь останавливал массовый прогон — см.
    # test_a_free_source_refusing_does_not_stop_the_run.
    assert result.error_code == "rejected"


@respx.mock
async def test_a_403_is_not_a_rejected_key(provider: NotariatInheritanceProvider) -> None:
    """403 у бесплатного реестра — «запрос не понравился», а не «ключ отклонён».

    Разница не косметическая: ``unauthorized`` входит в ``REFUSAL_CODES``, и три
    таких должника подряд останавливали весь прогон на восемьсот строк.
    """
    from app.services.batch import REFUSAL_CODES

    mock_page()
    respx.post(API_URL).mock(return_value=httpx.Response(403, text="forbidden"))

    result = await provider.fetch(subject())

    assert result.error_code not in REFUSAL_CODES
    assert "ключ" not in (result.error_message or "")


async def test_a_free_source_refusing_does_not_stop_the_run() -> None:
    """Бесплатный вспомогательный источник не кладёт платную работу.

    Остановка прогона существует ради денег: «продолжать — значит платить за
    пустоту». У реестра ФНП нет ни счёта, чтобы он кончился, ни ключа, чтобы
    его отклонили, и его отказ — дыра в одной строке, а не во всех оставшихся.
    """
    from app.services.batch import _refusals

    refused = ProviderResult(
        provider=ProviderName.INHERITANCE,
        status=ProviderStatus.UNAVAILABLE,
        error_code="unauthorized",
    )
    paid_one = ProviderResult(
        provider=ProviderName.FSSP,
        status=ProviderStatus.ERROR,
        error_code="payment_required",
    )
    person = subject()
    paid = frozenset({ProviderName.FSSP.value})

    free_only = DebtorReport(subject=person, provider_results=[refused])
    both = DebtorReport(subject=person, provider_results=[refused, paid_one])

    assert _refusals(free_only, paid=paid) == ()
    # А платный источник останавливает по-прежнему — и только он.
    assert _refusals(both, paid=paid) == (ProviderName.FSSP.value,)


def test_the_register_is_declared_free_to_the_batch() -> None:
    """Флаг, на котором держится предыдущий тест, стоит на самом провайдере."""
    from app.providers.fssp import FSSPProvider

    assert NotariatInheritanceProvider.is_free
    # Умолчание платное: новый источник не должен получить право молча
    # отказывать всему прогону.
    assert not FSSPProvider.is_free


@respx.mock
async def test_malformed_json_is_an_error(provider: NotariatInheritanceProvider) -> None:
    mock_page()
    respx.post(API_URL).mock(return_value=httpx.Response(200, text="<html>503</html>"))

    result = await provider.fetch(subject())

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "malformed_json"
    assert result.records == []


@respx.mock
async def test_unknown_schema_is_not_an_empty_register(
    provider: NotariatInheritanceProvider,
) -> None:
    """Строки пришли, ни одна не похожа на дело: схема уехала.

    Разбор, отдавший ноль записей на непустом ответе, выглядит как чистый
    реестр — а это ровно та подмена, которой здесь быть нельзя.
    """
    mock_page()
    mock_api({"count": 2, "records": [{"foo": "bar"}, {"baz": 1}]})

    result = await provider.fetch(subject())

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"


@respx.mock
async def test_answer_without_records_is_not_an_empty_register(
    provider: NotariatInheritanceProvider,
) -> None:
    mock_page()
    mock_api({"total": 5})

    result = await provider.fetch(subject())

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "unexpected_schema"


# ---------------------------------------------------------------- приватность


@respx.mock
async def test_raw_response_is_never_stored(provider: NotariatInheritanceProvider) -> None:
    """Тело ответа — персональные данные тысячи посторонних людей.

    ``redact_sensitive_json`` вырезает закрытый список ключей, и ключей этого
    источника (Fio, Address, DeathActNumber, ContactPhone) в нём нет. Поэтому
    сырое тело не сохраняется вообще, независимо от ``STORE_RAW_RESPONSES``.
    """
    assert provider._settings.store_raw_responses
    mock_page()
    mock_api(fixture("notariat_probate_cases.json"))

    result = await provider.fetch(subject())

    assert result.raw_response is None


@respx.mock
async def test_bystanders_personal_data_is_not_carried(
    provider: NotariatInheritanceProvider,
) -> None:
    """Адрес, актовая запись о смерти и телефон нотариуса в модель не едут."""
    mock_page()
    mock_api(fixture("notariat_probate_cases_namesakes.json"))

    result = await provider.fetch(subject())

    dumped = json.dumps(
        [record.model_dump(mode="json") for record in result.records], ensure_ascii=False
    )
    assert "ул. Примерная" not in dumped
    assert "17022977" not in dumped
    assert "+7 (495)" not in dumped


# ------------------------------------------------------------ сторож схемы


BROKEN_SCHEMAS = fixture("notariat_probate_broken_schemas.json")


@pytest.mark.parametrize(
    "broken",
    [
        "записи списками, а не словарями",
        "записи null",
        "нет ключа count вовсе",
        "count строкой",
    ],
)
@respx.mock
async def test_a_broken_body_is_a_schema_error_not_an_empty_register(
    provider: NotariatInheritanceProvider, broken: str
) -> None:
    """Четыре формы битого тела давали чистое «дел не найдено».

    Сторож смотрел на записи ПОСЛЕ отбора: на каждой из этих четырёх форм отбор
    отдавал пустой список, пустой список — пустой реестр, и отчёт с «наследственных
    дел по этому ФИО не найдено» уходил в суд. Иск к покойному без единого следа.
    """
    from app.services.reporting import render_report

    mock_page()
    mock_api(BROKEN_SCHEMAS[broken])
    person = subject()

    result = await provider.fetch(person)

    assert result.status is not ProviderStatus.NO_RESULTS
    assert result.error_code == "unexpected_schema"
    assert result.records == []
    section = render_report(build_report(person, result)).split("НАСЛЕДСТВЕННЫЕ ДЕЛА")[1]
    assert "не найдено" not in section.split("\n\n")[0]


@respx.mock
async def test_a_non_numeric_count_is_not_replaced_by_the_list_length(
    provider: NotariatInheritanceProvider,
) -> None:
    """Подставить длину списка значит поверить телу, о котором уже известно, что оно не то.

    ``count`` — единственное, что говорит о размере пула однофамильцев, а пул
    входит и в вердикт, и в фактор оценки.
    """
    mock_page()
    mock_api({"count": "1730", "records": [fixture("notariat_probate_cases.json")["records"][0]]})

    result = await provider.fetch(subject())

    assert result.error_code == "unexpected_schema"
    assert result.records == []


@respx.mock
async def test_records_that_did_not_parse_are_counted_as_incompleteness(
    provider: NotariatInheritanceProvider,
) -> None:
    """Расхождение между числом исходных записей и разобранных — неполнота."""
    body = fixture("notariat_probate_cases_namesakes.json")
    body["records"].append(None)
    body["count"] = 4
    mock_page()
    mock_api(body)

    result = await provider.fetch(subject())

    assert result.is_partial
    assert any("прочитать не удалось" in note for note in result.notes)


@respx.mock
async def test_cases_the_registry_did_not_send_are_counted_as_incompleteness(
    provider: NotariatInheritanceProvider,
) -> None:
    """«Нашли 1730, прислали 4» — это «видели не всё», а не «нашли 4»."""
    mock_page()
    mock_api(fixture("notariat_probate_cases_one_in_a_crowd.json"))

    result = await provider.fetch(subject())

    assert result.is_partial
    assert any("1726" in note for note in result.notes)


# ------------------------------------------------- пул однофамильцев в выводе


@respx.mock
async def test_the_size_of_the_pool_reaches_the_verdict_and_the_factor(
    provider: NotariatInheritanceProvider, settings: Settings
) -> None:
    """Одно совпадение из 1730 и одно из одного — утверждения разной силы.

    До сих пор они выглядели одинаково: число найденных по ФИО жило только
    отдельной строкой ПОСЛЕ карточки дела и не доходило ни до вердикта, ни до
    фактора оценки, ни до заголовка в выгрузке очереди.
    """
    mock_page()
    mock_api(fixture("notariat_probate_cases_one_in_a_crowd.json"))
    person = subject()

    report = build_report(person, await provider.fetch(person))

    case = report.inheritance_cases[0]
    assert case.namesake_count == 1730
    score = report.recovery_score
    assert score is not None
    factor = next(f for f in score.factors if f.name == "confirmed_probate_case")
    assert "1730" in factor.reason
    # Заголовок вердикта — это то, что оператор читает в выгрузке очереди.
    assert "1730" in VerdictEngine(settings).decide(report).headline


@respx.mock
async def test_two_matches_with_different_death_dates_cancel_the_confirmation(
    provider: NotariatInheritanceProvider, settings: Settings
) -> None:
    """Один человек умирает один раз.

    Две записи с СОВПАВШЕЙ датой рождения и разными датами смерти — доказанная
    неоднозначность: где-то среди них однофамилец с тем же днём рождения.
    Безоговорочное «должник умер» по такому набору выдавать нельзя.
    """
    from app.services.reporting import render_report

    mock_page()
    mock_api(fixture("notariat_probate_cases_two_matches.json"))
    person = subject()

    result = await provider.fetch(person)
    report = build_report(person, result)

    assert len(report.inheritance_cases) == 2
    assert all(case.contested for case in report.inheritance_cases)
    assert all(case.match_level is MatchLevel.PROBABLE for case in report.inheritance_cases)
    assert not report.confirmed_inheritance_cases
    assert result.is_partial
    assert any("один раз" in note for note in result.notes)

    score = report.recovery_score
    assert score is not None
    assert not [f for f in score.factors if f.source is ProviderName.INHERITANCE]
    assert "должник умер" not in VerdictEngine(settings).decide(report).headline.lower()
    assert "Должник умер" not in render_report(report)


# ---------------------------------------------------------------- доказательство


@respx.mock
async def test_the_birth_date_the_match_rests_on_is_printed(
    provider: NotariatInheritanceProvider,
) -> None:
    """Утверждение «дата рождения совпала» обязано быть проверяемым глазами.

    Дата рождения наследодателя не печаталась нигде — ни в тексте, ни в
    веб-таблице, — хотя на ней одной держатся и подтверждение, и отбраковка.
    """
    from app.services.reporting import render_report
    from app.web.render import inheritance_section

    mock_page()
    mock_api(fixture("notariat_probate_cases.json"))
    person = subject()

    report = build_report(person, await provider.fetch(person))
    text = render_report(report)
    html = inheritance_section(report)

    assert "Дата рождения: 12.03.1985" in text
    assert "Рождение" in html
    assert "12.03.1985" in html


@respx.mock
async def test_a_birth_date_the_registry_withheld_is_printed_too(
    provider: NotariatInheritanceProvider,
) -> None:
    """Обратное утверждение обязано быть опровержимым: пустая дата тоже видна."""
    from app.services.reporting import render_report

    mock_page()
    mock_api(fixture("notariat_probate_cases_namesakes.json"))
    person = subject()

    text = render_report(build_report(person, await provider.fetch(person)))

    assert "Дата рождения: реестр не указал" in text


# ---------------------------------------------------------------- три исхода


@respx.mock
async def test_all_cases_belonging_to_other_people_is_the_strongest_answer(
    provider: NotariatInheritanceProvider,
) -> None:
    """«Ни одно из этих дел не про вашего должника» — не нерешённая тревога.

    Три дела, все положительно исключены (чужая дата рождения, смерть раньше
    рождения должника), а отчёт печатал «найдено 3 дела, сопоставить не удалось
    ни одного» и «однофамильцев слишком много, записи не выводятся».
    """
    from app.services.reporting import render_report

    mock_page()
    mock_api(fixture("notariat_probate_cases_all_other_people.json"))
    person = subject()

    result = await provider.fetch(person)

    assert result.status is ProviderStatus.NO_RESULTS
    assert not result.is_partial
    joined = " ".join(result.notes)
    assert "относятся к другим людям" in joined
    assert "слишком много" not in joined
    assert "сопоставить не удалось ни одного" not in joined
    section = render_report(build_report(person, result)).split("НАСЛЕДСТВЕННЫЕ ДЕЛА")[1]
    assert "относятся к другим людям" in section


@respx.mock
async def test_too_many_namesakes_is_decided_by_how_many_are_indistinguishable(
    provider: NotariatInheritanceProvider,
) -> None:
    """Двадцать пять дел, двадцать три из них чужие — показать надо оставшиеся два.

    Граница считалась по размеру пула, а не по числу неразличимых записей,
    поэтому «однофамильцев слишком много» печаталось над пустотой там, где
    строк было две.
    """
    mock_page()
    mock_api(fixture("notariat_probate_cases_mostly_other_people.json"))

    result = await provider.fetch(subject())

    assert len(result.records) == 2
    joined = " ".join(result.notes)
    assert "слишком много" not in joined
    assert "23 дела отнесены к другим людям" in joined


@respx.mock
async def test_hundreds_of_indistinguishable_records_are_still_only_counted(
    provider: NotariatInheritanceProvider,
) -> None:
    """Обратная сторона: запрет показывать сотни чужих дел никуда не делся."""
    mock_page()
    mock_api(fixture("notariat_probate_cases_many.json"))

    result = await provider.fetch(subject())

    assert result.records == []
    assert "слишком много" in " ".join(result.notes)


# ------------------------------------------------- открытое и закрытое дело


@respx.mock
async def test_a_closed_case_is_never_called_open(
    provider: NotariatInheritanceProvider, settings: Settings
) -> None:
    """Открытое и закрытое дело — разные следующие шаги и разные сроки.

    Даты дела шли через общий ``parse_date``, который на «20150301» отдаёт
    ``None``; дело считалось открытым, и вердикт со скорингом писали «открыто
    наследственное дело» про дело, помеченное закрытым строкой выше.
    """
    from app.services.reporting import render_report

    mock_page()
    mock_api(fixture("notariat_probate_cases_closed.json"))
    person = subject()

    result = await provider.fetch(person)
    record = result.records[0]
    assert isinstance(record, InheritanceCase)
    assert record.case_date == date(2015, 3, 1)
    assert record.case_close_date == date(2015, 11, 20)
    assert not record.is_open

    report = build_report(person, result)
    text = render_report(report)
    assert "Дело 77/2015 — закрыто" in text
    assert "Закрыто: 20.11.2015" in text

    score = report.recovery_score
    assert score is not None
    factor = next(f for f in score.factors if f.name == "confirmed_probate_case")
    assert "наследственное дело закрыто" in factor.reason
    assert "открыто наследственное дело" not in factor.reason

    headline = VerdictEngine(settings).decide(report).headline
    assert "(закрыто)" in headline
    assert "Открыто наследственное дело" not in headline


def test_the_state_of_a_case_survives_a_date_we_could_not_read() -> None:
    """Состояние берётся из записи, а не выводится из разобранной даты."""
    from app.providers.inheritance import _to_case

    case = _to_case(
        {"Fio": "Тестов Андрей Сергеевич", "CaseNumber": "5/2015", "CaseCloseDate": "не указано"}
    )

    assert case.case_close_date is None
    assert not case.is_open


# ---------------------------------------------------------------- чей пробел


@respx.mock
async def test_our_own_missing_birth_date_is_not_blamed_on_the_registry(
    provider: NotariatInheritanceProvider,
) -> None:
    """Даты в записях ЕСТЬ, нет её у нас — и отчёт обязан звать её дописать.

    «Даты рождения в этих записях реестр не указал» в этом случае перекладывает
    наш пробел на источник и закрывает оператору самое дешёвое действие, какое
    здесь бывает: дописать дату рождения и переспросить бесплатный реестр.
    """
    mock_page()
    mock_api(fixture("notariat_probate_cases_dated.json"))

    result = await provider.fetch(subject(birth_date=None))

    joined = " ".join(result.notes)
    assert "реестр не указал" not in joined
    assert "в карточке должника нет даты рождения" in joined
    assert "Добавьте дату рождения" in joined


@respx.mock
async def test_the_registrys_own_gap_is_still_named_as_the_registrys(
    provider: NotariatInheritanceProvider,
) -> None:
    """Обратный случай никуда не делся: дата есть у нас и нет в записях."""
    mock_page()
    mock_api(fixture("notariat_probate_cases_namesakes.json"))

    result = await provider.fetch(subject())

    assert "даты рождения в этих записях реестр не указал" in " ".join(result.notes)


# ---------------------------------------------------------------- мелочи


def test_a_date_that_cannot_be_is_not_a_date() -> None:
    """«20991231» календарно безупречно и как «дата смерти 31.12.2099» читается фактом."""
    assert _parse_compact_date("20991231") is None
    assert _parse_compact_date("18991231") is None
    assert _parse_compact_date("19850312") == date(1985, 3, 12)


@respx.mock
async def test_the_report_agrees_with_its_own_numbers(
    provider: NotariatInheritanceProvider,
) -> None:
    """«Ниже показаны 1 из них» и «3 однофамильцев» — в листе, который уходит в суд."""
    from app.web.render import inheritance_section

    body = fixture("notariat_probate_cases_namesakes.json")
    body["records"] = body["records"][:1]
    body["count"] = 1
    mock_page()
    mock_api(body)
    person = subject()

    report = build_report(person, await provider.fetch(person))

    assert "Ниже показано 1 дело" in " ".join(report.provider_results[0].notes)
    assert "1 однофамилец:" in inheritance_section(report)


@respx.mock
async def test_three_namesakes_are_three_namesakes(
    provider: NotariatInheritanceProvider,
) -> None:
    from app.web.render import inheritance_section

    mock_page()
    mock_api(fixture("notariat_probate_cases_namesakes.json"))
    person = subject()

    report = build_report(person, await provider.fetch(person))

    assert "Ниже показано 3 дела" in " ".join(report.provider_results[0].notes)
    assert "3 однофамильца:" in inheritance_section(report)


# ---------------------------------------------------------------- реестр провайдеров


def test_stub_steps_aside_for_the_real_provider(inheritance_settings: Settings) -> None:
    """Две записи под одним именем означали бы, что одну никто не прочитает."""
    from app.providers.registry import build_external_providers

    providers = build_external_providers(inheritance_settings)
    by_name = {provider.name: provider for provider in providers}

    assert len(by_name) == len(providers)
    assert isinstance(by_name[ProviderName.INHERITANCE], NotariatInheritanceProvider)
    assert by_name[ProviderName.INHERITANCE].is_configured
