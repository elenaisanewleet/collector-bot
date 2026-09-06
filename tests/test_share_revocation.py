"""Отзыв ссылки: аварийная кнопка на случай «переслал не туда».

Кнопка эта единственная, поэтому проверяется не число в ответе сервиса, а то,
ради чего её жмут: перестала ли открываться страница, и совпал ли ответ бота с
тем, что на самом деле произошло. Ровно здесь дефект и жил — бот отвечал
«отзывать нечего» про живую ссылку.
"""

from __future__ import annotations

import dataclasses
from datetime import date, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer
from sqlalchemy import func, select

from app.container import Container
from app.db.models import SearchRequest, ShareLink
from app.db.repository import ShareLinkRepository
from app.domain.enums import Region, SearchType
from app.domain.identity import SearchSubject, parse_fio
from app.services.retention import purge_once
from app.services.share import ShareKind, ShareLinkService, ShareTarget
from app.utils.dates import utcnow
from app.web.app import build_app
from tests.bot_harness import SentMessages, dispatcher_for, feed, make_message

OPERATOR_ID = 111
# Второй сотрудник взыскателя из того же списка допущенных: два человека на
# одного должника — самый частый случай, и именно в нём отзыв не работал.
COLLEAGUE_ID = 222
PUBLIC_URL = "https://reports.example.test"

DEBTOR_FIO = "Демов Максим Игоревич"
DEBTOR_BIRTH_DATE = date(1990, 11, 3)


@pytest.fixture
def web_container(container: Container) -> Container:
    """Контейнер с включёнными веб-ссылками — как в ``tests/test_web.py``."""
    settings = container.settings.model_copy(update={"web_public_url": PUBLIC_URL})
    return dataclasses.replace(
        container,
        settings=settings,
        share_service=ShareLinkService(settings, container.database),
    )


def _path(url: str) -> str:
    return "/" + url.split("/", maxsplit=3)[3]


async def _check_debtor(container: Container, *, telegram_user_id: int) -> int:
    """Проверить одного и того же должника от имени сотрудника."""
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio(DEBTOR_FIO),
        birth_date=DEBTOR_BIRTH_DATE,
        regions=(Region.MOSCOW.value,),
    )
    outcome = await container.search_service.search_detailed(
        subject, telegram_user_id=telegram_user_id
    )
    assert outcome.request_id is not None
    return outcome.request_id


async def _link_for(container: Container, *, telegram_user_id: int) -> str:
    """Ссылка на отчёт по должнику — тем же путём, каким её выдаёт бот."""
    request_id = await _check_debtor(container, telegram_user_id=telegram_user_id)
    url = await container.share_service.issue(
        ShareTarget(ShareKind.REPORT, request_id), telegram_user_id=telegram_user_id
    )
    assert url is not None
    return url


async def _page_status(container: Container, url: str) -> int:
    async with TestClient(TestServer(build_app(container))) as client:
        response = await client.get(_path(url))
        return response.status


async def _revoke_command(
    container: Container, bot: Bot, sent: SentMessages, *, user_id: int
) -> str:
    """Прогнать настоящую команду /revoke и вернуть ответ бота."""
    dispatcher: Dispatcher = dispatcher_for(container)
    before = len(sent.texts)
    await feed(dispatcher, bot, message=make_message("/revoke", user_id=user_id))
    answers = sent.texts[before:]
    assert answers, "команда обязана отвечать хоть что-то"
    return "\n".join(answers)


async def _expire(container: Container, url: str) -> None:
    """Отодвинуть срок годности ссылки в прошлое."""
    token = url.rsplit("/", maxsplit=1)[-1]
    async with container.database.session() as session:
        link = await ShareLinkRepository(session).find_active(token)
        assert link is not None
        link.expires_at = utcnow() - timedelta(minutes=1)


async def _age_history(container: Container, *, days: int) -> None:
    """Отодвинуть историю проверок в прошлое, чтобы она попала под срок хранения."""
    async with container.database.session() as session:
        for request in await session.scalars(select(SearchRequest)):
            request.created_at = utcnow() - timedelta(days=days)


async def _live_links(container: Container) -> int:
    async with container.database.session() as session:
        return (
            await session.scalar(
                select(func.count()).select_from(ShareLink).where(ShareLink.revoked_at.is_(None))
            )
            or 0
        )


# ---------------------------------------------------------------- свой отзыв


async def test_revoked_link_stops_opening_in_the_browser(
    web_container: Container, bot: Bot, sent: SentMessages
) -> None:
    """Отзыв меряется страницей, а не числом в ответе сервиса."""
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    url = await _link_for(web_container, telegram_user_id=OPERATOR_ID)
    assert await _page_status(web_container, url) == 200

    answer = await _revoke_command(web_container, bot, sent, user_id=OPERATOR_ID)

    assert "Отозвано 1 ссылка" in answer
    assert await _page_status(web_container, url) == 404


async def test_revoking_a_report_first_checked_by_a_colleague(
    web_container: Container, bot: Bot, sent: SentMessages
) -> None:
    """Того же должника до этого проверял другой сотрудник.

    Кэш отчётов общий, нового запроса при попадании в него не появляется, и
    ссылка выдавалась одна на всех — чужая. Свой ``/revoke`` её не находил и
    отвечал «отзывать нечего», а пересланная не туда страница продолжала
    открываться.
    """
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    first = await _link_for(web_container, telegram_user_id=COLLEAGUE_ID)
    mine = await _link_for(web_container, telegram_user_id=OPERATOR_ID)

    # Один и тот же отчёт, но у каждого свой адрес: чужой погасить нельзя,
    # значит и получать его нельзя.
    assert mine != first
    assert await _page_status(web_container, mine) == 200

    answer = await _revoke_command(web_container, bot, sent, user_id=OPERATOR_ID)

    assert "отзывать нечего" not in answer
    assert await _page_status(web_container, mine) == 404


async def test_revoke_names_the_colleagues_link_that_stays_alive(
    web_container: Container, bot: Bot, sent: SentMessages
) -> None:
    """Свои адреса погашены, а отчёт всё ещё открывается — об этом надо сказать."""
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    theirs = await _link_for(web_container, telegram_user_id=COLLEAGUE_ID)
    await _link_for(web_container, telegram_user_id=OPERATOR_ID)

    answer = await _revoke_command(web_container, bot, sent, user_id=OPERATOR_ID)

    assert "других сотрудников" in answer
    # Обещание сдержано в обе стороны: чужую ссылку бот не гасит молча.
    assert await _page_status(web_container, theirs) == 200


async def test_revoking_twice_stops_claiming_a_second_revocation(
    web_container: Container, bot: Bot, sent: SentMessages
) -> None:
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    await _link_for(web_container, telegram_user_id=OPERATOR_ID)

    first = await _revoke_command(web_container, bot, sent, user_id=OPERATOR_ID)
    second = await _revoke_command(web_container, bot, sent, user_id=OPERATOR_ID)

    assert "Отозвано 1 ссылка" in first
    assert second == "Действующих ссылок нет — отзывать нечего."


async def test_expired_link_is_not_reported_as_revoked(
    web_container: Container, bot: Bot, sent: SentMessages
) -> None:
    """Истёкшая ссылка и так не открывается, и считать её отозванной — враньё.

    «Отозвано 1» про мёртвый адрес читается как «была живая, стала мёртвой», а
    оператор по этому ответу решает, звонить ли тому, кому переслал.
    """
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    request_id = await _check_debtor(web_container, telegram_user_id=OPERATOR_ID)
    target = ShareTarget(ShareKind.REPORT, request_id)
    url = await web_container.share_service.issue(target, telegram_user_id=OPERATOR_ID)
    assert url is not None
    await _expire(web_container, url)

    assert (
        await web_container.share_service.revoke(target, telegram_user_id=OPERATOR_ID)
    ).revoked == 0
    answer = await _revoke_command(web_container, bot, sent, user_id=OPERATOR_ID)

    assert answer == "Действующих ссылок нет — отзывать нечего."
    assert await _page_status(web_container, url) == 404


# ---------------------------------------------------------------- ретеншен


async def test_retention_removes_links_to_purged_reports(web_container: Container) -> None:
    """Отчёта нет — не должно остаться и ссылки на него.

    Иначе связка «оператор → какой отчёт он смотрел» переживает сам отчёт,
    ретеншен отчитывается удалившим больше, чем удалил, а ``/revoke`` считает
    такую ссылку действующей.
    """
    await web_container.import_service.import_file(web_container.settings.internal_csv_path)
    url = await _link_for(web_container, telegram_user_id=OPERATOR_ID)
    token = url.rsplit("/", maxsplit=1)[-1]
    settings = web_container.settings.model_copy(update={"history_retention_days": 1})
    await _age_history(web_container, days=2)

    await purge_once(settings, web_container.database)

    # Ссылка ещё в своём TTL — переживает она именно отчёт, а не срок годности.
    assert await web_container.share_service.resolve(token, ShareKind.REPORT) is None
    assert await _live_links(web_container) == 0
