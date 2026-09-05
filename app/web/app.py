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

from typing import Any

from aiohttp import web

from app.container import Container
from app.db.models import ShareLink
from app.domain.verdict import VERDICT_TITLES
from app.logging_setup import get_logger
from app.services.export import queue_to_csv
from app.services.reporting import render_report
from app.services.share import ShareKind
from app.utils.dates import utcnow
from app.web.render import ExportLinks, render_message_page, render_report_page
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
        ]
    )
    return app


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


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
    """Очередь таблицей. Тот же ``queue_to_csv``, что и кнопка в боте."""
    context = await _queue_context(request)
    if context is None:
        return _not_found(request.app[CONTAINER_KEY])
    _container, link, snapshot = context
    body = queue_to_csv(snapshot.items)
    logger.info("web.queue_downloaded", kind="csv", user_id=link.telegram_user_id)
    name = f"ochered-{link.target_id}-{utcnow():%Y%m%d}.csv"
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
    return web.Response(text=body, status=status, content_type="text/html", headers=PRIVATE_HEADERS)


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
