"""Дорогое и опасное — только владельцу.

Бот уехал к заказчику открытым: ``ALLOWED_TELEGRAM_USER_IDS=*``, владельцы не
заданы. В таком виде посторонний нажимает «Проверить всю базу» и запускает
прогон по всей выгрузке заказчика — до тысячи платных обращений и персональные
данные всех должников одним файлом, — а через импорт подмешивает свои строки в
базу, по которой решают, на кого подавать в суд.

Отсюда две вещи, которые проверяются здесь и которые легко потерять по
отдельности.

**Закрыты все входы, а не команда со слешем.** У прогона их четыре: ``/batch``,
нижняя кнопка, инлайн-кнопка меню и подтверждение сметы из старого сообщения.
Проверка, забытая в одном из них, отменяет проверку в трёх остальных, поэтому
тест есть на каждый — включая колбэк из пересланного сообщения, у которого
сообщения-носителя нет вовсе и ответить можно только всплывающим окном.

**Показ не сломан.** Бот остаётся открытым, одиночная проверка работает как
работала, а владелец делает всё, что делал. Тест на это стоит рядом с тестами
на отказ намеренно: закрыть бота целиком было бы проще всего и означало бы
сорванную завтрашнюю передачу.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Document, InaccessibleMessage, Message, User

from app.bot.keyboards import BUTTON_BATCH, main_menu, main_reply_keyboard
from app.bot.states import CsvImport
from app.config import DEFAULT_OWNER_USER_IDS, NO_OWNERS, Settings
from app.container import Container
from app.db.repository import BatchRepository, DebtorRepository
from app.services.access import AccessService

from .bot_harness import (
    CHAT_ID,
    OPERATOR_ID,
    SentMessages,
    buttons,
    callbacks,
    dispatcher_for,
    feed,
    make_callback,
    make_message,
)

#: Допущенный сотрудник: в ``ALLOWED_TELEGRAM_USER_IDS`` он есть, владельцем не
#: является. Именно он, а не незнакомец, — главный герой этих тестов: посторонний
#: упирается в allowlist ещё в middleware, а сотрудник доходит до хендлера.
EMPLOYEE_ID = 222

#: Первый из владельцев, зашитых в код на случай незаполненной настройки.
SHIPPED_OWNER_ID = sorted(DEFAULT_OWNER_USER_IDS)[0]


def owner_only_refusal(sent: SentMessages) -> str:
    """Отказ целиком — тот, что человек читает в чате."""
    return next((text for text in sent.texts if "только для владельца" in text), "")


def open_bot(container: Container) -> Dispatcher:
    """Бот ровно в том виде, в каком он уехал на сервер.

    «*» в списке допущенных и незаполненная настройка владельцев: пускают всех,
    а владельцы берутся из кода. Ради этого сочетания всё и делалось.
    """
    settings = container.settings.model_copy(
        update={"allowed_telegram_user_ids": "*", "owner_telegram_user_ids": ""}
    )
    return dispatcher_for(
        replace(
            container,
            settings=settings,
            access_service=AccessService(settings, container.database),
        )
    )


def document_message(user_id: int) -> Message:
    return Message.model_construct(
        message_id=7,
        date=datetime(2026, 9, 6),
        chat=Chat(id=CHAT_ID, type="private"),
        from_user=User(id=user_id, is_bot=False, first_name="Employee"),
        document=Document(
            file_id="file-1", file_unique_id="u1", file_name="dolzhniki.csv", file_size=64
        ),
    )


def forwarded_callback(data: str, user_id: int) -> CallbackQuery:
    """Нажатие кнопки в пересланном сообщении.

    У такого колбэка нет доступного сообщения: Telegram отдаёт
    ``InaccessibleMessage``, ответить в чат нечем, и единственный канал — окно
    поверх экрана.
    """
    return CallbackQuery.model_construct(
        id=f"cb-fwd-{data}",
        from_user=User(id=user_id, is_bot=False, first_name="Employee"),
        chat_instance="chat-instance",
        data=data,
        message=InaccessibleMessage(chat=Chat(id=CHAT_ID, type="private"), message_id=2, date=0),
    )


# ---------------------------------------------------------------- прогон по базе


async def test_batch_command_is_refused_for_an_allowed_employee(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message("/batch", user_id=EMPLOYEE_ID))

    assert not sent.contains("Обращений к источникам")
    refusal = owner_only_refusal(sent)
    assert refusal, sent.texts
    # Отказ объясняет, а не отшивает: чьё это право, что доступно вместо и как
    # спросить. «Недостаточно прав» оставило бы человека без всех трёх ответов.
    assert "/search" in refusal
    assert "OWNER_TELEGRAM_USER_IDS" in refusal
    assert str(EMPLOYEE_ID) in refusal


async def test_bottom_keyboard_press_is_refused_like_the_command(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Кнопки у сотрудника нет, но нижняя клавиатура живёт на стороне Telegram.

    Она переживает и смену прав, и чужой скриншот: подпись можно прислать
    текстом, и это тот же вход в прогон.
    """
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message(BUTTON_BATCH, user_id=EMPLOYEE_ID))

    assert not sent.contains("Обращений к источникам")
    assert owner_only_refusal(sent)


async def test_inline_button_is_refused_with_a_popup_and_an_explanation(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, callback_query=make_callback("batch:start", user_id=EMPLOYEE_ID))

    assert not sent.contains("Обращений к источникам")
    # Окно поверх экрана — чтобы нажатие не выглядело зависшим, текст в чат —
    # потому что в двести символов окна объяснение не помещается.
    assert any("владелец" in answer for answer in sent.callback_answers), sent.callback_answers
    assert owner_only_refusal(sent)


async def test_forwarded_confirm_button_answers_instead_of_hanging(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Кнопка «Списать до …» из пересланного сообщения.

    Сообщения-носителя у нажавшего нет, и написать ему в чат нечем. Молчание
    здесь выглядело бы как зависший бот, поэтому окно уходит всегда и говорит
    достаточно само по себе.
    """
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(
        dispatcher, bot, callback_query=forwarded_callback("batch:run:5", user_id=EMPLOYEE_ID)
    )

    assert sent.callback_answers, "нажатие осталось без ответа"
    alert = sent.callback_answers[-1]
    assert "владелец" in alert
    assert str(EMPLOYEE_ID) in alert
    # Двести символов — предел Telegram на всплывающее окно.
    assert len(alert) <= 200
    assert sent.texts == []


async def test_a_confirmed_estimate_from_an_employee_starts_no_run(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Главное последствие: прогона нет, деньги не списаны."""
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, callback_query=make_callback("batch:run:5", user_id=EMPLOYEE_ID))

    async with container.database.session() as session:
        assert await BatchRepository(session).latest_run(EMPLOYEE_ID) is None
    assert not sent.contains("Проверка завершена")


async def test_queue_export_is_refused(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Выгрузка очереди — это все должники одним файлом, а не просмотр экрана."""
    await feed(dispatcher, bot, callback_query=make_callback("batch:export", user_id=EMPLOYEE_ID))

    assert sent.documents == []
    assert owner_only_refusal(sent)


async def test_the_debtor_list_is_refused(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(
        dispatcher, bot, callback_query=make_callback("batch:list:file", user_id=EMPLOYEE_ID)
    )

    assert not sent.contains("Подавать —")
    assert owner_only_refusal(sent)


# ---------------------------------------------------------------- импорт


async def test_import_command_is_refused(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/import", user_id=EMPLOYEE_ID))

    assert not sent.contains("Отправьте выгрузку должников")
    refusal = owner_only_refusal(sent)
    assert refusal
    # Причина у импорта своя: он правит базу, по которой идут в суд.
    assert "взыскании" in refusal


async def test_import_menu_button_is_refused(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:import", user_id=EMPLOYEE_ID))

    assert not sent.contains("Отправьте выгрузку должников")
    assert owner_only_refusal(sent)


async def test_a_document_from_an_employee_never_reaches_the_base(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Состояние «жду файл» могло остаться с тех пор, когда права были.

    Проверка стоит не на входе в диалог, а перед каждым его хендлером, и файл,
    присланный в уже открытый диалог, до базы не доходит.
    """
    await dispatcher.storage.set_state(
        StorageKey(bot_id=bot.id, chat_id=CHAT_ID, user_id=EMPLOYEE_ID),
        CsvImport.waiting_document,
    )

    await feed(dispatcher, bot, message=document_message(EMPLOYEE_ID))

    async with container.database.session() as session:
        assert await DebtorRepository(session).count() == 0
    assert not sent.contains("Импорт завершён")


# ---------------------------------------------------------------- показ не сломан


async def test_the_owner_still_runs_the_batch(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message("/batch", user_id=OPERATOR_ID))

    assert sent.contains("Массовая проверка")
    assert not owner_only_refusal(sent)


async def test_the_owner_still_imports(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/import", user_id=OPERATOR_ID))

    assert sent.contains("Отправьте выгрузку должников")


async def test_a_single_check_stays_open_to_an_employee(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Ради этого закрывали не бота, а два действия в нём."""
    await feed(dispatcher, bot, message=make_message("/search", user_id=EMPLOYEE_ID))
    # ``/search`` — явный вход «дай выбрать», в отличие от нижней кнопки, которая
    # ведёт сразу к телефону. Сотруднику он открыт: закрывали не бота, а два
    # дорогих действия в нём.
    assert sent.contains("Вы в главном меню")

    sent.texts.clear()
    await feed(
        dispatcher, bot, message=make_message("Тестов Андрей Сергеевич", user_id=EMPLOYEE_ID)
    )

    assert sent.contains("Фамилия: Тестов")
    assert not owner_only_refusal(sent)


async def test_status_says_who_may_spend_the_balance(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """«Доступ открыт всем» не должно читаться как «и база тоже».

    Владельцу — список ID, ему по нему править настройку; остальным только
    число: /status открыт всем допущенным, а «кто здесь главный» — ответ из
    /access.
    """
    await feed(dispatcher, bot, message=make_message("/status", user_id=OPERATOR_ID))
    assert sent.contains(f"только владельцам: {OPERATOR_ID} (вы среди них)")

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("/status", user_id=EMPLOYEE_ID))
    assert sent.contains("только владельцам: их 1")
    assert not sent.contains(str(OPERATOR_ID))


# ---------------------------------------------------------------- кнопки и меню


def test_the_menu_hides_what_an_employee_cannot_press() -> None:
    payloads = [
        button.callback_data for row in main_menu(owner=False).inline_keyboard for button in row
    ]

    assert "batch:start" not in payloads
    assert "menu:import" not in payloads
    # Остальное на месте: закрыты два действия, а не меню.
    assert "menu:person" in payloads
    assert "menu:more" in payloads


def test_the_menu_keeps_everything_for_the_owner() -> None:
    payloads = [
        button.callback_data for row in main_menu(owner=True).inline_keyboard for button in row
    ]

    assert "batch:start" in payloads
    assert "batch:start" in payloads


def test_the_bottom_keyboard_drops_the_batch_button_for_an_employee() -> None:
    labels = [button.text for row in main_reply_keyboard(owner=False).keyboard for button in row]

    assert BUTTON_BATCH not in labels
    assert "Проверить человека" in labels


async def test_start_shows_an_employee_no_batch_button(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Кнопка, которая всем отвечает «нельзя», — обещание, которого бот не держит."""
    await feed(dispatcher, bot, message=make_message("/start", user_id=EMPLOYEE_ID))

    assert BUTTON_BATCH not in buttons(sent)
    assert "batch:start" not in callbacks(sent)
    keyboard = next(markup for markup in sent.markups if getattr(markup, "keyboard", None))
    labels = [button.text for row in keyboard.keyboard for button in row]
    assert BUTTON_BATCH not in labels


async def test_start_keeps_the_batch_button_for_the_owner(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/start", user_id=OPERATOR_ID))
    await feed(dispatcher, bot, message=make_message("Главное меню", user_id=OPERATOR_ID))

    # Прогон по базе живёт в главном меню: нижняя клавиатура — это две кнопки
    # навигации, и место под пальцем нужнее тому, что жмут каждый день, чем
    # тому, что жмут раз в неделю.
    menu = next(markup for markup in sent.markups if getattr(markup, "inline_keyboard", None))
    labels = [button.text for row in menu.inline_keyboard for button in row]
    assert BUTTON_BATCH in labels


async def test_help_does_not_promise_the_batch_to_an_employee(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Список команд — то же меню, только текстом: обещать в нём нечего."""
    await feed(dispatcher, bot, message=make_message("/help", user_id=EMPLOYEE_ID))
    assert not sent.contains("/batch")
    assert not sent.contains("/import")

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("/help", user_id=OPERATOR_ID))
    assert sent.contains("/batch")


# ---------------------------------------------------------------- открытый бот


async def test_an_open_bot_still_closes_the_batch(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Тот самый случай: «*» пускает всех, но прогон запускает владелец.

    Открытость — осознанное решение и условие завтрашнего показа. Оно про то,
    кто может поговорить с ботом, а не про то, кто может потратить его баланс.
    """
    dispatcher = open_bot(container)
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message("/batch", user_id=4242))

    assert not sent.contains("Обращений к источникам")
    assert owner_only_refusal(sent)


async def test_an_open_bot_still_answers_a_stranger(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    dispatcher = open_bot(container)

    await feed(dispatcher, bot, message=make_message("/start", user_id=4242))

    assert sent.contains("Проверяю ваших должников")


async def test_the_shipped_owner_runs_the_batch_on_an_unconfigured_bot(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Иначе «закрыли» означало бы «сломали»: настройка на сервере пуста."""
    dispatcher = open_bot(container)
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message("/batch", user_id=SHIPPED_OWNER_ID))

    assert sent.contains("Массовая проверка")


# ---------------------------------------------------------------- сама настройка


def test_a_blank_setting_means_unfilled_not_ownerless() -> None:
    """Строка ``OWNER_TELEGRAM_USER_IDS=`` в ``.env`` — это забытая настройка.

    Прочитать её как «владельцев нет» значит открыть прогон и импорт всем, кто
    нашёл бота: именно в таком виде он и уехал на сервер.
    """
    settings = Settings(owner_telegram_user_ids="", _env_file=None)

    assert settings.owner_user_ids == DEFAULT_OWNER_USER_IDS


def test_an_explicit_list_wins_over_the_shipped_one() -> None:
    settings = Settings(owner_telegram_user_ids="4242", _env_file=None)

    assert settings.owner_user_ids == frozenset({4242})


def test_ownerless_is_expressible() -> None:
    """Без этого «владельцев нет» стало бы невыразимо, а такой деплой бывает."""
    settings = Settings(owner_telegram_user_ids=NO_OWNERS, _env_file=None)

    assert settings.owner_user_ids == frozenset()


@pytest.mark.parametrize("user_id", sorted(DEFAULT_OWNER_USER_IDS))
def test_every_shipped_owner_is_recognised(container: Container, user_id: int) -> None:
    settings = container.settings.model_copy(update={"owner_telegram_user_ids": ""})

    assert AccessService(settings, container.database).is_owner(user_id)
