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
from app.config import Settings
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
            sent.markups.append(method.reply_markup)
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
#
# Жалоба, из-за которой этот флоу переписан, звучала дословно: «много требует»,
# «это указать, это указать, раз уж можно пропустить — зачем это спрашивать».
# Поэтому тесты здесь считают не только результат, но и НАЖАТИЯ: сценарий,
# который снова начнёт спрашивать телефон или регион, обязан упасть.

FULL_LINE = "Тестов Андрей Сергеевич 12.03.1985"


def buttons(sent: SentMessages) -> list[str]:
    """Тексты всех кнопок, которые бот показал за прогон."""
    return [
        button.text
        for markup in sent.markups
        if markup is not None and getattr(markup, "inline_keyboard", None)
        for row in markup.inline_keyboard
        for button in row
    ]


def callbacks(sent: SentMessages) -> list[str]:
    return [
        button.callback_data
        for markup in sent.markups
        if markup is not None and getattr(markup, "inline_keyboard", None)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


async def test_free_line_runs_without_a_single_button(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Типичный случай: одна строка, ноль нажатий, отчёт.

    Ни ``/start``, ни меню — оператор просто пишет то, что у него есть. Раньше
    здесь было семь взаимодействий: меню, ФИО, дата, «пропустить» телефон,
    регион.
    """
    from app.db.repository import SearchRepository

    await feed(dispatcher, bot, message=make_message(FULL_LINE))

    assert sent.contains("RECOVERY SCORE")
    assert sent.contains("Тестов Андрей Сергеевич")
    assert sent.contains("Уверенность данных")
    assert sent.contains("не заменяет юридическую проверку")

    async with container.database.session() as session:
        history = await SearchRepository(session).recent_for_user(OPERATOR_ID)
    assert len(history) == 1


async def test_the_card_echoes_what_was_understood(
    linked_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Бот угадывает по форме — и обязан показать, что именно угадал."""
    await feed(linked_dispatcher, bot, message=make_message(FULL_LINE))

    assert sent.contains("Принял:")
    assert sent.contains("дата рождения 12.03.1985")


async def test_menu_person_asks_for_one_line_and_offers_no_skipping(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))

    assert sent.contains("одной строкой")
    assert "Пропустить" not in buttons(sent)

    await feed(dispatcher, bot, message=make_message(FULL_LINE))
    assert sent.contains("RECOVERY SCORE")


async def test_region_is_never_asked_and_defaults_to_all(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Пустой ``regions`` уже означает «все регионы» — шага для этого не нужно."""
    await feed(dispatcher, bot, message=make_message(FULL_LINE))

    assert not sent.contains("Выберите регион")
    subject = next(iter(container.subject_store._items.values()))[0]
    assert subject.regions == ()


async def test_phone_is_never_asked_but_is_accepted_from_the_line(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """По телефону во внешних реестрах не ищут — спрашивать его не за чем.

    Но если он в строке есть, он подтверждает личность во внутренней базе, и
    выбрасывать его тоже незачем.
    """
    await feed(dispatcher, bot, message=make_message(f"{FULL_LINE} +79161234567"))

    assert not sent.contains("Телефон должника")
    subject = next(iter(container.subject_store._items.values()))[0]
    assert subject.phone == "+79161234567"


async def test_a_malformed_name_is_explained_not_guessed(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("Иванов"))

    assert sent.contains("как минимум фамилия и имя")
    assert not sent.contains("RECOVERY SCORE")


async def test_garbage_gets_one_question_not_five(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("asdf"))

    assert len(sent.texts) == 1
    assert not sent.contains("RECOVERY SCORE")

    # И следующая нормальная строка сразу запускает проверку.
    await feed(dispatcher, bot, message=make_message(FULL_LINE))
    assert sent.contains("RECOVERY SCORE")


async def test_a_name_alone_still_runs_and_says_what_is_missing(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Полнота не блокирует. Бот идёт с тем, что дали, и называет цену.

    Строка про ИНН показывается, пока идёт проверка, — она ничего не стоит и
    учит. Что именно осталось неопрошенным, дословно проверяется на карточке
    (``tests/test_bot_view.py``) и на самих провайдерах: демо-источники ищут по
    одному ФИО и про обязательную дату у ФССП не знают.
    """
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))

    assert sent.contains("RECOVERY SCORE")
    assert sent.contains("Без ИНН не спрошу банкротство, ИП и арбитраж")
    assert any(text.startswith("📅 Добавить дату рождения") for text in buttons(sent))


async def test_a_broken_date_blocks_and_can_be_corrected(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Единственный блок, кроме «искать нечего», — и он про ложь, а не про полноту.

    Оператор дату дал. Молча выбросить её значило бы отдать отчёт с двумя
    пустыми разделами по вине опечатки, которую он считает исправленной.
    """
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 15.13.1980"))

    assert sent.contains("на дату не похоже")
    assert not sent.contains("RECOVERY SCORE")

    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    assert sent.contains("RECOVERY SCORE")
    assert sent.contains("дата рождения 12.03.1985")


async def test_a_broken_date_can_be_skipped_forward(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 15.13.1980"))
    assert "pskip:birth_date" in callbacks(sent)

    await feed(dispatcher, bot, callback_query=make_callback("pskip:birth_date"))

    assert sent.contains("RECOVERY SCORE")
    subject = next(iter(container.subject_store._items.values()))[0]
    assert subject.birth_date is None


async def test_ambiguous_ten_digits_ask_instead_of_guessing(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Угаданный «телефон» закрыл бы единственный вход в мост «паспорт → ИНН»."""
    await feed(
        dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985 9204384710")
    )

    assert sent.contains("паспорт или телефон")
    assert not sent.contains("RECOVERY SCORE")

    await feed(dispatcher, bot, callback_query=make_callback("pten:passport"))

    assert sent.contains("RECOVERY SCORE")
    subject = next(iter(container.subject_store._items.values()))[0]
    assert subject.passport == "9204384710"


async def test_an_entity_inn_is_reported_but_does_not_stop_the_run(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message(f"{FULL_LINE} ИНН 7709123456"))

    assert sent.contains("ИНН организации")
    assert sent.contains("RECOVERY SCORE")


async def test_cancel_resets_the_conversation(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message("/cancel"))
    assert sent.contains("Отменено")
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


# ------------------------------------------------- паспорт: только после отчёта


async def test_passport_is_not_asked_before_the_report(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Мост включён, а вопроса нет: отчёт приходит первым.

    Паспорт нужен ровно для того, чтобы добыть ИНН, и ровно тем, кому ИНН не
    хватило. Узнать это можно только после прогона, поэтому вопрос переехал под
    карточку и стал кнопкой.
    """
    enabled = _with_bridge(container)
    dispatcher = setup_dispatcher(Dispatcher(storage=MemoryStorage()), enabled)

    await feed(dispatcher, bot, message=make_message(FULL_LINE))

    assert not sent.contains("Серия и номер паспорта")
    assert sent.contains("RECOVERY SCORE")
    assert any(text.startswith("🪪 Узнать ИНН по паспорту") for text in buttons(sent))


@pytest.mark.parametrize(
    "line",
    [
        # ИНН уже есть — мост не нужен вовсе.
        f"{FULL_LINE} 770912345601",
        # Нет даты рождения — мост ответит insufficient_query, не сделав вызова.
        "Тестов Андрей Сергеевич",
    ],
)
async def test_the_passport_button_is_hidden_when_it_would_lie(
    bot: Bot, sent: SentMessages, container: Container, line: str
) -> None:
    """Кнопка показывается, только если она не соврёт.

    ``will_query`` на субъекте с подставленным паспортом — единственная честная
    проверка: провайдер и кнопка не могут разойтись, потому что это один и тот
    же код. Случай «мост выключен» проверяется на живом провайдере в
    ``tests/test_report_actions.py``: демо-мост не стоит денег и включён всегда.
    """
    enabled = _with_bridge(container)
    dispatcher = setup_dispatcher(Dispatcher(storage=MemoryStorage()), enabled)

    await feed(dispatcher, bot, message=make_message(line))

    assert sent.contains("RECOVERY SCORE")
    assert not any(text.startswith("🪪 Узнать ИНН по паспорту") for text in buttons(sent))


async def test_the_passport_button_masks_the_number_and_feeds_the_bridge(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Нажали кнопку — паспорт спрашивается, удаляется из чата и едет в мост.

    В демо мост детерминированно выдаёт ИНН профиля, поэтому его строка
    появляется в блоке ИСТОЧНИКИ.
    """
    from app.db.repository import SearchRepository

    enabled = _with_bridge(container)
    dispatcher = setup_dispatcher(Dispatcher(storage=MemoryStorage()), enabled)

    await feed(dispatcher, bot, message=make_message(FULL_LINE))
    token = _last_add_token(sent, "passport")
    await feed(dispatcher, bot, callback_query=make_callback(f"padd:passport:{token}"))

    assert sent.contains("Серия и номер паспорта")
    assert sent.contains("не сохраняются в базе")
    assert sent.contains("Ваше сообщение с номером я удалю")

    await feed(dispatcher, bot, message=make_message("4509123456"))

    assert not sent.contains("4509123456")
    assert sent.contains("45** ******")
    assert sent.contains("✓ ИНН по паспорту (ФНС) — ИНН получен")

    async with enabled.database.session() as session:
        request = (await SearchRepository(session).recent_for_user(OPERATOR_ID))[0]
    # Паспорт не сохраняется: STORE_SENSITIVE_IDENTIFIERS по умолчанию выключен.
    assert "4509123456" not in request.subject_json


async def test_an_inn_from_the_line_skips_the_bridge_entirely(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    enabled = _with_bridge(container)
    dispatcher = setup_dispatcher(Dispatcher(storage=MemoryStorage()), enabled)

    await feed(dispatcher, bot, message=make_message(f"{FULL_LINE} 770912345601"))

    subject = next(iter(enabled.subject_store._items.values()))[0]
    assert subject.inn == "770912345601"
    bridge = enabled.registry.inn_bridge
    assert bridge is not None
    assert not bridge.is_needed(subject)
    assert not sent.contains("ИНН по паспорту")


async def test_adding_an_inn_reruns_with_a_different_query_hash(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Добор — это второй платный прогон, и кэш его не подменит."""
    from app.services.search import build_query_hash

    await feed(dispatcher, bot, message=make_message(FULL_LINE))
    before = next(iter(container.subject_store._items.values()))[0]
    token = _last_add_token(sent, "inn")

    await feed(dispatcher, bot, callback_query=make_callback(f"padd:inn:{token}"))
    assert sent.contains("ИНН физлица")

    await feed(dispatcher, bot, message=make_message("770912345601"))
    after = next(reversed(container.subject_store._items.values()))[0]

    assert after.inn == "770912345601"
    assert build_query_hash(before) != build_query_hash(after)


async def test_a_ten_digit_inn_is_refused_as_a_company(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message(FULL_LINE))
    token = _last_add_token(sent, "inn")
    await feed(dispatcher, bot, callback_query=make_callback(f"padd:inn:{token}"))

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("7709123456"))

    assert sent.contains("ИНН организации")
    assert not sent.contains("RECOVERY SCORE")


async def test_the_region_is_an_offer_under_the_card_not_a_step(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, message=make_message(FULL_LINE))
    assert not sent.contains("Выберите регион")

    token = _last_add_token(sent, "region")
    await feed(dispatcher, bot, callback_query=make_callback(f"padd:region:{token}"))
    assert sent.contains("Сейчас ищу по всем регионам")

    await feed(dispatcher, bot, callback_query=make_callback("region:moscow"))

    narrowed = next(reversed(container.subject_store._items.values()))[0]
    assert narrowed.regions == ("moscow",)


def _last_add_token(sent: SentMessages, field: str) -> str:
    """Токен субъекта из кнопки «добавить <поле>» под последней карточкой."""
    matches = [data for data in callbacks(sent) if data.startswith(f"padd:{field}:")]
    assert matches, f"кнопки padd:{field} нет среди {callbacks(sent)}"
    return matches[-1].split(":", maxsplit=2)[2]


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

    await feed(dispatcher, bot, message=make_message(FULL_LINE))

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("/history"))

    assert sent.contains("Последние проверки")
    assert sent.contains("Тестов А. С.")


async def test_history_repeat_reruns_the_search(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    from app.db.repository import SearchRepository

    await feed(dispatcher, bot, message=make_message(FULL_LINE))

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
        await feed(dispatcher, bot, message=make_message(FULL_LINE))

    await run_person_search()
    sent.texts.clear()
    await run_person_search()

    assert sent.contains("Использованы кэшированные данные")


# ---------------------------------------------------------------- веб-отчёты


@pytest.fixture
def linked(container: Container) -> Container:
    """Контейнер с включёнными ссылками на веб-отчёт."""
    from app.services.share import ShareLinkService

    settings = container.settings.model_copy(
        update={"web_public_url": "https://reports.example.test"}
    )
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


@pytest.fixture
def linked_dispatcher(linked: Container) -> Dispatcher:
    return setup_dispatcher(Dispatcher(storage=MemoryStorage()), linked)


async def test_search_sends_a_card_with_a_link_not_a_wall(
    linked_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, linked: Container
) -> None:
    """С включённым вебом в чат уходит карточка и кнопка, а не отчёт текстом."""
    await feed(linked_dispatcher, bot, message=make_message(FULL_LINE))

    assert sent.contains("Тестов Андрей Сергеевич")
    assert sent.contains("Recovery Score")
    # Полного текстового отчёта нет — он теперь на странице.
    assert not sent.contains("ИСТОЧНИКИ")
    urls = [
        button.url
        for markup in sent.markups
        if markup is not None
        for row in markup.inline_keyboard
        for button in row
        if button.url
    ]
    assert any(url.startswith("https://reports.example.test/r/") for url in urls)


async def test_search_falls_back_to_text_without_a_public_url(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Без публичного адреса лучше простыня, чем нерабочая кнопка."""
    await feed(dispatcher, bot, message=make_message(FULL_LINE))

    assert sent.contains("ИСТОЧНИКИ")


async def test_batch_offers_the_queue_page(
    linked_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, linked: Container
) -> None:
    await linked.import_service.import_file(linked.settings.internal_csv_path)

    await feed(linked_dispatcher, bot, message=make_message("/batch"))
    await feed(linked_dispatcher, bot, callback_query=make_callback("batch:run"))

    urls = [
        button.url
        for markup in sent.markups
        if markup is not None
        for row in markup.inline_keyboard
        for button in row
        if button.url
    ]
    assert any(url.startswith("https://reports.example.test/q/") for url in urls)


async def test_progress_message_is_edited_not_reposted(
    linked_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Одно сообщение, которое меняется, вместо очереди новых.

    На сотне должников это и есть разница между читаемым чатом и лентой.
    """
    await feed(linked_dispatcher, bot, message=make_message(FULL_LINE))

    # Первое — прогресс, дальше правка того же сообщения результатом.
    assert sent.texts[0].startswith("Проверяю")
    assert sent.contains("Recovery Score")


def test_star_opens_the_bot_to_everyone(live_settings: Settings) -> None:
    """``*`` — единственный способ открыть бота, и он должен быть явным."""
    from app.bot.middleware import AllowlistMiddleware

    closed = live_settings.model_copy(update={"allowed_telegram_user_ids": "1,2"})
    assert not closed.telegram_access_is_open
    assert not AllowlistMiddleware(closed.allowed_user_ids).is_allowed(999)

    opened = live_settings.model_copy(update={"allowed_telegram_user_ids": "*"})
    assert opened.telegram_access_is_open
    guard = AllowlistMiddleware(opened.allowed_user_ids, open_access=True)
    assert guard.is_allowed(999)
    # Отсутствие пользователя не значит «открыто»: анонимный апдейт всё равно нет.
    assert not guard.is_allowed(None)


# ------------------------------------------------- чужие сценарии не перехвачены


async def test_other_flows_are_not_hijacked_by_the_catch_all(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Ловец свободной строки стоит последним и только вне состояния.

    Без ``StateFilter(None)`` он съедал бы ввод госномера, VIN, адреса и
    договора: ``search_person`` включается в корень раньше всех них.
    """
    await feed(dispatcher, bot, callback_query=make_callback("menu:contract"))
    await feed(dispatcher, bot, message=make_message("EV-20481"))
    assert sent.contains("НАШИ ДАННЫЕ")

    sent.texts.clear()
    await feed(dispatcher, bot, callback_query=make_callback("menu:vehicle_plate"))
    await feed(dispatcher, bot, message=make_message("не номер"))
    assert sent.contains("Не похоже на российский госномер")

    sent.texts.clear()
    await feed(dispatcher, bot, callback_query=make_callback("menu:vin"))
    await feed(dispatcher, bot, message=make_message("SHORTVIN"))
    assert sent.contains("17 символов")

    sent.texts.clear()
    await feed(dispatcher, bot, callback_query=make_callback("menu:address"))
    await feed(dispatcher, bot, message=make_message("Москва, ул. Примерная, д. 1"))
    assert sent.contains("ФИО, если известно")

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("/cancel"))
    await feed(dispatcher, bot, message=make_message("/import"))
    await feed(dispatcher, bot, message=make_message("не файл"))
    assert sent.contains("Нужно отправить файл документом")


async def test_a_bare_plate_in_a_free_line_is_a_vehicle_not_a_person(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Голый госномер — это не человек без ФИО."""
    await feed(dispatcher, bot, message=make_message("А123ВС77"))

    subject = next(iter(container.subject_store._items.values()))[0]
    assert subject.search_type == "vehicle_plate"
    assert subject.vehicle is not None
    assert subject.vehicle.plate == "А123ВС77"
    assert sent.contains("Авто — не подключено")
