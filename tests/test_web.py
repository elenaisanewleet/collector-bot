"""Веб-отчёты: ссылки, страницы и то, что на них нельзя написать."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from app.container import Container
from app.db.repository import ShareLinkRepository
from app.domain.enums import Region, SearchType
from app.domain.identity import SearchSubject, parse_fio
from app.services.share import ShareKind, ShareTarget
from app.utils.dates import utcnow
from app.web.app import build_app

OPERATOR_ID = 111
PUBLIC_URL = "https://reports.example.test"


@pytest.fixture
def web_container(container: Container) -> Container:
    """Контейнер с включёнными веб-ссылками."""
    settings = container.settings.model_copy(update={"web_public_url": PUBLIC_URL})
    from app.services.share import ShareLinkService

    return Container(
        settings=settings,
        database=container.database,
        registry=container.registry,
        search_service=container.search_service,
        import_service=container.import_service,
        batch_service=container.batch_service,
        verdict_engine=container.verdict_engine,
        share_service=ShareLinkService(settings, container.database),
        subject_store=container.subject_store,
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
    assert "Демов Максим Игоревич" in body
    assert "Судебный приказ" in body


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


# ---------------------------------------------------------------- разметка


def test_escaping_blocks_markup_injection() -> None:
    """Данные приходят из внешних источников и попадают в HTML."""
    from app.web.render import e

    assert e("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"
    assert e('" onload="x') == "&quot; onload=&quot;x"


def test_page_declares_both_themes() -> None:
    from app.web.style import CSS

    assert "prefers-color-scheme:dark" in CSS
    assert '[data-theme="dark"]' in CSS
