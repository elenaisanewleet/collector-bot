"""HTTP-сервер отчётов.

Маленькое приложение на aiohttp — той же библиотеке, на которой уже работает
aiogram, поэтому новых зависимостей оно не приносит и живёт в том же event
loop, что и опрос Telegram.

Маршрутов ровно три: отчёт по должнику, очередь взыскания и проверка живости.
Всё остальное — 404 одной и той же страницей, без подсказок о том, существовал
ли токен: страница отдаёт персональные данные, и разница между «неверный
токен» и «истёкший» здесь никому не нужна.
"""

from __future__ import annotations

from aiohttp import web

from app.container import Container
from app.domain.verdict import VERDICT_TITLES
from app.logging_setup import get_logger
from app.services.share import ShareKind
from app.web.render import render_message_page, render_report_page
from app.web.render_queue import render_queue_page

logger = get_logger(__name__)

CONTAINER_KEY = web.AppKey[Container]("container")

NOT_FOUND_TITLE = "Ссылка недоступна"
NOT_FOUND_TEXT = (
    "Ссылка не найдена или срок её действия истёк. Запросите отчёт в боте заново — он выдаст новую."
)
# Страница не кэшируется и не индексируется: за ней персональные данные.
PRIVATE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, private",
    "X-Robots-Tag": "noindex, nofollow, noarchive",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def build_app(container: Container) -> web.Application:
    app = web.Application()
    app[CONTAINER_KEY] = container
    app.add_routes(
        [
            web.get("/healthz", handle_health),
            web.get("/r/{token}", handle_report),
            web.get("/q/{token}", handle_queue),
        ]
    )
    return app


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_report(request: web.Request) -> web.Response:
    container = request.app[CONTAINER_KEY]
    token = request.match_info["token"]

    link = await container.share_service.resolve(token, ShareKind.REPORT)
    if link is None:
        return _not_found(container)

    report = await container.search_service.load_report(link.target_id)
    if report is None:
        # Ссылка жива, а отчёта за ней нет — например, историю почистили.
        return _not_found(container)

    decision = container.verdict_engine.decide(report)
    html = render_report_page(report, decision, app_name=container.settings.app_name)
    logger.info(
        "web.report_opened",
        verdict=VERDICT_TITLES[decision.verdict],
        user_id=link.telegram_user_id,
    )
    return _html(html)


async def handle_queue(request: web.Request) -> web.Response:
    container = request.app[CONTAINER_KEY]
    token = request.match_info["token"]

    link = await container.share_service.resolve(token, ShareKind.QUEUE)
    if link is None:
        return _not_found(container)

    snapshot = await container.batch_service.queue_snapshot(link.target_id)
    if snapshot is None:
        return _not_found(container)

    return _html(render_queue_page(snapshot, app_name=container.settings.app_name))


def _html(body: str, *, status: int = 200) -> web.Response:
    return web.Response(text=body, status=status, content_type="text/html", headers=PRIVATE_HEADERS)


def _not_found(container: Container) -> web.Response:
    return _html(
        render_message_page(NOT_FOUND_TITLE, NOT_FOUND_TEXT, app_name=container.settings.app_name),
        status=404,
    )


async def run_web_server(container: Container) -> web.AppRunner:
    """Поднять сервер рядом с ботом и вернуть runner для остановки."""
    settings = container.settings
    runner = web.AppRunner(build_app(container), access_log=None)
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
