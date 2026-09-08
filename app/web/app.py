"""HTTP-сервер отчётов.

Маленькое приложение на aiohttp — той же библиотеке, на которой уже работает
aiogram, поэтому новых зависимостей оно не приносит и живёт в том же event
loop, что и опрос Telegram.

Выгрузка сделана без генератора PDF на сервере, и это осознанный выбор.
Печатает браузер по печатному стилю: на бумагу уходит ровно то, что человек
видел на экране, PDF не расходится со страницей при каждой правке вёрстки, в
образ не приезжает ни WeasyPrint с cairo и pango, ни headless-браузер, и файл с
персональными данными нигде не оседает на диске. Текстовая выгрузка отдаёт тот
же ``render_report``, что уходит в чат: второго текста у отчёта нет и заводить
его нельзя.

Все выгрузки живут за тем же токеном, что и страница: отдельного открытого
адреса у файла нет, и отзыв ссылки закрывает их разом.

Всё, кроме известных маршрутов, — 404 одной и той же страницей, без подсказок о
том, существовал ли токен: страница отдаёт персональные данные, и разница между
«неверный токен» и «истёкший» здесь никому не нужна.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from aiohttp import web

from app.config import AppMode
from app.container import Container
from app.db.models import Debtor, ShareLink
from app.db.repository import DebtorRepository, ShareLinkRepository
from app.domain.verdict import VERDICT_TITLES
from app.logging_setup import get_logger
from app.services.export import queue_to_csv
from app.services.reporting import render_report
from app.services.share import ShareKind
from app.utils.dates import utcnow
from app.web.render import ExportLinks, render_message_page, render_report_page
from app.web.render_base import FeeRules, render_base_page, render_person_page
from app.web.render_lookups import render_lookups_page
from app.web.render_queue import render_queue_page

logger = get_logger(__name__)

CONTAINER_KEY = web.AppKey[Container]("container")

NOT_FOUND_TITLE = "Ссылка недоступна"
NOT_FOUND_TEXT = (
    "Ссылка не найдена, отозвана или срок её действия истёк. "
    "Запросите отчёт в боте заново — он выдаст новую."
)
# Страница не кэшируется и не индексируется: за ней персональные данные.
PRIVATE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # Страница автономна: ни шрифтов, ни картинок, ни запросов наружу. CSP это
    # фиксирует, чтобы экранирование перестало быть единственным слоем защиты.
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
        "img-src data:; frame-ancestors 'none'; form-action 'none'; base-uri 'none'"
    ),
}


def build_app(container: Container) -> web.Application:
    app = web.Application()
    app[CONTAINER_KEY] = container
    app.add_routes(
        [
            web.get("/healthz", handle_health),
            web.get("/r/{token}", handle_report),
            web.get("/r/{token}/print", handle_report_print),
            web.get("/r/{token}/report.txt", handle_report_text),
            web.get("/q/{token}", handle_queue),
            web.get("/q/{token}/print", handle_queue_print),
            web.get("/q/{token}/queue.csv", handle_queue_csv),
            web.get("/b/{token}", handle_base),
            web.get("/p/{token}", handle_person),
            web.get("/n/{token}", handle_lookups),
        ]
    )
    return app


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# ---------------------------------------------------------------- база


def _fee_rules(container: Container) -> FeeRules:
    """Пороги подачи из настроек.

    Страницы не решают, где проходят границы «приказ / иск / не окупается», —
    их знает вердикт, и вторая копия разъехалась бы с первой на первой правке
    тарифа. Обе страницы берут их отсюда, а не каждая у себя: две копии внутри
    одного модуля расходятся так же охотно, как две копии в разных.
    """
    return FeeRules(
        court_order_max=Decimal(str(container.settings.court_order_max_amount)),
        min_debt_to_fee_ratio=Decimal(str(container.settings.min_debt_to_fee_ratio)),
    )


async def handle_base(request: web.Request) -> web.Response:
    """Справочник должников целиком: кто заведён и что про него известно.

    Отвечает на вопрос, которого очередь не закрывает: она показывает только
    проверенных, то есть уже оплаченных. А «кто у меня вообще есть» спрашивают
    каждый день, и сегодня за ответом лезут в 1С.
    """
    container = request.app[CONTAINER_KEY]
    token = request.match_info["token"]
    link = await container.share_service.resolve(token, ShareKind.BASE)
    if link is None:
        return _not_found(container)
    async with container.database.session() as session:
        debtors = await DebtorRepository(session).all_by_name(
            limit=container.settings.batch_max_debtors
        )
    share = container.share_service
    html = render_base_page(
        debtors,
        app_name=container.settings.app_name,
        rules=_fee_rules(container),
        person_urls={row.id: f"/p/{share.person_token(link, row.id)}" for row in debtors},
        demo_mode=container.settings.app_mode is AppMode.DEMO,
        print_mode="print" in request.query,
    )
    return web.Response(text=html, content_type="text/html", headers=PRIVATE_HEADERS)


async def handle_lookups(request: web.Request) -> web.Response:
    """Проверки по номеру телефона: кого пробили и кого из них в базе нет.

    Отдельная страница, а не вкладка справочника: справочник — это выгрузка
    заказчика, а здесь люди, которых в ней ещё нет. Смешать их значило бы
    выдать непроверенную личность из чужого источника за строку базы.
    """
    container = request.app[CONTAINER_KEY]
    token = request.match_info["token"]
    link = await container.share_service.resolve(token, ShareKind.LOOKUPS)
    if link is None:
        return _not_found(container)
    lookups = await container.phone_lookups.recent(limit=container.settings.batch_max_debtors)
    html = render_lookups_page(
        lookups,
        app_name=container.settings.app_name,
        demo_mode=container.settings.app_mode is AppMode.DEMO,
        print_mode="print" in request.query,
    )
    return web.Response(text=html, content_type="text/html", headers=PRIVATE_HEADERS)


async def handle_person(request: web.Request) -> web.Response:
    """Сводка по одному должнику из списка.

    Токен свой, производный от ссылки на список: переслать одного человека
    можно, а получить из этой ссылки остальные две тысячи — нельзя. И гаснет
    он вместе с общей ссылкой: её идентификатор подписан и проверяется живым.
    """
    container = request.app[CONTAINER_KEY]
    parsed = container.share_service.read_person_token(request.match_info["token"])
    if parsed is None:
        return _not_found(container)
    link_id, debtor_id = parsed
    async with container.database.session() as session:
        link = await ShareLinkRepository(session).get_active(link_id)
        if link is None or link.kind != ShareKind.BASE.value:
            return _not_found(container)
        debtor = await session.get(Debtor, debtor_id)
    if debtor is None:
        return _not_found(container)
    html = render_person_page(
        debtor,
        app_name=container.settings.app_name,
        rules=_fee_rules(container),
        back_url=container.share_service.url_for(link.token, ShareKind.BASE),
        demo_mode=container.settings.app_mode is AppMode.DEMO,
        print_mode="print" in request.query,
    )
    return web.Response(text=html, content_type="text/html", headers=PRIVATE_HEADERS)


# ---------------------------------------------------------------- отчёт


async def _report_context(request: web.Request) -> tuple[Container, ShareLink, Any] | None:
    """Токен → живая ссылка → отчёт. ``None``, если хоть что-то не сошлось."""
    container = request.app[CONTAINER_KEY]
    token = request.match_info["token"]
    link = await container.share_service.resolve(token, ShareKind.REPORT)
    if link is None:
        return None
    report = await container.search_service.load_report(link.target_id)
    if report is None:
        # Ссылка жива, а отчёта за ней нет — например, историю почистили.
        return None
    return container, link, report


async def handle_report(request: web.Request) -> web.Response:
    return await _render_report(request, print_mode=False)


async def handle_report_print(request: web.Request) -> web.Response:
    return await _render_report(request, print_mode=True)


async def _render_report(request: web.Request, *, print_mode: bool) -> web.Response:
    context = await _report_context(request)
    if context is None:
        return _not_found(request.app[CONTAINER_KEY])
    container, link, report = context

    decision = container.verdict_engine.decide(report)
    html = render_report_page(
        report,
        decision,
        app_name=container.settings.app_name,
        demo_mode=container.settings.is_demo,
        exports=_report_exports(request),
        print_mode=print_mode,
    )
    logger.info(
        "web.report_opened",
        verdict=VERDICT_TITLES[decision.verdict],
        user_id=link.telegram_user_id,
        print_mode=print_mode,
    )
    return _html(html)


async def handle_report_text(request: web.Request) -> web.Response:
    """Тот же текст, что уходит в чат, — файлом."""
    context = await _report_context(request)
    if context is None:
        return _not_found(request.app[CONTAINER_KEY])
    container, link, report = context

    body = render_report(report, demo_mode=container.settings.is_demo)
    logger.info("web.report_downloaded", kind="txt", user_id=link.telegram_user_id)
    # В имени файла — номер запроса и дата, но не ФИО: файл живёт в папке
    # «Загрузки» дольше, чем сама ссылка, и попадает в чужие бэкапы.
    name = f"otchet-{link.target_id}-{utcnow():%Y%m%d}.txt"
    return _attachment(body.encode("utf-8"), name=name, content_type="text/plain")


# ---------------------------------------------------------------- очередь


async def _queue_context(request: web.Request) -> tuple[Container, ShareLink, Any] | None:
    container = request.app[CONTAINER_KEY]
    token = request.match_info["token"]
    link = await container.share_service.resolve(token, ShareKind.QUEUE)
    if link is None:
        return None
    snapshot = await container.batch_service.queue_snapshot(link.target_id)
    if snapshot is None:
        return None
    return container, link, snapshot


async def handle_queue(request: web.Request) -> web.Response:
    return await _render_queue(request, print_mode=False)


async def handle_queue_print(request: web.Request) -> web.Response:
    return await _render_queue(request, print_mode=True)


async def _render_queue(request: web.Request, *, print_mode: bool) -> web.Response:
    context = await _queue_context(request)
    if context is None:
        return _not_found(request.app[CONTAINER_KEY])
    container, _link, snapshot = context
    return _html(
        render_queue_page(
            snapshot,
            app_name=container.settings.app_name,
            demo_mode=container.settings.is_demo,
            exports=_queue_exports(request),
            print_mode=print_mode,
        )
    )


async def handle_queue_csv(request: web.Request) -> web.Response:
    """Очередь таблицей — в том же виде, в каком её показывает страница.

    ФИО маскировано, даты рождения и госномера нет. Страница прячет полное ФИО
    намеренно: одна пересланная ссылка на восемьсот строк — это выгрузка базы
    должников. Кнопка «Таблицей» рядом с ней отдавала ровно то, что страница
    прятала, и сверх того дату рождения и госномер, обесценивая маску.

    Полный файл остаётся у владельца: он приходит кнопкой в боте, где
    получатель — не тот, у кого оказалась ссылка, а конкретный человек в
    Telegram. Имя файла об этом говорит, чтобы разницу было видно до открытия.
    """
    context = await _queue_context(request)
    if context is None:
        return _not_found(request.app[CONTAINER_KEY])
    _container, link, snapshot = context
    body = queue_to_csv(snapshot.items, mask_personal=True)
    logger.info("web.queue_downloaded", kind="csv", user_id=link.telegram_user_id)
    name = f"ochered-{link.target_id}-{utcnow():%Y%m%d}-bez-fio.csv"
    return _attachment(body, name=name, content_type="text/csv")


# ---------------------------------------------------------------- ответы


def _report_exports(request: web.Request) -> ExportLinks:
    """Адреса выгрузки — относительные, от текущего токена.

    Собираются из пути запроса, а не из ``web_public_url``: у файла не должно
    быть адреса, отличного от того, по которому открыта страница.
    """
    token = request.match_info["token"]
    return ExportLinks(text_url=f"/r/{token}/report.txt", print_url=f"/r/{token}/print")


def _queue_exports(request: web.Request) -> ExportLinks:
    token = request.match_info["token"]
    return ExportLinks(text_url=f"/q/{token}/queue.csv", print_url=f"/q/{token}/print")


def _html(body: str, *, status: int = 200) -> web.Response:
    response = web.Response(
        text=body, status=status, content_type="text/html", headers=PRIVATE_HEADERS
    )
    # Очередь на восемьсот строк — это под мегабайт разметки, и открывают её с
    # телефона. Сжатие уносит её примерно в тринадцать раз; страница остаётся
    # автономной, потому что жмётся то же тело, а не подгружается новое.
    response.enable_compression()
    return response


def _attachment(payload: bytes, *, name: str, content_type: str) -> web.Response:
    headers = dict(PRIVATE_HEADERS)
    headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return web.Response(body=payload, content_type=content_type, charset="utf-8", headers=headers)


def _not_found(container: Container) -> web.Response:
    return _html(
        render_message_page(NOT_FOUND_TITLE, NOT_FOUND_TEXT, app_name=container.settings.app_name),
        status=404,
    )


async def run_web_server(container: Container) -> web.AppRunner:
    """Поднять сервер рядом с ботом и вернуть runner для остановки."""
    settings = container.settings
    # Токен ездит в пути URL, поэтому access_log выключен; заголовок сервера
    # заодно перестаёт сообщать версию aiohttp.
    runner = web.AppRunner(build_app(container), access_log=None, server_header=False)
    await runner.setup()
    site = web.TCPSite(runner, settings.web_host, settings.web_port)
    await site.start()
    logger.info(
        "web.started",
        host=settings.web_host,
        port=settings.web_port,
        public_url=settings.web_public_url or "<не задан: ссылки не отправляются>",
    )
    return runner
