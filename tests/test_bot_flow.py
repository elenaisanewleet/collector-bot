"""End-to-end bot flows.

These drive real ``aiogram`` updates through the real dispatcher — middleware,
routers, FSM and handlers — with only the outbound Telegram API replaced. That
makes them the closest thing to running the bot without a token.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageText,
    SendDocument,
    SendMessage,
    TelegramMethod,
)
from aiogram.types import CallbackQuery, Chat, Message, Update, User

from app.bot.middleware import ACCESS_DENIED_MESSAGE
from app.bot.router import setup_dispatcher
from app.container import Container

FAKE_TOKEN = "123456789:AAEnoughCharactersToLookLikeARealToken00"
OPERATOR_ID = 111
OUTSIDER_ID = 999
CHAT_ID = 500


class SentMessages:
    """Captures everything the bot tried to send."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.markups: list[Any] = []
        self.callback_answers: list[str] = []
        self.documents: list[tuple[str, bytes]] = []

    @property
    def joined(self) -> str:
        return "\n".join(self.texts)

    def contains(self, needle: str) -> bool:
        return any(needle in text for text in self.texts)


@pytest.fixture
def sent() -> SentMessages:
    return SentMessages()


@pytest.fixture
def bot(sent: SentMessages, monkeypatch: pytest.MonkeyPatch) -> Iterator[Bot]:
    """A Bot whose outbound calls are intercepted instead of sent."""
    instance = Bot(token=FAKE_TOKEN, default=DefaultBotProperties(parse_mode=None))
    counter = {"id": 1000}

    async def fake_call(self: Bot, method: TelegramMethod[Any], *args: Any, **kwargs: Any) -> Any:
        if isinstance(method, SendMessage):
            sent.texts.append(method.text)
            sent.markups.append(method.reply_markup)
            counter["id"] += 1
            return Message.model_construct(
                message_id=counter["id"],
                date=datetime(2026, 9, 4),
                chat=Chat(id=CHAT_ID, type="private"),
                text=method.text,
            ).as_(self)
        if isinstance(method, EditMessageText):
            # Прогресс правится на месте — для теста это такой же текст.
            sent.texts.append(method.text or "")
            return True
        if isinstance(method, SendDocument):
            document = method.document
            sent.documents.append(
                (getattr(document, "filename", ""), getattr(document, "data", b""))
            )
            return True
        if isinstance(method, AnswerCallbackQuery):
            sent.callback_answers.append(method.text or "")
            return True
        if isinstance(method, DeleteMessage):
            return True
        return True

    monkeypatch.setattr(Bot, "__call__", fake_call, raising=True)
    yield instance


@pytest.fixture
def dispatcher(container: Container) -> Dispatcher:
    return setup_dispatcher(Dispatcher(storage=MemoryStorage()), container)


def make_message(text: str, user_id: int = OPERATOR_ID, message_id: int = 1) -> Message:
    return Message.model_construct(
        message_id=message_id,
        date=datetime(2026, 9, 4),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Operator"),
        text=text,
    )


def make_callback(data: str, user_id: int = OPERATOR_ID) -> CallbackQuery:
    return CallbackQuery.model_construct(
        id=f"cb-{data}",
        from_user=User(id=user_id, is_bot=False, first_name="Operator"),
        chat_instance="chat-instance",
        data=data,
        message=make_message("предыдущее сообщение", user_id=user_id, message_id=2),
    )


async def feed(dispatcher: Dispatcher, bot: Bot, **update: Any) -> None:
    await dispatcher.feed_update(bot, Update.model_construct(update_id=1, **update))


# ---------------------------------------------------------------- access


async def test_start_shows_the_main_menu(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, message=make_message("/start"))

    assert sent.contains(container.settings.app_name)
    assert sent.contains("Внутренний сервис проверки должников")
    assert sent.markups[0] is not None  # the inline menu


async def test_outsider_is_refused_and_reaches_no_handler(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/start", user_id=OUTSIDER_ID))

    assert sent.texts == [ACCESS_DENIED_MESSAGE]


async def test_outsider_cannot_start_a_search(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """The refused update must leave no trace: no search request, no history."""
    from app.db.repository import SearchRepository

    await feed(
        dispatcher, bot, message=make_message("Тестов Андрей Сергеевич", user_id=OUTSIDER_ID)
    )
    await feed(dispatcher, bot, callback_query=make_callback("menu:person", user_id=OUTSIDER_ID))

    async with container.database.session() as session:
        history = await SearchRepository(session).recent_for_user(OUTSIDER_ID)
    assert history == []
    assert set(sent.texts) == {ACCESS_DENIED_MESSAGE}


# ---------------------------------------------------------------- person flow


async def test_person_search_end_to_end(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """The full flow: menu -> ФИО -> дата -> телефон -> регион -> отчёт."""
    from app.db.repository import SearchRepository

    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    assert sent.contains("Введите ФИО")

    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    assert sent.contains("Дата рождения")

    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    assert sent.contains("Телефон")

    await feed(dispatcher, bot, callback_query=make_callback("skip"))
    assert sent.contains("Выберите регион")

    await feed(dispatcher, bot, callback_query=make_callback("region:moscow"))

    assert sent.contains("RECOVERY SCORE")
    assert sent.contains("Тестов Андрей Сергеевич")
    assert sent.contains("Уверенность данных")
    assert sent.contains("не заменяет юридическую проверку")

    async with container.database.session() as session:
        history = await SearchRepository(session).recent_for_user(OPERATOR_ID)
    assert len(history) == 1


async def test_malformed_name_is_rejected_without_guessing(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message("Иванов"))

    assert sent.contains("как минимум фамилия и имя")
    assert not sent.contains("RECOVERY SCORE")


async def test_bad_date_can_be_corrected(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, message=make_message("не дата"))
    assert sent.contains("Не удалось разобрать дату")

    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    assert sent.contains("Телефон")


async def test_cancel_resets_the_conversation(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message("/cancel"))
    assert sent.contains("Отменено")

    # A stray message after cancelling must not be read as a name.
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    assert not sent.contains("RECOVERY SCORE")


# ---------------------------------------------------------------- other flows


async def test_contract_search_shows_the_internal_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:contract"))
    await feed(dispatcher, bot, message=make_message("EV-20481"))

    assert sent.contains("НАШИ ДАННЫЕ")
    assert sent.contains("Тестов Андрей Сергеевич")
    assert sent.contains("38 400 ₽")
    # The external check is offered, not performed automatically.
    assert not sent.contains("RECOVERY SCORE")


async def test_contract_search_then_external_check(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:contract"))
    await feed(dispatcher, bot, message=make_message("EV-20481"))

    token = next(iter(container.subject_store._items))
    await feed(dispatcher, bot, callback_query=make_callback(f"external:{token}"))

    assert sent.contains("RECOVERY SCORE")


async def test_unknown_contract_reports_nothing_found(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:contract"))
    await feed(dispatcher, bot, message=make_message("НЕТ-ТАКОГО-ДОГОВОРА"))

    assert sent.contains("Во внутренней базе ничего не найдено")


async def test_invalid_plate_is_rejected(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:vehicle_plate"))
    await feed(dispatcher, bot, message=make_message("не номер"))

    assert sent.contains("Не похоже на российский госномер")


async def test_plate_search_reports_the_source_as_unconnected(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """No lawful plate-to-owner provider exists here, and the report says so."""
    await feed(dispatcher, bot, callback_query=make_callback("menu:vehicle_plate"))
    await feed(dispatcher, bot, message=make_message("А123ВС77"))

    assert sent.contains("Авто — не подключено")


async def test_invalid_vin_is_rejected(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:vin"))
    await feed(dispatcher, bot, message=make_message("SHORTVIN"))

    assert sent.contains("17 символов")


async def test_vin_search_runs(dispatcher: Dispatcher, bot: Bot, sent: SentMessages) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:vin"))
    await feed(dispatcher, bot, message=make_message("XW8ZZZ61ZKG011111"))

    assert sent.contains("RECOVERY SCORE")


async def test_passport_search_masks_the_number(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:passport"))
    await feed(dispatcher, bot, message=make_message("4509123456"))

    assert not sent.contains("4509123456")
    assert sent.contains("45** ******")


async def test_person_flow_asks_for_a_passport_only_when_the_bridge_is_on(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Флаг выключен — паспорт в этом флоу не спрашивают вовсе."""
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback("skip"))
    await feed(dispatcher, bot, callback_query=make_callback("region:moscow"))

    assert not sent.contains("Серия и номер паспорта")
    assert sent.contains("RECOVERY SCORE")


async def test_the_passport_step_masks_the_number_and_feeds_the_bridge(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Флаг включён — паспорт спрашивается последним и в чат не возвращается.

    Он же доезжает до моста: в демо тот детерминированно выдаёт ИНН профиля, и
    строка моста появляется в блоке ИСТОЧНИКИ.
    """
    from app.db.repository import SearchRepository

    enabled = _with_bridge(container)
    dispatcher = setup_dispatcher(Dispatcher(storage=MemoryStorage()), enabled)

    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback("skip"))
    await feed(dispatcher, bot, callback_query=make_callback("region:moscow"))

    assert sent.contains("Серия и номер паспорта")
    assert sent.contains("не сохраняются в базе")

    await feed(dispatcher, bot, message=make_message("4509123456"))

    assert not sent.contains("4509123456")
    assert sent.contains("45** ******")
    assert sent.contains("✓ ИНН по паспорту (ФНС) — ИНН получен")
    assert sent.contains("RECOVERY SCORE")

    async with enabled.database.session() as session:
        request = (await SearchRepository(session).recent_for_user(OPERATOR_ID))[0]
    # Паспорт не сохраняется: STORE_SENSITIVE_IDENTIFIERS по умолчанию выключен.
    assert "4509123456" not in request.subject_json


async def test_the_passport_step_can_be_skipped(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    enabled = _with_bridge(container)
    dispatcher = setup_dispatcher(Dispatcher(storage=MemoryStorage()), enabled)

    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback("skip"))
    await feed(dispatcher, bot, callback_query=make_callback("region:moscow"))
    await feed(dispatcher, bot, callback_query=make_callback("skip"))

    assert sent.contains("RECOVERY SCORE")


def _with_bridge(container: Container) -> Container:
    """Тот же контейнер, но с включённым INN_BRIDGE_ENABLED."""
    from dataclasses import replace

    return replace(
        container, settings=container.settings.model_copy(update={"inn_bridge_enabled": True})
    )


async def test_help_lists_connected_sources(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/help"))

    assert sent.contains("Команды:")
    assert sent.contains("не использует базы утечек")
    assert sent.contains("Источники:")


async def test_history_is_empty_then_populated(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/history"))
    assert sent.contains("История пуста")

    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, callback_query=make_callback("skip"))
    await feed(dispatcher, bot, callback_query=make_callback("skip"))
    await feed(dispatcher, bot, callback_query=make_callback("region:moscow"))

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("/history"))

    assert sent.contains("Последние проверки")
    assert sent.contains("Тестов А. С.")


async def test_history_repeat_reruns_the_search(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    from app.db.repository import SearchRepository

    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback("skip"))
    await feed(dispatcher, bot, callback_query=make_callback("region:moscow"))

    async with container.database.session() as session:
        request_id = (await SearchRepository(session).recent_for_user(OPERATOR_ID))[0].id

    sent.texts.clear()
    await feed(dispatcher, bot, callback_query=make_callback(f"repeat:{request_id}"))

    assert sent.contains("RECOVERY SCORE")
    # Re-running is always fresh, never served from the cache.
    assert not sent.contains("Использованы кэшированные данные")


async def test_repeat_of_another_operators_search_is_refused(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Callback payloads are user-supplied; ownership is checked server-side."""
    from app.db.repository import SearchRepository

    async with container.database.session() as session:
        request = await SearchRepository(session).create_request(
            telegram_user_id=222,
            search_type="person",
            normalized_query_hash="hash",
            masked_query="Чужой З.",
            subject_json='{"search_type": "person"}',
        )
        foreign_id = request.id

    await feed(dispatcher, bot, callback_query=make_callback(f"repeat:{foreign_id}"))

    assert sent.contains("Данные устарели")
    assert not sent.contains("RECOVERY SCORE")


# ---------------------------------------------------------------- массовая проверка


async def test_batch_shows_an_estimate_before_spending(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Прогон тратит платные запросы, поэтому сначала смета и подтверждение."""
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message("/batch"))

    assert sent.contains("Массовая проверка")
    assert sent.contains("Обращений к источникам")
    assert sent.contains("списываются с вашего баланса")
    # Ничего ещё не запущено.
    assert not sent.contains("Проверка завершена")


async def test_batch_on_an_empty_base_asks_for_an_import(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/batch"))
    assert sent.contains("Внутренняя база пуста")


async def test_batch_runs_and_reports_a_queue(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message("/batch"))
    await feed(dispatcher, bot, callback_query=make_callback("batch:run"))

    assert sent.contains("Проверка завершена")
    assert sent.contains("Судебный приказ")
    assert sent.contains("Не подавать")
    assert sent.contains("Не будет потрачено на пошлины")


async def test_batch_lists_one_verdict(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await container.import_service.import_file(container.settings.internal_csv_path)
    await feed(dispatcher, bot, message=make_message("/batch"))
    await feed(dispatcher, bot, callback_query=make_callback("batch:run"))

    sent.texts.clear()
    await feed(dispatcher, bot, callback_query=make_callback("batch:list:drop"))

    assert sent.contains("Не подавать")
    assert sent.contains("Демов Максим Игоревич")


async def test_batch_list_without_a_run(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("batch:list:file"))
    assert sent.contains("Прогонов ещё не было")


async def test_batch_export_sends_a_file(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await container.import_service.import_file(container.settings.internal_csv_path)
    await feed(dispatcher, bot, message=make_message("/batch"))
    await feed(dispatcher, bot, callback_query=make_callback("batch:run"))
    await feed(dispatcher, bot, callback_query=make_callback("batch:export"))

    assert sent.documents, "CSV не отправлен"
    name, payload = sent.documents[-1]
    assert name.endswith(".csv")
    assert payload.startswith(b"\xef\xbb\xbf")
    assert "Тестов Андрей Сергеевич" in payload.decode("utf-8-sig")


async def test_outsider_cannot_start_a_batch(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message("/batch", user_id=OUTSIDER_ID))

    assert sent.texts == [ACCESS_DENIED_MESSAGE]


async def test_import_requires_a_document(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/import"))
    assert sent.contains("Отправьте CSV-файл")

    await feed(dispatcher, bot, message=make_message("не файл"))
    assert sent.contains("Нужно отправить файл документом")


async def test_status_command_reports_configuration(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/status"))

    assert sent.contains("состояние")
    assert sent.contains("Источники:")
    # The token is confirmed as present without being printed.
    assert not sent.contains(FAKE_TOKEN)
    assert sent.contains("<set:")


async def test_cached_search_is_announced(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    async def run_person_search() -> None:
        await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
        await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
        await feed(dispatcher, bot, message=make_message("12.03.1985"))
        await feed(dispatcher, bot, callback_query=make_callback("skip"))
        await feed(dispatcher, bot, callback_query=make_callback("region:moscow"))

    await run_person_search()
    sent.texts.clear()
    await run_person_search()

    assert sent.contains("Использованы кэшированные данные")
