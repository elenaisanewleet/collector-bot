"""Веб-отчёты: ссылки, страницы, выгрузка и то, что на них нельзя написать."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Sequence
from datetime import date, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.container import Container
from app.db.repository import AuditRepository, ShareLinkRepository
from app.domain.enums import (
    BankruptcyStatus,
    BusinessStatus,
    ProviderName,
    ProviderStatus,
    Region,
    SearchType,
)
from app.domain.identity import SearchSubject, parse_fio
from app.domain.models import DebtorReport, ProviderResult
from app.services.aggregation import Aggregator
from app.services.reporting import render_report, source_state, unanswered_line
from app.services.scoring import RecoveryScoreEngine
from app.services.share import ShareKind, ShareLinkService, ShareTarget
from app.utils.dates import utcnow
from app.web import render
from app.web.app import build_app
from app.web.style import CSS
from tests.conftest import (
    make_bankruptcy,
    make_business,
    make_court_case,
    make_pledge,
    make_proceeding,
    provider_result,
)

OPERATOR_ID = 111
PUBLIC_URL = "https://reports.example.test"

# Состояния, в которых источник НЕ ответил. Ниже они гоняются по одному набору
# проверок: страница обязана описывать каждое теми же словами, что и текстовый
# отчёт, и ни одно из них не должно читаться как «проверено, чисто».
UNCHECKED_STATES: tuple[tuple[str, ProviderResult | None], ...] = (
    ("not_queried", None),
    (
        "not_configured",
        ProviderResult(provider=ProviderName.FSSP, status=ProviderStatus.NOT_CONFIGURED),
    ),
    (
        "insufficient",
        ProviderResult(
            provider=ProviderName.FSSP,
            status=ProviderStatus.ERROR,
            error_code="insufficient_query",
            error_message="Для поиска в ФССП нужно ФИО",
        ),
    ),
    (
        "unavailable",
        ProviderResult(
            provider=ProviderName.FSSP,
            status=ProviderStatus.UNAVAILABLE,
            error_code="timeout",
        ),
    ),
    (
        "error",
        ProviderResult(
            provider=ProviderName.FSSP, status=ProviderStatus.ERROR, error_code="http_502"
        ),
    ),
)
UNCHECKED_IDS = [name for name, _ in UNCHECKED_STATES]


@pytest.fixture
def web_container(container: Container) -> Container:
    """Контейнер с включёнными веб-ссылками.

    Копия существующего, а не сборка по полям заново: иначе новый сервис в
    ``Container`` пришлось бы дописывать ещё и здесь, а забытый упал бы не там,
    где ошибка.
    """
    settings = container.settings.model_copy(update={"web_public_url": PUBLIC_URL})
    return dataclasses.replace(
        container,
        settings=settings,
        share_service=ShareLinkService(settings, container.database),
    )


async def _make_report(container: Container, fio: str, dob: date) -> int:
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio(fio),
        birth_date=dob,
        regions=(Region.MOSCOW.value,),
    )
    outcome = await container.search_service.search_detailed(
        subject, telegram_user_id=OPERATOR_ID, force_refresh=True
    )
    assert outcome.request_id is not None
    return outcome.request_id


def _path(url: str) -> str:
    return "/" + url.split("/", maxsplit=3)[3]


def _report_for(person_subject: SearchSubject, results: Sequence[ProviderResult]) -> DebtorReport:
    report = Aggregator().build(person_subject, list(results))
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


# ---------------------------------------------------------------- ссылки


async def test_link_is_not_issued_without_a_public_url(container: Container) -> None:
    """Без публичного адреса бот не должен слать URL, который не откроется."""
    assert not container.share_service.enabled
    assert (
        await container.share_service.issue(
            ShareTarget(ShareKind.REPORT, 1), telegram_user_id=OPERATOR_ID
        )
        is None
    )


async def test_link_is_issued_and_reused(web_container: Container) -> None:
    target = ShareTarget(ShareKind.REPORT, 1)
    first = await web_container.share_service.issue(target, telegram_user_id=OPERATOR_ID)
    second = await web_container.share_service.issue(target, telegram_user_id=OPERATOR_ID)

    assert first is not None
    assert first.startswith(f"{PUBLIC_URL}/r/")
    # Повтор не плодит адреса: иначе отозвать их все было бы нечем.
    assert first == second


async def test_token_is_long_enough_to_be_unguessable(web_container: Container) -> None:
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, 1), telegram_user_id=OPERATOR_ID
    )
    assert url is not None
    assert len(url.rsplit("/", maxsplit=1)[-1]) >= 40


async def test_expired_link_does_not_resolve(web_container: Container) -> None:
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, 1), telegram_user_id=OPERATOR_ID
    )
    assert url is not None
    token = url.rsplit("/", maxsplit=1)[-1]

    async with web_container.database.session() as session:
        link = await ShareLinkRepository(session).find_active(token)
        assert link is not None
        link.expires_at = utcnow() - timedelta(minutes=1)

    assert await web_container.share_service.resolve(token, ShareKind.REPORT) is None


async def test_a_report_token_does_not_open_the_queue(web_container: Container) -> None:
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, 1), telegram_user_id=OPERATOR_ID
    )
    assert url is not None
    token = url.rsplit("/", maxsplit=1)[-1]
    assert await web_container.share_service.resolve(token, ShareKind.QUEUE) is None


async def test_revoked_link_stops_opening(web_container: Container) -> None:
    """Сценарий отзыва один и он бытовой: переслал не туда."""
    target = ShareTarget(ShareKind.REPORT, 1)
    url = await web_container.share_service.issue(target, telegram_user_id=OPERATOR_ID)
    assert url is not None
    token = url.rsplit("/", maxsplit=1)[-1]

    assert await web_container.share_service.revoke(target, telegram_user_id=OPERATOR_ID) == 1
    assert await web_container.share_service.resolve(token, ShareKind.REPORT) is None

    # И перевыпуск не возвращает скомпрометированный адрес.
    fresh = await web_container.share_service.issue(target, telegram_user_id=OPERATOR_ID)
    assert fresh is not None
    assert fresh != url


async def test_revoke_all_closes_every_live_link(web_container: Container) -> None:
    await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, 1), telegram_user_id=OPERATOR_ID
    )
    await web_container.share_service.issue(
        ShareTarget(ShareKind.QUEUE, 2), telegram_user_id=OPERATOR_ID
    )
    assert await web_container.share_service.revoke_all(telegram_user_id=OPERATOR_ID) == 2


async def test_queue_link_expires_sooner_than_a_report_link(web_container: Container) -> None:
    """За ссылкой на очередь стоит вся выгрузка, а не один человек."""
    settings = web_container.settings
    assert settings.share_queue_ttl_hours < settings.share_link_ttl_hours

    for kind, target_id in ((ShareKind.REPORT, 1), (ShareKind.QUEUE, 2)):
        assert (
            await web_container.share_service.issue(
                ShareTarget(kind, target_id), telegram_user_id=OPERATOR_ID
            )
            is not None
        )

    async with web_container.database.session() as session:
        repo = ShareLinkRepository(session)
        report_link = await repo.find_for_target(ShareKind.REPORT.value, 1)
        queue_link = await repo.find_for_target(ShareKind.QUEUE.value, 2)
    assert report_link is not None
    assert queue_link is not None
    assert queue_link.expires_at < report_link.expires_at


async def test_issuing_and_opening_are_audited_without_the_token(
    web_container: Container,
) -> None:
    """После инцидента /audit — первое, куда смотрят."""
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, 1), telegram_user_id=OPERATOR_ID
    )
    assert url is not None
    token = url.rsplit("/", maxsplit=1)[-1]
    await web_container.share_service.resolve(token, ShareKind.REPORT)

    async with web_container.database.session() as session:
        events = await AuditRepository(session).recent(limit=20)
    actions = [event.action for event in events]
    assert "share.issued" in actions
    assert "share.opened" in actions
    # Токен в аудит не попадает: иначе журнал сам станет хранилищем ключей.
    assert all(token not in (event.detail or "") for event in events)
    assert all(token not in (event.entity_id or "") for event in events)


# ---------------------------------------------------------------- страницы


async def test_report_page_renders(web_container: Container) -> None:
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Демов Максим Игоревич", date(1990, 11, 3))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        response = await client.get(_path(url))
        body = await response.text()

    assert response.status == 200
    assert "Демов Максим Игоревич" in body
    assert "Не подавать" in body
    assert "77012/26/77018-ИП" in body
    # Страница с персональными данными не должна индексироваться и кэшироваться.
    assert "noindex" in response.headers["X-Robots-Tag"]
    assert "no-store" in response.headers["Cache-Control"]
    assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]


async def test_report_page_shows_pledges_and_court_cases(web_container: Container) -> None:
    """Ради этих двух разделов страницу и открывают.

    Найденный залог на машину должника раньше был виден только строкой
    «Залоги | 1 зап.» в списке источников: страница сообщала, что запись есть,
    и не показывала её.
    """
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Демов Максим Игоревич", date(1990, 11, 3))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None
    report = await web_container.search_service.load_report(request_id)
    assert report is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        html = await (await client.get(_path(url))).text()
    text = render_report(report)

    assert 'id="pledge"' in html
    assert 'id="court"' in html
    for fragment in ("KIA RIO", "А40-227414/2026"):
        assert fragment in text, "демо-данные должны нести залог и арбитражное дело"
        assert fragment in html


def test_scope_notes_reach_the_page(person_subject: SearchSubject) -> None:
    """«Не найдено» без оговорки об охвате читается шире проверенного."""
    from app.services.reporting import COURT_SCOPE_NOTE, PLEDGE_SCOPE_NOTE

    empty = _report_for(
        person_subject,
        [
            provider_result(ProviderName.PLEDGE, ProviderStatus.NO_RESULTS),
            provider_result(ProviderName.COURT, ProviderStatus.NO_RESULTS),
        ],
    )
    assert PLEDGE_SCOPE_NOTE in render.pledge_section(empty)
    assert COURT_SCOPE_NOTE in render.court_section(empty)

    # И когда записи нашлись — тоже: охват источника от этого не меняется.
    found = _report_for(
        person_subject,
        [
            provider_result(ProviderName.PLEDGE, ProviderStatus.SUCCESS, [make_pledge()]),
            provider_result(ProviderName.COURT, ProviderStatus.SUCCESS, [make_court_case()]),
        ],
    )
    assert PLEDGE_SCOPE_NOTE in render.pledge_section(found)
    assert COURT_SCOPE_NOTE in render.court_section(found)


@pytest.mark.parametrize(("name", "result"), UNCHECKED_STATES, ids=UNCHECKED_IDS)
def test_every_unchecked_state_uses_the_shared_wording(
    name: str, result: ProviderResult | None, person_subject: SearchSubject
) -> None:
    """Формулировок состояний ровно один набор — в reporting.

    Второй вывод разъедется по одной фразе за правку, и «не проверено»
    где-нибудь да прочитается как «чисто». Тест ломается при любом дрейфе.
    """
    report = _report_for(person_subject, [result] if result is not None else [])
    html = render.enforcement_section(report)

    line = unanswered_line(result)
    assert line is not None
    assert render.e(line) in html
    # И чип состояния в списке источников берётся оттуда же.
    assert render.e(source_state(result).label) in render.sources_section(report)


@pytest.mark.parametrize(("name", "result"), UNCHECKED_STATES, ids=UNCHECKED_IDS)
def test_unchecked_state_is_never_rendered_as_clean(
    name: str, result: ProviderResult | None, person_subject: SearchSubject
) -> None:
    report = _report_for(person_subject, [result] if result is not None else [])
    html = render.enforcement_section(report)
    assert "не найдено" not in html.lower()
    assert "unchecked" in html


def test_answered_and_empty_source_is_rendered_as_checked(
    person_subject: SearchSubject,
) -> None:
    report = _report_for(
        person_subject, [provider_result(ProviderName.FSSP, ProviderStatus.NO_RESULTS)]
    )
    html = render.enforcement_section(report)
    assert "Активных исполнительных производств не найдено" in html
    assert "Проверено:" in html
    assert "unchecked" not in html


def test_hero_says_how_many_sources_answered(person_subject: SearchSubject) -> None:
    """Вердикт с пошлиной не объявляется так, будто данные полные."""
    report = _report_for(
        person_subject,
        [
            provider_result(ProviderName.FSSP, ProviderStatus.NO_RESULTS),
            provider_result(ProviderName.FEDRESURS, ProviderStatus.NOT_CONFIGURED),
        ],
    )
    line = render.coverage_line(report)
    assert "ответили" in line
    assert "вердикт посчитан по неполным данным" in line
    assert "Не проверено: ЕФРСБ" in line


def test_sources_list_names_providers_that_never_answered(
    person_subject: SearchSubject,
) -> None:
    """Источник без результата иначе просто исчезал из списка."""
    report = _report_for(
        person_subject, [provider_result(ProviderName.FSSP, ProviderStatus.NO_RESULTS)]
    )
    html = render.sources_section(report)
    for title in ("ЕФРСБ", "ФНС", "Залоги", "Суды", "Наши данные"):
        assert title in html
    assert "не опрашивался" in html
    answered, total, _ = render.coverage(report)
    assert f"Ответили {answered} из {total}." in html


def test_bankruptcy_unknown_status_is_not_printed_as_completed(
    person_subject: SearchSubject,
) -> None:
    """«Состояние не прочитано» — не «процедура завершена»."""
    record = make_bankruptcy()
    record.status = BankruptcyStatus.UNKNOWN
    report = _report_for(
        person_subject, [provider_result(ProviderName.FEDRESURS, ProviderStatus.SUCCESS, [record])]
    )
    html = render.bankruptcy_section(report)
    assert "состояние процедуры не определено" in html
    assert ">завершено<" not in html


def test_business_unknown_status_is_not_printed_as_terminated(
    person_subject: SearchSubject,
) -> None:
    record = make_business()
    record.status = BusinessStatus.UNKNOWN
    report = _report_for(
        person_subject, [provider_result(ProviderName.FNS, ProviderStatus.SUCCESS, [record])]
    )
    html = render.business_section(report)
    assert "состояние не определено" in html
    assert ">прекращено<" not in html


def test_enforcement_total_is_not_zero_when_amounts_are_unknown(
    person_subject: SearchSubject,
) -> None:
    """Ноль вместо «неизвестно» в единственной денежной цифре раздела."""
    record = make_proceeding()
    record.amount = None
    report = _report_for(
        person_subject, [provider_result(ProviderName.FSSP, ProviderStatus.SUCCESS, [record])]
    )
    html = render.enforcement_section(report)
    assert "Подтверждённая сумма: неизвестна" in html
    assert "Подтверждённая сумма: 0" not in html


def test_filtered_out_records_are_counted_not_dropped(person_subject: SearchSubject) -> None:
    """Отфильтрованное молча превращалось в отсутствующее.

    ``birth_date=None`` здесь обязателен и означает ровно то, что написано:
    источник не дал ни одного уточняющего идентификатора. С совпавшей датой
    рождения запись теперь набирает 0.55 и остаётся в отчёте подписанной как
    «возможное совпадение» — это намеренное правило матчера, а не утечка мимо
    фильтра. Слабой запись делает именно отсутствие идентификаторов, и только
    такую имеет смысл считать скрытой.
    """
    weak = make_proceeding(
        number="2/26/77001-ИП",
        name="Другов Пётр Иванович",
        birth_date=None,
        confidence=0.2,
    )
    report = _report_for(
        person_subject, [provider_result(ProviderName.FSSP, ProviderStatus.SUCCESS, [weak])]
    )
    html = render.enforcement_section(report)
    assert "Источник вернул ещё 1 записей" in html


def test_score_without_factors_says_so(person_subject: SearchSubject) -> None:
    """Базовые 50 не должны выглядеть как измеренная средняя перспектива."""
    from app.services.reporting import NO_FACTORS_NOTE

    report = _report_for(person_subject, [])
    html = render.score_section(report)
    assert NO_FACTORS_NOTE in html
    assert "Оценка на неполных данных" in html


def test_internal_source_failure_is_not_reported_as_no_match(
    person_subject: SearchSubject,
) -> None:
    """Упавшая база не должна давать ту же строку, что честный пустой ответ."""
    broken = ProviderResult(
        provider=ProviderName.INTERNAL,
        status=ProviderStatus.ERROR,
        error_code="internal_source_failed",
    )
    report = _report_for(person_subject, [broken])
    html = render.internal_section(report)
    assert "Совпадений во внутренней базе нет" not in html
    assert "unchecked" in html


async def test_internal_lookup_reports_its_own_state(container: Container) -> None:
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio("Никого Нет Такого"),
        birth_date=date(1970, 1, 1),
    )
    _records, result = await container.search_service.lookup_internal_result(subject)
    assert result.provider is ProviderName.INTERNAL
    assert result.status is ProviderStatus.NO_RESULTS

    nameless = SearchSubject(search_type=SearchType.PERSON.value)
    _none, empty = await container.search_service.lookup_internal_result(nameless)
    assert empty.error_code == "insufficient_query"


def test_navigation_never_links_to_a_missing_section(person_subject: SearchSubject) -> None:
    report = _report_for(person_subject, [])
    report.recovery_score = None
    blocks = render.build_blocks(report)
    assert all(block.html for block in blocks)
    assert "score" not in {block.anchor for block in blocks}


async def test_report_page_never_says_clean_about_an_unchecked_source(
    web_container: Container,
) -> None:
    """Главный инвариант проекта должен держаться и в вебе."""
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Тестов Андрей Сергеевич", date(1985, 3, 12))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        body = await (await client.get(_path(url))).text()

    # Неподключённые источники названы, а не пропущены и не выданы за чистые.
    assert "не подключено" in body
    assert "Наследственные дела" in body


async def test_demo_mode_is_visible_on_the_page(web_container: Container) -> None:
    """Пересланная ссылка на выдуманные данные обязана себя называть."""
    from app.services.reporting import DEMO_BANNER

    assert web_container.settings.is_demo
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Тестов Андрей Сергеевич", date(1985, 3, 12))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        body = await (await client.get(_path(url))).text()
    assert render.e(DEMO_BANNER) in body


async def test_report_page_masks_the_phone(web_container: Container) -> None:
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Тестов Андрей Сергеевич", date(1985, 3, 12))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        body = await (await client.get(_path(url))).text()

    assert "+79991234501" not in body
    assert "***" in body


async def test_page_title_does_not_carry_the_name(web_container: Container) -> None:
    """Вставленный текстом адрес соберёт превью — и заголовок уедет в чужой кэш."""
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Тестов Андрей Сергеевич", date(1985, 3, 12))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        body = await (await client.get(_path(url))).text()

    title = re.search(r"<title>(.*?)</title>", body)
    assert title is not None
    assert "Тестов" not in title.group(1)
    assert "Тестов Андрей Сергеевич" in body  # в H1 оно есть — там оно и нужно


async def test_page_loads_nothing_from_third_parties(web_container: Container) -> None:
    """За страницей персданные; ходить за шрифтами к третьей стороне нечего."""
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Тестов Андрей Сергеевич", date(1985, 3, 12))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        body = await (await client.get(_path(url))).text()
    assert "fonts.googleapis.com" not in body
    assert "fonts.gstatic.com" not in body
    # Ни одной ссылки или ресурса за пределы страницы: только якоря и
    # относительные адреса выгрузки за тем же токеном.
    assert re.search(r'(?:src|href)="(?:https?:)?//', body) is None


async def test_queue_page_renders(web_container: Container) -> None:
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    summary = await web_container.batch_service.run(telegram_user_id=OPERATOR_ID)
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.QUEUE, summary.run_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        response = await client.get(_path(url))
        body = await response.text()

    assert response.status == 200
    assert "Очередь взыскания" in body
    # ФИО в таблице на восемьсот строк сокращены: полное имя есть в отчёте.
    assert "Демов М. И." in body
    assert "Демов Максим Игоревич" not in body
    assert "Судебный приказ" in body


def test_failed_queue_row_is_not_a_verdict() -> None:
    """Сбой проверки — собственное состояние, а не жёлтое «проверить руками»."""
    from app.db.models import BatchItem
    from app.web.render_queue import FAILED, FAILED_TITLE, _row, _row_tone

    item = BatchItem(
        batch_run_id=1,
        debtor_id=1,
        verdict="review",
        verdict_order=2,
        headline="Проверка не выполнена",
        error="TimeoutError",
    )
    cells = "".join(_row(item))
    assert FAILED_TITLE in cells
    assert "Проверить руками" not in cells
    assert 'data-v="failed"' in cells
    assert _row_tone(item) == FAILED


async def test_unknown_token_is_not_found(web_container: Container) -> None:
    async with TestClient(TestServer(build_app(web_container))) as client:
        response = await client.get("/r/definitely-not-a-real-token")
        body = await response.text()

    assert response.status == 404
    # Одна и та же страница на «нет такого» и «истекла»: разница никому не нужна,
    # а разговорчивый ответ помогает перебирать токены.
    assert "Ссылка недоступна" in body


async def test_healthcheck(web_container: Container) -> None:
    async with TestClient(TestServer(build_app(web_container))) as client:
        response = await client.get("/healthz")
        assert response.status == 200
        assert (await response.json())["status"] == "ok"


# ---------------------------------------------------------------- выгрузка


async def test_text_export_matches_the_report_sent_to_chat(web_container: Container) -> None:
    """Второго текстового отчёта у системы нет и заводить его нельзя."""
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Демов Максим Игоревич", date(1990, 11, 3))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        response = await client.get(_path(url) + "/report.txt")
        body = await response.text()

    report = await web_container.search_service.load_report(request_id)
    assert report is not None
    assert body == render_report(report, demo_mode=web_container.settings.is_demo)
    assert response.status == 200
    assert "attachment" in response.headers["Content-Disposition"]
    # В имени файла нет ФИО: он живёт в «Загрузках» дольше, чем сама ссылка.
    assert "Демов" not in response.headers["Content-Disposition"]


async def test_exports_require_a_valid_token(web_container: Container) -> None:
    """Отдельного открытого адреса у файла быть не должно."""
    async with TestClient(TestServer(build_app(web_container))) as client:
        for path in (
            "/r/not-a-token/report.txt",
            "/r/not-a-token/print",
            "/q/not-a-token/queue.csv",
            "/q/not-a-token/print",
        ):
            response = await client.get(path)
            assert response.status == 404, path
            assert "Ссылка недоступна" in await response.text()


async def test_report_token_does_not_open_the_queue_export(web_container: Container) -> None:
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Демов Максим Игоревич", date(1990, 11, 3))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None
    token = url.rsplit("/", maxsplit=1)[-1]

    async with TestClient(TestServer(build_app(web_container))) as client:
        assert (await client.get(f"/q/{token}/queue.csv")).status == 404


async def test_revoked_link_closes_the_exports_too(web_container: Container) -> None:
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Демов Максим Игоревич", date(1990, 11, 3))
    target = ShareTarget(ShareKind.REPORT, request_id)
    url = await web_container.share_service.issue(target, telegram_user_id=OPERATOR_ID)
    assert url is not None
    await web_container.share_service.revoke(target, telegram_user_id=OPERATOR_ID)

    async with TestClient(TestServer(build_app(web_container))) as client:
        for suffix in ("", "/print", "/report.txt"):
            assert (await client.get(_path(url) + suffix)).status == 404


async def test_print_page_is_the_same_report_ready_to_print(web_container: Container) -> None:
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Демов Максим Игоревич", date(1990, 11, 3))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        body = await (await client.get(_path(url) + "/print")).text()

    assert "window.print()" in body
    assert "Демов Максим Игоревич" in body
    # Колонтитул с датой формирования, повторяемый браузером на каждом листе.
    assert 'class="printfoot"' in body
    assert "Сформировано" in body
    # Интерфейсного на печатной странице нет.
    assert 'class="actions"' not in body


async def test_queue_csv_export(web_container: Container) -> None:
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    summary = await web_container.batch_service.run(telegram_user_id=OPERATOR_ID)
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.QUEUE, summary.run_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(web_container))) as client:
        response = await client.get(_path(url) + "/queue.csv")
        payload = await response.read()

    assert response.status == 200
    assert payload.startswith(b"\xef\xbb\xbf"), "без BOM Excel ломает кириллицу"
    assert "вердикт" in payload.decode("utf-8-sig")


async def test_export_buttons_are_on_the_page(web_container: Container) -> None:
    """Выгрузка живёт на самой странице, а не только в боте."""
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _make_report(web_container, "Демов Максим Игоревич", date(1990, 11, 3))
    url = await web_container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=OPERATOR_ID
    )
    assert url is not None
    token = url.rsplit("/", maxsplit=1)[-1]

    async with TestClient(TestServer(build_app(web_container))) as client:
        body = await (await client.get(_path(url))).text()

    assert f'href="/r/{token}/print"' in body
    assert f'href="/r/{token}/report.txt"' in body


# ---------------------------------------------------------------- печать


def _print_css() -> str:
    return CSS[CSS.index("@media print{") :]


def test_print_stylesheet_is_black_and_white() -> None:
    """Распечатку подшивают к делу, и тёмная тема на неё не попадает."""
    assert "@page{size:A4" in CSS
    for token in ("--good:#000", "--warn:#000", "--crit:#000", "--paper:#fff"):
        assert token in _print_css()


def test_source_states_are_distinguishable_without_colour() -> None:
    """Шесть состояний на ч/б принтере обязаны различаться формой и знаком.

    «Не проверено», ставшее на бумаге неотличимым от «проверено, записей нет», —
    то же нарушение инварианта, только на листе, который уходит в дело.
    """
    unchecked_marks = {source_state(result).mark for _name, result in UNCHECKED_STATES}
    answered_marks = {
        source_state(ProviderResult(provider=ProviderName.FSSP, status=status)).mark
        for status in (ProviderStatus.SUCCESS, ProviderStatus.NO_RESULTS)
    }
    assert all(mark.strip() for mark in unchecked_marks | answered_marks)
    # Ответивший и непроверенный источник не могут совпасть по знаку.
    assert not (unchecked_marks & answered_marks)

    # Разметка несёт знак отдельным элементом, а не только цветной чип.
    for _name, result in UNCHECKED_STATES:
        html = render.state_tag(source_state(result))
        assert 'class="mark"' in html
        assert "unchecked" in html
    for status in (ProviderStatus.SUCCESS, ProviderStatus.NO_RESULTS):
        html = render.state_tag(
            source_state(ProviderResult(provider=ProviderName.FSSP, status=status))
        )
        assert "unchecked" not in html

    # У непроверенного — штриховка и пунктирная рамка, различимые без цвета,
    # и они переживают печать: браузер по умолчанию фоны не печатает.
    print_css = _print_css()
    assert ".tag.unchecked{border-style:dashed" in print_css
    assert "repeating-linear-gradient" in print_css
    assert "print-color-adjust:exact" in print_css


def test_print_expands_collapsed_blocks_and_hides_the_interface() -> None:
    print_css = _print_css()
    assert "details{display:block}" in print_css
    hidden = re.search(r"\n  ([^\n]*?)\{display:none!important\}", print_css)
    assert hidden is not None
    # Интерфейсное на бумагу не уходит: оглавление, фильтры, поиск с сортировкой,
    # счётчик показанного и кнопка «показать ещё».
    for selector in ("nav", ".filters", ".actions", ".copyhint", ".tools", ".qstatus", ".more"):
        assert selector in hidden.group(1).split(",")
    # Свёрнутое раскрывается и скриптом — CSS этого не умеет во всех браузерах.
    assert "beforeprint" in render._SCRIPT
    # Внешние ссылки печатаются текстом.
    assert 'a[href^="http"]::after{content:" (" attr(href) ")"' in print_css


def test_print_keeps_tables_and_headings_together() -> None:
    print_css = _print_css()
    assert "thead{display:table-header-group}" in print_css
    assert "break-inside:avoid" in print_css
    assert "break-after:avoid" in print_css


# ---------------------------------------------------------------- разметка


def test_escaping_blocks_markup_injection() -> None:
    """Данные приходят из внешних источников и попадают в HTML."""
    assert render.e("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert render.e('" onload="x') == "&quot; onload=&quot;x"


def test_copy_button_never_overwrites_the_value() -> None:
    """Повторный тап копировал слово «скопировано» и затирал им номер дела."""
    html = render.cell("77012/26/77018-ИП", label="Производство", numeric=True, copy=True)
    assert 'data-copy="77012/26/77018-ИП"' in html
    assert "<button" in html, "копирование должно работать и с клавиатуры"
    assert "el.dataset.copy" in render._SCRIPT
    assert "el.textContent ||" not in render._SCRIPT


def test_page_declares_a_dark_theme_it_can_actually_reach() -> None:
    """Переключателя тем нет, значит и мёртвого селектора под него быть не должно."""
    assert "prefers-color-scheme:dark" in CSS
    assert "data-theme" not in CSS
