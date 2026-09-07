"""Доступ по одобрению.

Тесты гоняют настоящие апдейты через настоящий диспетчер, потому что проверять
надо не сервис, а границу: незнакомец не должен дойти ни до одного хендлера и
ни до одного платного запроса, а владелец — увидеть, кто просится, и решить
кнопкой. Сервис в отрыве от middleware этого не показывает.

Отдельная забота — перезапуск. Решение, которое живёт в памяти процесса, любое
падение отменяет: отобранный доступ возвращается, суточная пауза обнуляется.
Здесь перезапуск изображается новым контейнером и новым диспетчером поверх той
же базы — ровно то, что происходит с ботом на сервере.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import Chat, Message, User
from sqlalchemy import select

from app.bot.access_view import (
    APPROVED_NOTICE,
    OWNER_HEADER,
    REQUEST_PENDING,
    REQUEST_REJECTED,
    REQUEST_SENT,
    REVOKED_NOTICE,
)
from app.bot.handlers.access import MODERATION_OFF, NOT_OWNER
from app.bot.middleware import ACCESS_DENIED_MESSAGE
from app.config import NO_OWNERS
from app.container import Container
from app.db.models import AccessRequest
from app.db.repository import AuditRepository, SearchRepository
from app.db.session import Database
from app.services.access import AccessService, AccessStatus, RequestOutcome
from app.utils.dates import utcnow

from .bot_harness import (
    CHAT_ID,
    OPERATOR_ID,
    SentMessages,
    dispatcher_for,
    feed,
    make_callback,
    make_message,
)

OWNER_ID = 777
STRANGER_ID = 999
STRANGER_NAME = "Пётр Сидоров"
STRANGER_USERNAME = "petrov"


# ---------------------------------------------------------------- фикстуры


def moderated_container(container: Container, *, owners: str = str(OWNER_ID)) -> Container:
    """Тот же контейнер, но с владельцем.

    ``replace``, а не пересборка по полям: список полей, выписанный руками,
    молча теряет всякую новую службу.
    """
    settings = container.settings.model_copy(update={"owner_telegram_user_ids": owners})
    return replace(
        container,
        settings=settings,
        access_service=AccessService(settings, container.database),
    )


@pytest.fixture
def moderated(container: Container) -> Container:
    return moderated_container(container)


@pytest.fixture
def moderated_dispatcher(moderated: Container) -> Dispatcher:
    return dispatcher_for(moderated)


@pytest.fixture
def open_container(container: Container) -> Container:
    """«*» и владелец одновременно: открытый бот сильнее одобрения."""
    settings = container.settings.model_copy(
        update={"allowed_telegram_user_ids": "*", "owner_telegram_user_ids": str(OWNER_ID)}
    )
    return replace(
        container,
        settings=settings,
        access_service=AccessService(settings, container.database),
    )


def stranger_message(text: str, *, user_id: int = STRANGER_ID, message_id: int = 1) -> Message:
    """Сообщение от незнакомца — с именем и ником, по которым решает владелец."""
    return Message.model_construct(
        message_id=message_id,
        date=datetime(2026, 9, 5),
        chat=Chat(id=user_id, type="private"),
        from_user=User(
            id=user_id,
            is_bot=False,
            first_name="Пётр",
            last_name="Сидоров",
            username=STRANGER_USERNAME,
        ),
        text=text,
    )


async def stored_row(database: Database, user_id: int = STRANGER_ID) -> AccessRequest | None:
    async with database.session() as session:
        row: AccessRequest | None = await session.scalar(
            select(AccessRequest).where(AccessRequest.telegram_user_id == user_id)
        )
        if row is not None:
            session.expunge(row)
    return row


async def age_the_request(database: Database, *, hours: int, user_id: int = STRANGER_ID) -> None:
    """Отмотать заявку и решение по ней назад — вместо ожидания суток."""
    async with database.session() as session:
        row = await session.scalar(
            select(AccessRequest).where(AccessRequest.telegram_user_id == user_id)
        )
        assert row is not None
        shifted = utcnow() - timedelta(hours=hours)
        row.requested_at = shifted
        if row.decided_at is not None:
            row.decided_at = shifted


# ---------------------------------------------------------------- заявка


async def test_stranger_gets_a_request_screen_instead_of_a_wall(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))

    assert sent.contains(REQUEST_SENT)
    assert not sent.contains(ACCESS_DENIED_MESSAGE)


async def test_owner_sees_who_is_asking(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Решение принимается по имени, нику и ID — значит все три обязаны быть."""
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))

    to_owner = sent.to_chat(OWNER_ID)
    assert len(to_owner) == 1
    card = to_owner[0]
    assert OWNER_HEADER in card
    assert STRANGER_NAME in card
    assert f"@{STRANGER_USERNAME}" in card
    assert str(STRANGER_ID) in card


async def test_owner_card_carries_both_buttons(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))

    index = sent.chats.index(OWNER_ID)
    buttons = [
        button.callback_data for row in sent.markups[index].inline_keyboard for button in row
    ]
    assert buttons == [f"access:allow:{STRANGER_ID}", f"access:deny:{STRANGER_ID}"]


async def test_request_reaches_no_handler_and_spends_nothing(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, moderated: Container
) -> None:
    """Заявка — это не «пустили посмотреть»: поиска не происходит."""
    await feed(moderated_dispatcher, bot, message=stranger_message("Тестов Андрей Сергеевич"))

    async with moderated.database.session() as session:
        history = await SearchRepository(session).recent_for_user(STRANGER_ID)
    assert history == []
    assert not sent.contains("RECOVERY SCORE")


async def test_request_is_written_to_the_database(
    moderated_dispatcher: Dispatcher, bot: Bot, database: Database
) -> None:
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))

    row = await stored_row(database)
    assert row is not None
    assert row.status == AccessStatus.PENDING
    assert row.username == STRANGER_USERNAME
    assert row.full_name == STRANGER_NAME


async def test_repeat_message_does_not_wake_the_owner_twice(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Заявка одна на человека. Иначе нетерпеливый проситель заваливает владельца."""
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))
    await feed(moderated_dispatcher, bot, message=stranger_message("ну что там"))

    assert len(sent.to_chat(OWNER_ID)) == 1
    assert REQUEST_PENDING in sent.to_chat(STRANGER_ID)


async def test_callback_from_a_stranger_is_refused_not_turned_into_a_request(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, database: Database
) -> None:
    """Кнопка у постороннего берётся только из пересланного чужого сообщения."""
    await feed(
        moderated_dispatcher, bot, callback_query=make_callback("menu:person", user_id=STRANGER_ID)
    )

    assert sent.callback_answers == [ACCESS_DENIED_MESSAGE]
    assert await stored_row(database) is None


# ---------------------------------------------------------------- одобрение


async def approve_stranger(dispatcher: Dispatcher, bot: Bot) -> None:
    await feed(dispatcher, bot, message=stranger_message("/start"))
    await feed(
        dispatcher,
        bot,
        callback_query=make_callback(f"access:allow:{STRANGER_ID}", user_id=OWNER_ID),
    )


async def test_approval_opens_the_bot(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, moderated: Container
) -> None:
    await approve_stranger(moderated_dispatcher, bot)
    sent.texts.clear()
    sent.chats.clear()
    sent.markups.clear()

    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))

    # Название приложения из приветствия убрано: имя бота Telegram печатает
    # в шапке чата сам. Признак «пустили» — сам факт приветствия.
    assert sent.contains("стоит ли подавать и платить пошлину")
    assert not sent.contains(REQUEST_SENT)


async def test_approved_user_is_told(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Молчаливое одобрение никто не заметит: человек ждёт ответа, а не гадает."""
    await approve_stranger(moderated_dispatcher, bot)

    assert APPROVED_NOTICE in sent.to_chat(STRANGER_ID)


async def test_approval_is_recorded_in_the_audit(
    moderated_dispatcher: Dispatcher, bot: Bot, database: Database
) -> None:
    await approve_stranger(moderated_dispatcher, bot)

    async with database.session() as session:
        events = await AuditRepository(session).recent(limit=20)
    actions = [event.action for event in events]
    assert "access.requested" in actions
    assert "access.approved" in actions


async def test_a_plain_operator_cannot_approve(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, database: Database
) -> None:
    """Допущенный — не владелец: пускать он никого не может."""
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))
    await feed(
        moderated_dispatcher,
        bot,
        callback_query=make_callback(f"access:allow:{STRANGER_ID}", user_id=OPERATOR_ID),
    )

    row = await stored_row(database)
    assert row is not None
    assert row.status == AccessStatus.PENDING
    assert sent.callback_answers[-1] != ""


# ---------------------------------------------------------------- отказ


async def reject_stranger(dispatcher: Dispatcher, bot: Bot) -> None:
    await feed(dispatcher, bot, message=stranger_message("/start"))
    await feed(
        dispatcher,
        bot,
        callback_query=make_callback(f"access:deny:{STRANGER_ID}", user_id=OWNER_ID),
    )


async def test_rejection_keeps_the_door_shut(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, moderated: Container
) -> None:
    await reject_stranger(moderated_dispatcher, bot)
    assert REQUEST_REJECTED in sent.to_chat(STRANGER_ID)

    sent.texts.clear()
    sent.chats.clear()
    sent.markups.clear()
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))

    assert not sent.contains(moderated.settings.app_name)


async def test_rejected_cannot_ask_again_the_same_day(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Иначе «нет» ничего не значит и владелец получает заявку каждые пять минут."""
    await reject_stranger(moderated_dispatcher, bot)
    owner_cards = len(sent.to_chat(OWNER_ID))

    await feed(moderated_dispatcher, bot, message=stranger_message("пустите"))

    assert len(sent.to_chat(OWNER_ID)) == owner_cards
    assert any("не чаще раза в сутки" in text for text in sent.to_chat(STRANGER_ID))


async def test_the_wait_is_named_in_hours(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, database: Database
) -> None:
    """«Попробуйте позже» человек читает как «пробуйте прямо сейчас»."""
    await reject_stranger(moderated_dispatcher, bot)
    await age_the_request(database, hours=20)
    sent.texts.clear()
    sent.chats.clear()
    sent.markups.clear()

    await feed(moderated_dispatcher, bot, message=stranger_message("пустите"))

    assert any("через 4 часа" in text for text in sent.to_chat(STRANGER_ID))


async def test_rejected_may_ask_again_after_a_day(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, database: Database
) -> None:
    await reject_stranger(moderated_dispatcher, bot)
    await age_the_request(database, hours=25)
    owner_cards = len(sent.to_chat(OWNER_ID))

    await feed(moderated_dispatcher, bot, message=stranger_message("прошли сутки"))

    assert len(sent.to_chat(OWNER_ID)) == owner_cards + 1
    row = await stored_row(database)
    assert row is not None
    assert row.status == AccessStatus.PENDING
    # Новая заявка обнуляет прежнее решение: иначе список у владельца показывал
    # бы «отклонён» человеку, который сейчас ждёт ответа.
    assert row.decided_at is None


async def test_late_rejection_still_buys_a_full_day(
    moderated: Container, database: Database
) -> None:
    """Пауза считается от более позднего из двух: подачи и решения.

    Владелец, разобравший заявку через три дня, иначе получил бы новую тем же
    вечером — пауза, которую он думал поставить нажатием, уже истекла бы.
    """
    access = moderated.access_service
    await access.submit_request(user_id=STRANGER_ID, username=None, full_name="Пётр")
    await age_the_request(database, hours=72)
    await access.reject(STRANGER_ID, by=OWNER_ID)

    result = await access.submit_request(user_id=STRANGER_ID, username=None, full_name="Пётр")

    assert result.outcome is RequestOutcome.THROTTLED
    assert result.retry_after_hours == 24


# ---------------------------------------------------------------- отзыв


async def test_revocation_takes_access_away(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, moderated: Container
) -> None:
    await approve_stranger(moderated_dispatcher, bot)
    await feed(
        moderated_dispatcher,
        bot,
        callback_query=make_callback(f"access:revoke:{STRANGER_ID}", user_id=OWNER_ID),
    )
    assert REVOKED_NOTICE in sent.to_chat(STRANGER_ID)

    sent.texts.clear()
    sent.chats.clear()
    sent.markups.clear()
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))

    assert not sent.contains(moderated.settings.app_name)


async def test_revoked_user_cannot_search(
    moderated_dispatcher: Dispatcher, bot: Bot, moderated: Container
) -> None:
    await approve_stranger(moderated_dispatcher, bot)
    await feed(
        moderated_dispatcher,
        bot,
        callback_query=make_callback(f"access:revoke:{STRANGER_ID}", user_id=OWNER_ID),
    )

    await feed(moderated_dispatcher, bot, message=stranger_message("Тестов Андрей Сергеевич"))

    async with moderated.database.session() as session:
        history = await SearchRepository(session).recent_for_user(STRANGER_ID)
    assert history == []


# ---------------------------------------------------------------- перезапуск


async def test_approval_survives_a_restart(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, moderated: Container
) -> None:
    """Перезапуск не должен отбирать выданный доступ."""
    await approve_stranger(moderated_dispatcher, bot)

    restarted = moderated_container(moderated, owners=str(OWNER_ID))
    fresh = dispatcher_for(restarted)
    sent.texts.clear()
    sent.chats.clear()
    sent.markups.clear()

    await feed(fresh, bot, message=stranger_message("/start"))

    # Название приложения из приветствия убрано: имя бота Telegram печатает
    # в шапке чата сам. Признак «пустили» — сам факт приветствия.
    assert sent.contains("стоит ли подавать и платить пошлину")


async def test_rejection_survives_a_restart(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, moderated: Container
) -> None:
    """И не должен обнулять суточную паузу: иначе её обходит любое падение."""
    await reject_stranger(moderated_dispatcher, bot)

    restarted = moderated_container(moderated, owners=str(OWNER_ID))
    fresh = dispatcher_for(restarted)
    sent.texts.clear()
    sent.chats.clear()
    sent.markups.clear()

    await feed(fresh, bot, message=stranger_message("пустите"))

    assert sent.to_chat(OWNER_ID) == []
    assert any("не чаще раза в сутки" in text for text in sent.to_chat(STRANGER_ID))


# ---------------------------------------------------------------- режим «*»


async def test_open_access_still_lets_everyone_in(
    open_container: Container, bot: Bot, sent: SentMessages
) -> None:
    """«*» продолжает работать как работал: одобрение его не отменяет."""
    dispatcher = dispatcher_for(open_container)

    await feed(dispatcher, bot, message=stranger_message("/start"))

    # Название приложения из приветствия убрано: имя бота Telegram печатает
    # в шапке чата сам. Признак «пустили» — сам факт приветствия.
    assert sent.contains("стоит ли подавать и платить пошлину")
    assert not sent.contains(REQUEST_SENT)


async def test_open_access_creates_no_requests(
    open_container: Container, bot: Bot, database: Database
) -> None:
    """Пущены и так все — заявке неоткуда взяться и нечего одобрять."""
    dispatcher = dispatcher_for(open_container)

    await feed(dispatcher, bot, message=stranger_message("/start"))

    assert await stored_row(database) is None


# ------------------------------------------------- без владельца ничего не меняется


async def test_without_owners_a_stranger_is_refused_as_before(
    unowned_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, database: Database
) -> None:
    await feed(unowned_dispatcher, bot, message=stranger_message("/start"))

    assert sent.texts == [ACCESS_DENIED_MESSAGE]
    assert await stored_row(database) is None


async def test_dropping_the_owner_closes_the_bot_for_the_approved(
    moderated_dispatcher: Dispatcher, bot: Bot, moderated: Container
) -> None:
    """Одобрять было некому — значит и одобренных быть не может.

    Строка от прежней конфигурации не должна пускать в бота, у которого больше
    нет владельца: иначе выключение режима тихо оставляет чужие ключи.
    """
    await approve_stranger(moderated_dispatcher, bot)
    unowned = moderated_container(moderated, owners=NO_OWNERS)

    assert not await unowned.access_service.is_allowed(STRANGER_ID)


async def test_owner_works_even_without_being_in_the_allowlist(
    moderated: Container,
) -> None:
    """Иначе одобрять заявки было бы некому — их некому было бы и увидеть."""
    assert OWNER_ID not in moderated.settings.allowed_user_ids
    assert await moderated.access_service.is_allowed(OWNER_ID)


# ---------------------------------------------------------------- список доступа


async def test_access_list_is_owner_only(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """«Кто ещё пользуется ботом» — не то, что обязан знать каждый допущенный."""
    await feed(moderated_dispatcher, bot, message=make_message("/access"))

    assert sent.texts == [NOT_OWNER]


async def test_access_list_shows_pending_and_approved(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))
    sent.texts.clear()
    sent.chats.clear()
    sent.markups.clear()

    await feed(moderated_dispatcher, bot, message=make_message("/access", user_id=OWNER_ID))

    listing = "\n".join(sent.to_chat(CHAT_ID))
    assert "Ждут решения (1)" in listing
    assert STRANGER_NAME in listing
    assert str(STRANGER_ID) in listing


async def test_access_list_offers_revocation_for_the_approved(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await approve_stranger(moderated_dispatcher, bot)
    sent.markups.clear()
    sent.texts.clear()
    sent.chats.clear()

    await feed(moderated_dispatcher, bot, message=make_message("/access", user_id=OWNER_ID))

    buttons = [
        button.callback_data
        for markup in sent.markups
        if markup is not None
        for row in markup.inline_keyboard
        for button in row
    ]
    assert f"access:revoke:{STRANGER_ID}" in buttons


async def test_access_list_names_who_cannot_be_revoked(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Доступ из .env кнопкой не отбирается — лучше сказать это, чем дать искать."""
    await feed(moderated_dispatcher, bot, message=make_message("/access", user_id=OWNER_ID))

    listing = "\n".join(sent.texts)
    assert "кнопкой не отзывается" in listing
    assert str(OPERATOR_ID) in listing


async def test_access_list_explains_when_moderation_is_off(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Владельцев нет, поэтому и команда никому не принадлежит — но объяснить надо."""
    settings = container.settings.model_copy(
        update={"owner_telegram_user_ids": str(OPERATOR_ID), "allowed_telegram_user_ids": "*"}
    )
    open_bot = replace(
        container, settings=settings, access_service=AccessService(settings, container.database)
    )
    open_dispatcher = dispatcher_for(open_bot)

    await feed(open_dispatcher, bot, message=make_message("/access"))

    assert sent.contains("бот сейчас отвечает всем")
    assert not sent.contains(MODERATION_OFF)


# ---------------------------------------------------------------- меню команд


async def test_owner_gets_the_access_command_in_the_blue_menu(
    bot: Bot, moderated: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/access` показывается только владельцу — у Telegram для этого есть scope."""
    from typing import Any

    from aiogram.methods import SetMyCommands
    from aiogram.types import BotCommandScopeChat

    from app.main import publish_commands

    calls: list[Any] = []

    async def capture(self: Bot, method: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", capture, raising=True)
    await publish_commands(bot, moderated.settings)

    published = [call for call in calls if isinstance(call, SetMyCommands)]
    scoped = [call for call in published if isinstance(call.scope, BotCommandScopeChat)]
    assert [
        call.scope.chat_id for call in scoped if isinstance(call.scope, BotCommandScopeChat)
    ] == [OWNER_ID]
    assert "access" in [command.command for command in scoped[0].commands]
    # Общий список остаётся общим: /access в нём нет.
    unscoped = next(
        call for call in calls if isinstance(call, SetMyCommands) and call.scope is None
    )
    assert "access" not in [command.command for command in unscoped.commands]


# ---------------------------------------------------------------- порядок роутеров


async def test_owner_can_decide_in_the_middle_of_a_search(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Заявка приходит, когда владелец занят своим делом, — и это норма.

    Роутер заявок подключён до диалоговых. Окажись он ниже, «Разрешить»
    досталось бы шагу ввода ФИО, и кнопка молчала бы ровно в тот момент, когда
    её нажимают.
    """
    await feed(moderated_dispatcher, bot, callback_query=make_callback("menu:person", OWNER_ID))
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))

    await feed(
        moderated_dispatcher,
        bot,
        callback_query=make_callback(f"access:allow:{STRANGER_ID}", user_id=OWNER_ID),
    )

    assert APPROVED_NOTICE in sent.to_chat(STRANGER_ID)


async def test_owner_returns_to_the_same_step_after_deciding(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Решение по заявке не рвёт наполовину введённую проверку владельца."""
    await feed(moderated_dispatcher, bot, callback_query=make_callback("menu:person", OWNER_ID))
    await feed(moderated_dispatcher, bot, message=stranger_message("/start"))
    await feed(
        moderated_dispatcher,
        bot,
        callback_query=make_callback(f"access:deny:{STRANGER_ID}", user_id=OWNER_ID),
    )
    sent.texts.clear()
    sent.chats.clear()
    sent.markups.clear()

    await feed(
        moderated_dispatcher,
        bot,
        message=make_message("Тестов Андрей Сергеевич", user_id=OWNER_ID),
    )

    assert sent.contains("Дата рождения")


async def test_any_first_message_becomes_a_request_not_just_start(
    moderated_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Незнакомец не обязан знать про /start, чтобы попроситься."""
    await feed(moderated_dispatcher, bot, message=stranger_message("здравствуйте"))

    assert REQUEST_SENT in sent.to_chat(STRANGER_ID)
    assert len(sent.to_chat(OWNER_ID)) == 1
