"""Router assembly.

Order matters: the more specific routers are registered first so a state-scoped
handler wins over a generic one.
"""

from __future__ import annotations

from aiogram import Dispatcher, Router

from app.bot.handlers import (
    access,
    admin,
    batch,
    buttons,
    history,
    import_csv,
    search_contract,
    search_misc,
    search_person,
    search_vehicle,
    sources,
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
    # Нижняя клавиатура — самой первой, до всего остального. Её нажатия приходят
    # обычным текстом, и любой диалоговый роутер, оказавшийся выше, съел бы их:
    # «🕘 История» на шаге ввода ФИО разобралась бы как фамилия. Фильтры здесь —
    # пять точных совпадений по строке, поэтому первое место ничего не
    # перехватывает у остальных. Подробности — в docstring модуля.
    root.include_router(buttons.build_router())
    root.include_router(start.build_router())
    # Справочные экраны идут до диалогов намеренно. Хендлеры состояний забирают
    # себе весь текст, дошедший до их роутера, поэтому команда, подключённая
    # после них, посреди диалога молча съедается: /help во время ввода ФИО
    # отвечает «Нужно как минимум фамилия и имя». Спросить «откуда данные» и
    # «как это работает» человек вправе в любой момент.
    root.include_router(sources.build_router())
    root.include_router(help_handlers.build_router())
    # Заявки на доступ — тоже до диалоговых. Владелец получает карточку с
    # кнопками в тот момент, когда сам, возможно, стоит на шаге ввода ФИО, и
    # «Разрешить» обязано сработать, не дожидаясь, пока он доиграет свою
    # проверку. Ни один хендлер здесь состояние не трогает.
    root.include_router(access.build_router())
    root.include_router(batch.build_router())
    root.include_router(search_person.build_router())
    root.include_router(search_vehicle.build_router())
    root.include_router(search_contract.build_router())
    root.include_router(search_misc.build_router())
    root.include_router(import_csv.build_router())
    root.include_router(history.build_router())
    root.include_router(admin.build_router())
    return root


def setup_dispatcher(dispatcher: Dispatcher, container: Container) -> Dispatcher:
    """Attach middleware and routers.

    The allowlist middleware is registered on both the message and callback
    observers *before* the routers, so no handler can run for a user outside the
    allowlist.
    """
    allowlist = AllowlistMiddleware(
        container.settings.allowed_user_ids,
        open_access=container.settings.telegram_access_is_open,
        access=container.access_service,
    )
    dependencies = DependencyMiddleware(container=container)

    for observer in (dispatcher.message, dispatcher.callback_query):
        observer.outer_middleware(allowlist)
        observer.outer_middleware(dependencies)

    dispatcher.include_router(build_router())
    return dispatcher
