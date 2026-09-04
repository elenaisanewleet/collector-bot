"""Router assembly.

Order matters: the more specific routers are registered first so a state-scoped
handler wins over a generic one.
"""

from __future__ import annotations

from aiogram import Dispatcher, Router

from app.bot.handlers import (
    admin,
    batch,
    history,
    import_csv,
    search_contract,
    search_misc,
    search_person,
    search_vehicle,
    start,
)
from app.bot.handlers import (
    help as help_handlers,
)
from app.bot.middleware import AllowlistMiddleware, DependencyMiddleware
from app.container import Container


def build_router() -> Router:
    """Assemble a fresh router tree.

    Every module hands back a new Router, so calling this twice yields two
    independent trees rather than failing on an already-attached child.
    """
    root = Router(name="root")
    root.include_router(start.build_router())
    root.include_router(batch.build_router())
    root.include_router(search_person.build_router())
    root.include_router(search_vehicle.build_router())
    root.include_router(search_contract.build_router())
    root.include_router(search_misc.build_router())
    root.include_router(import_csv.build_router())
    root.include_router(history.build_router())
    root.include_router(help_handlers.build_router())
    root.include_router(admin.build_router())
    return root


def setup_dispatcher(dispatcher: Dispatcher, container: Container) -> Dispatcher:
    """Attach middleware and routers.

    The allowlist middleware is registered on both the message and callback
    observers *before* the routers, so no handler can run for a user outside the
    allowlist.
    """
    allowlist = AllowlistMiddleware(container.settings.allowed_user_ids)
    dependencies = DependencyMiddleware(container=container)

    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.outer_middleware(allowlist)
        observer.outer_middleware(dependencies)

    dispatcher.include_router(build_router())
    return dispatcher
