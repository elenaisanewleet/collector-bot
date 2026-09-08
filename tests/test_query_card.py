"""Накопительная карточка запроса: память между сообщениями.

Первый тест здесь — дословная жалоба владелицы, и он же главный: «написала
клочкова елена николаевна а потом 24 11 1994 и мне пишут а это к чему вообще».
Остальные сторожат то, чем за эту память заплачено: приватность (паспорт и
телефон в базу не едут), деньги (платит одна кнопка, и дважды за одно и то же
она не платит) и честность (карточка не создаёт впечатления полноты).

Гоняется через настоящий диспетчер: стенд и фикстуры общие с остальными
флоу-тестами (:mod:`tests.bot_harness`, ``conftest``).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from app.bot.handlers import query_card as handlers
from app.bot.router import setup_dispatcher
from app.config import FedresursBackend, FNSBackend, Settings
from app.container import Container
from app.db.base import Base
from app.db.models import QueryCard
from app.db.repository import QueryCardRepository, SearchRepository
from app.db.session import Database
from app.domain.identity import PersonName, SearchSubject
from app.providers.registry import (
    ProviderRegistry,
    build_external_providers,
    build_inn_bridge,
    build_internal_provider,
)
from app.services.query_card import QueryCardService
from app.services.retention import CARD_RETENTION_DAYS, purge_once
from app.utils.dates import utcnow

from .bot_harness import CHAT_ID, OPERATOR_ID, SentMessages, feed, make_callback, make_message

RUN = "qc:run"
SHIPPED_MAP = Path("config/field_maps/example_newdb.json")


def last(sent: SentMessages) -> str:
    return sent.texts[-1]


def buttons(sent: SentMessages) -> list[str]:
    return [
        button.text
        for markup in sent.markups
        if markup is not None and getattr(markup, "inline_keyboard", None)
        for row in markup.inline_keyboard
        for button in row
    ]


async def card_of(container: Container) -> QueryCard | None:
    async with container.database.session() as session:
        return await QueryCardRepository(session).get(OPERATOR_ID, CHAT_ID)


# ---------------------------------------------------------------- жалоба


async def test_the_owners_complaint_word_for_word(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """«Клочкова Елена Николаевна», потом «24 11 1994» — один человек с датой.

    Ни одного «а это к чему», ни одного прогона до кнопки, ровно один прогон
    после неё.
    """
    await feed(dispatcher, bot, message=make_message("Клочкова Елена Николаевна"))
    assert "Фамилия: Клочкова" in last(sent)
    assert "Имя: Елена" in last(sent)
    assert "Отчество: Николаевна" in last(sent)

    await feed(dispatcher, bot, message=make_message("24 11 1994"))
    assert "Дата рождения: 24.11.1994" in last(sent)
    assert "Фамилия: Клочкова" in last(sent)
    assert not sent.contains("Не нашёл в строке")
    assert not sent.contains("RECOVERY SCORE")

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    assert sent.contains("RECOVERY SCORE")

    async with container.database.session() as session:
        history = await SearchRepository(session).recent_for_user(OPERATOR_ID)
    assert len(history) == 1


# ---------------------------------------------------------------- ввод по частям


async def test_the_name_can_arrive_one_word_at_a_time(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    for word in ("Тестов", "Андрей", "Сергеевич"):
        await feed(dispatcher, bot, message=make_message(word))

    assert "Фамилия: Тестов" in last(sent)
    assert "Имя: Андрей" in last(sent)
    assert "Отчество: Сергеевич" in last(sent)


async def test_a_surname_then_the_rest_is_one_person(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """«Клочкова», затем «Елена Николаевна» — имя и отчество, а не новое ФИО.

    Сам :func:`parse_fio` так не умеет и уметь не должен: «Елена Николаевна» он
    законно читает фамилией с именем, потому что не знает, что фамилия уже
    есть. Знает карточка — она и решает.
    """
    await feed(dispatcher, bot, message=make_message("Клочкова"))
    await feed(dispatcher, bot, message=make_message("Елена Николаевна"))

    assert "Фамилия: Клочкова" in last(sent)
    assert "Имя: Елена" in last(sent)
    assert "Отчество: Николаевна" in last(sent)
    assert "другой человек или исправление" not in last(sent)


async def test_two_words_without_a_patronymic_are_a_different_person(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """«Иванов Иван» поверх «Клочкова» — это другой должник, а не имя Иванов.

    Граница проходит по форме второго слова, и она названа вслух: «Николаевна»
    дописывает начатое имя, «Иван» начинает новое.
    """
    await feed(dispatcher, bot, message=make_message("Клочкова"))
    await feed(dispatcher, bot, message=make_message("Иванов Иван"))

    assert "другой человек или исправление" in last(sent)


async def test_the_conflict_question_does_not_lock_the_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Модальный экран здесь был бы ловушкой: думать можно, работать нельзя."""
    await feed(dispatcher, bot, message=make_message("Петров Пётр Петрович"))
    await feed(dispatcher, bot, message=make_message("Клочкова Елена Николаевна"))

    assert "другой человек или исправление" in last(sent)
    assert "Проверить" in buttons(sent)
    assert "Дата рождения" in buttons(sent)


async def test_a_guess_is_shown_not_hidden(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Спрятанная догадка и есть тот молчаливый разбор, от которого лечим."""
    await feed(dispatcher, bot, message=make_message("Клочкова"))
    assert "записал в фамилию" in last(sent)


async def test_a_named_field_is_never_guessed_into_another_one(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Нажали «+ ИНН» — значит присланное читается ИНН и только ИНН."""
    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:inn"))
    await feed(dispatcher, bot, message=make_message("Клочкова"))

    assert "на ИНН физлица не похоже" in last(sent)
    row = await card_of(container)
    assert row is not None
    assert row.last_name is None
    assert row.inn is None


async def test_a_skipped_field_reads_as_not_asked(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:birth_date"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:skip"))

    assert "Дата рождения: пропустили" in last(sent)
    assert any("не спрашивали" in answer for answer in sent.callback_answers)


async def test_garbage_leaves_the_card_untouched(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    before = last(sent)
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("asdf"))

    assert "не понял" in last(sent)
    assert "Фамилия: Тестов" in last(sent)
    row = await card_of(container)
    assert row is not None
    assert row.first_name == "Андрей"
    assert "Фамилия: Тестов" in before


async def test_a_message_is_never_left_without_an_answer(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """На сообщение человека бот отвечает всегда, даже когда сказать нечего.

    Это дефект с прода, и он выглядел страшнее, чем был: заказчик нажал
    /start, прислал номер — и не получил НИЧЕГО. Номер уже лежал в её
    карточке с прошлого раза, ``apply`` вернул «не изменилось», и обработчик
    выходил молча. Со стороны это неотличимо от упавшего бота.

    Тишина стояла здесь намеренно и по разумной причине: Telegram отвечает на
    правку сообщения тем же текстом ошибкой 400, а её обработчик отправил бы
    карточку заново. Но лекарство оказалось хуже болезни. Правильный ответ —
    не править на месте, а ПЕРЕСТАВИТЬ карточку вниз: старое сообщение
    снимается, новое встаёт рядом с тем, что человек написал. И 400 не
    случается, потому что правки нет.
    """
    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    count = len(sent.texts)

    await feed(dispatcher, bot, message=make_message("12.03.1985"))

    assert len(sent.texts) > count, "то же значение второй раз осталось без ответа"


async def test_even_a_blank_message_gets_an_answer(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Пробелы — тоже сообщение, и на него тоже отвечают.

    Исключений из правила «сообщение без ответа не остаётся» нет намеренно:
    каждое такое исключение — это ещё одна дорога к молчащему боту, а отличить
    «я отправил случайно» от «бот меня не слышит» человек по пустому экрану не
    может.
    """
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    count = len(sent.texts)

    await feed(dispatcher, bot, message=make_message("   "))

    assert len(sent.texts) > count


# ---------------------------------------------------------------- десять цифр


@pytest.mark.parametrize(
    ("line", "expected_row"),
    [
        ("(916) 000-00-00", "Телефон: +7 (916) ***-**-00"),
        ("4515 384710", "Паспорт: 4515384710"),
    ],
    ids=["телефон по форме записи", "паспорт по форме записи"],
)
async def test_the_shape_decides_where_it_can(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, line: str, expected_row: str
) -> None:
    await feed(dispatcher, bot, message=make_message(line))
    assert expected_row in last(sent)
    assert "паспорт или телефон?" not in last(sent)


async def test_ten_joined_digits_are_asked_about_not_guessed(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Угаданный телефон закрывает единственный вход в мост «паспорт → ИНН»."""
    await feed(dispatcher, bot, message=make_message("9160000000"))

    assert "паспорт или телефон?" in last(sent)
    row = await card_of(container)
    assert row is not None
    assert row.phone_masked is None
    assert row.passport_masked is None


# ---------------------------------------------------------------- приватность


def test_the_table_stores_documents_but_never_the_phone() -> None:
    """Паспорт и СНИЛС — да, телефон — нет, и это две разные причины.

    Документы хранятся с 08.09.2026 по прямому указанию владелицы («нам надо
    наоборот сохранять эти номера») и по общему правилу базы: сама запись
    происходит только при поднятом ``STORE_SENSITIVE_IDENTIFIERS``, а маска
    рядом остаётся — по ней карточка отличает «было, но не сохранилось» от «не
    спрашивали».

    Телефона нет ни под каким флагом, и это не забыли: оператор вводит его сам
    и помнит, хранить нечего.
    """
    columns = {column.name for column in Base.metadata.tables["query_cards"].columns}
    assert {"passport", "snils"} <= columns
    assert "phone" not in columns
    assert {"passport_masked", "snils_masked", "phone_masked"} <= columns


async def test_a_passport_is_shown_kept_in_memory_and_left_out_of_history(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Паспорт виден целиком, маска рядом, в историю запросов не едет.

    Три разных правила, и они не спорят.

    ВИДЕН — потому что владелица отменила обещание «номер не сохраняю и
    сообщение удалю»: бот закрыт, принадлежит ей одной и добывает документы
    ровно затем, чтобы подать с ними в суд.

    МАСКА РЯДОМ — потому что при опущенном ``STORE_SENSITIVE_IDENTIFIERS``
    (умолчание, и в этом тесте тоже) сам номер в базу не едет, и после
    перезапуска карточка обязана сказать «было, но не сохранилось», а не
    показать прочерк.

    В ИСТОРИЮ НЕ ЕДЕТ — ``redact_subject`` вычёркивает паспорт из
    ``subject_json``. История это список «кто что искал», и превращать её в
    хранилище документов не нужно никому.
    """
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:passport"))
    await feed(dispatcher, bot, message=make_message("4509123456"))

    assert "Паспорт: 4509123456" in last(sent)

    row = await card_of(container)
    assert row is not None
    assert row.passport_masked == "45** ******"
    assert row.passport is None, "флаг опущен — самого номера в базе быть не должно"

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    async with container.database.session() as session:
        request = (await SearchRepository(session).recent_for_user(OPERATOR_ID))[0]
    assert "4509123456" not in request.subject_json


async def test_a_forgotten_secret_says_so_instead_of_showing_a_dash(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """После перезапуска маска на месте, номера нет — и это видно.

    Прочерк здесь соврал бы: «не спрашивали» и «было, но не сохраняю» — разные
    вещи, и оператор обязан различать их без чтения инструкции.
    """
    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:phone"))
    await feed(dispatcher, bot, message=make_message("+7 916 123 45 67"))
    assert "+7 (916) ***-**-67" in last(sent)

    # «Перезапуск»: сервис пересоздан, память процесса пуста, база — нет.
    container.query_cards = QueryCardService(container.database, container.settings)
    restarted = setup_dispatcher(Dispatcher(storage=MemoryStorage()), container)

    # Карточку открывают отменой вопроса: оговорка живёт в списке полей, а
    # экран вопроса полей не печатает — он задаёт одну строку.
    #
    # Полным ФИО тут пользоваться нельзя: оно опознаёт должника однозначно,
    # проверка уходит сразу, и карточка не рисуется вовсе.
    await feed(restarted, bot, callback_query=make_callback("qc:cancel"))

    assert "+7 (916) ***-**-67 — сам номер не храню, пришлите заново" in last(sent)


async def test_the_card_survives_a_restart_with_a_half_filled_person(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Хранилище FSM обнуляется, карточка читается из базы."""
    await feed(dispatcher, bot, message=make_message("Тестов Андрей"))

    restarted = setup_dispatcher(Dispatcher(storage=MemoryStorage()), container)
    await feed(restarted, bot, message=make_message("12.03.1985"))

    assert "Фамилия: Тестов" in last(sent)
    assert "Дата рождения: 12.03.1985" in last(sent)


# ---------------------------------------------------------------- деньги


async def test_an_empty_card_refuses_to_run(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback(RUN))

    assert any("нужны фамилия с именем" in answer for answer in sent.callback_answers)
    assert not container.subject_store._items


async def test_running_twice_without_a_change_is_refused(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    await feed(dispatcher, bot, callback_query=make_callback(RUN))

    assert any("ничего не добавилось" in answer for answer in sent.callback_answers)
    async with container.database.session() as session:
        history = await SearchRepository(session).recent_for_user(OPERATOR_ID)
    assert len(history) == 1


async def test_a_field_sent_after_the_report_keeps_the_person(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Регрессия, которую этот тест обязан фиксировать: ФИО и дата терялись."""
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback(RUN))

    await feed(dispatcher, bot, message=make_message("770912345601"))
    assert "Фамилия: Тестов" in last(sent)
    assert "Дата рождения: 12.03.1985" in last(sent)

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    subject = next(reversed(container.subject_store._items.values()))[0]
    assert subject.inn == "770912345601"
    assert subject.birth_date == date(1985, 3, 12)
    assert subject.name is not None
    assert subject.name.middle_name == "Сергеевич"


async def test_the_card_steps_aside_for_the_report(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """После проверки последним в чате остаётся отчёт, а не карточка.

    Старое сообщение карточки снимается — иначе она навсегда уезжает выше
    отчёта и оператор правит то, чего не видит. Но и заново под отчётом она
    не появляется: это было три сообщения на один введённый номер, из которых
    последнее повторяло всё сказанное выше и несло тринадцать кнопок.

    Карточка помечена проверенной, чтобы следующее открытие через «Уточнить
    данные» показало «Перепроверить», а не «Проверить».
    """
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985"))
    before = await card_of(container)
    assert before is not None

    await feed(dispatcher, bot, callback_query=make_callback(RUN))

    after = await card_of(container)
    assert after is not None
    assert after.card_message_id is None, "старое сообщение карточки не снято"
    assert after.checked_at is not None
    assert not last(sent).startswith("Проверка должника — проверено в ")
    assert "Уточнить данные" in buttons(sent)


# ---------------------------------------------------------------- честность


async def test_the_card_never_shows_a_completeness_score(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Полей семь, а связок три — линейная шкала соврала бы про полноту."""
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))

    text = last(sent)
    assert "из 7" not in text
    assert "%" not in text
    assert "Прочерк — это «я не спрашивал», а не «не нашли»." in text


async def test_the_report_names_the_sources_that_stayed_unqueried(
    container: Container, database: Database, live_settings: Settings
) -> None:
    """Отчёт по неполной карточке обязан назвать неопрошенное и причину.

    Считается на живом наборе источников: в демо все они ищут по одному ФИО, и
    разница между «нужен ИНН» и «нужна дата» там не наблюдаема.
    """
    wired = live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": "https://api.example.test",
            "newdb_field_map": SHIPPED_MAP,
            "fedresurs_backend": FedresursBackend.NEWDB,
            "fns_provider": FNSBackend.NEWDB,
        }
    )
    registry = ProviderRegistry(
        internal=build_internal_provider(wired, database),
        external=build_external_providers(wired),
        inn_bridge=build_inn_bridge(wired),
    )
    live = replace(container, settings=wired, registry=registry)
    subject = SearchSubject(
        search_type="person",
        name=PersonName(last_name="Тестов", first_name="Андрей"),
        birth_date=date(1985, 3, 12),
    )

    notes = handlers.run_notes(subject, live)

    assert notes
    assert "ЕФРСБ" in notes[0]
    assert "нужен ИНН физлица (12 цифр)" in notes[0]
    assert "«не спрашивали», а не «не найдено»" in notes[0]


async def test_a_different_surname_is_never_merged_silently(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Молчаливое слияние двух должников — самая дорогая ошибка карточки."""
    await feed(dispatcher, bot, message=make_message("Петров Пётр Петрович"))
    await feed(dispatcher, bot, message=make_message("Клочкова Елена Николаевна"))

    assert "другой человек или исправление?" in last(sent)
    assert "Петров Пётр Петрович" in last(sent)
    assert "Это исправление" in buttons(sent)

    await feed(dispatcher, bot, callback_query=make_callback("qc:keep"))
    row = await card_of(container)
    assert row is not None
    assert row.last_name == "Клочкова"


async def test_a_new_person_starts_from_a_clean_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, message=make_message("Петров Пётр Петрович 01.02.1979"))
    await feed(dispatcher, bot, message=make_message("Клочкова Елена Николаевна"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:new"))

    row = await card_of(container)
    assert row is not None
    assert row.last_name == "Клочкова"
    # Дата прежнего должника не осталась висеть на новом.
    assert row.birth_date is None


# ---------------------------------------------------------------- роутеры


@pytest.mark.parametrize(
    ("menu", "text", "expected"),
    [
        ("menu:vehicle_plate", "О123АА777", "Авто — не подключено"),
        ("menu:contract", "EV-20481", "НАШИ ДАННЫЕ"),
        ("menu:address", "Москва", "ФИО, если известно"),
        ("menu:passport", "4509123456", "4509123456"),
    ],
    ids=["госномер", "договор", "адрес", "паспорт"],
)
async def test_other_scenarios_still_get_their_own_text(
    dispatcher: Dispatcher,
    bot: Bot,
    sent: SentMessages,
    container: Container,
    menu: str,
    text: str,
    expected: str,
) -> None:
    """Карточка не сняла ``StateFilter(None)`` и ничей ввод не забрала.

    Это и есть цена решения не делать карточку состоянием FSM: порядок роутеров
    не тронут, и четыре сценария ввода работают как работали.
    """
    await feed(dispatcher, bot, callback_query=make_callback(menu))
    await feed(dispatcher, bot, message=make_message(text))

    assert sent.contains(expected)
    assert await card_of(container) is None


async def test_the_bottom_buttons_work_with_a_filled_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    from app.bot.keyboards import BUTTON_HISTORY

    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message(BUTTON_HISTORY))
    assert sent.contains("История")


# ---------------------------------------------------------------- ретеншен


async def test_retention_drops_stale_cards_and_keeps_fresh_ones(
    container: Container, settings: Settings
) -> None:
    """Карточка — черновик, а не история: свой срок, а не девяносто дней."""
    service = container.query_cards
    stale = await service.load(1, 1)
    stale.last_name = "Старый"
    await service.save(stale)
    fresh = await service.load(2, 2)
    fresh.last_name = "Свежий"
    await service.save(fresh)

    async with container.database.session() as session:
        row = await QueryCardRepository(session).get(1, 1)
        assert row is not None
        row.updated_at = utcnow() - timedelta(days=CARD_RETENTION_DAYS + 1)

    _, _, cards = await purge_once(settings, container.database)

    assert cards == 1
    async with container.database.session() as session:
        repo = QueryCardRepository(session)
        assert await repo.get(1, 1) is None
        assert await repo.get(2, 2) is not None


# ---------------------------------------------------------------- старые кнопки


async def test_an_old_report_button_pours_its_subject_into_the_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """``padd:*`` живёт в чате бесконечно и обязан продолжать работать.

    Субъект прошлого прогона вливается в карточку — так старый отчёт и новая
    карточка становятся одним разговором, а не двумя. Молчащая кнопка в истории
    хуже отсутствующей.
    """
    subject = SearchSubject(
        search_type="person",
        name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
        birth_date=date(1985, 3, 12),
    )
    token = container.subject_store.put(subject)

    await feed(dispatcher, bot, callback_query=make_callback(f"padd:inn:{token}"))

    assert "Фамилия: Тестов" in last(sent)
    assert "Дата рождения: 12.03.1985" in last(sent)
    assert "✎ ИНН" in last(sent)

    await feed(dispatcher, bot, message=make_message("770912345601"))
    row = await card_of(container)
    assert row is not None
    assert row.inn == "770912345601"
    assert row.last_name == "Тестов"


async def test_an_expired_token_says_so_instead_of_pretending(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("padd:inn:no-such-token"))
    assert sent.contains("Данные устарели")


async def test_the_miss_is_reported_once_not_over_every_question(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """«По номеру никого не нашёл» — новость ровно один раз.

    Дальше оператор отвечает на вопрос за вопросом, номер при этом не меняется,
    и строка висела над каждым следующим экраном, повторяя одно и то же три
    раза подряд. Такие экраны перестают читать целиком — вместе с тем, что
    правда ново.
    """
    await feed(dispatcher, bot, message=make_message("Проверить человека"))
    await feed(dispatcher, bot, message=make_message("79990001122"))

    assert "никого не нашёл" in last(sent)

    await feed(dispatcher, bot, message=make_message("Несуществующев"))

    assert "никого не нашёл" not in last(sent), "новость повторена на следующем вопросе"
    assert "✎ Имя" in last(sent), "разговор при этом обязан идти дальше"


# ---------------------------------------------------------------- первый шаг


async def test_the_first_step_asks_for_a_number_or_a_name(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Экран первого шага — четыре строки, и просит он телефон.

    Дословное указание владелицы: «сначала спрашиваем только номер телефона,
    номер либо пишется либо не пишется». Прежний экран звался «✎ Номер» и
    перечислял под собой «госномер, телефон, ИНН или номер договора» — этот
    скриншот она и прислала как то, что надо поменять.

    Сверяется текст целиком, а не подстрокой, и это главное в тесте: подстрока
    «Телефон» нашлась бы и в прежнем перечислении, и в строке «Принял —
    Телефон: …», то есть проверка проходила бы, ничего не проверяя. Заодно
    равенство сторожит длину: пятую строку на этом экране владелица возвращала.
    """
    await feed(dispatcher, bot, message=make_message("Проверить человека"))

    assert last(sent) == (
        "✎ Номер или ФИО\n"
        "\n"
        "Отправьте номер телефона или фамилию с именем.\n"
        "Например: Иванов Иван Иванович\n"
        "Не знаете — «Дальше»."
    )


@pytest.mark.parametrize("plate", ["А123ВС77", "A123BC77"])
async def test_the_first_step_still_takes_a_plate_whatever_the_layout(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, plate: str
) -> None:
    """Экран просит телефон, но присланный на него госномер доходит до отчёта.

    Телефона в выгрузке заказчика нет ни у одного из 2052 должников, госномер
    есть у 2008. Шаг, отвечающий «на телефон не похоже», отказал бы самому
    частому вводу — поэтому экран просит телефон (так велела владелица), а код
    принимает любой номер и раскладывает его по форме записи. Страховка ничего
    не стоит и молча спасает.

    Раскладка клавиатуры при этом не решает ничего: «А123ВС77» кириллицей и
    «A123BC77» латиницей — один и тот же госномер, и найтись обязан обоими.

    До этого теста поведение не сторожил никто: отключение ветки «первый шаг
    принимает любой номер» в :meth:`QueryCardService.apply` оставляло весь
    суите зелёным — то есть правку первого шага не поймал бы ни один гейт.
    """
    await feed(dispatcher, bot, message=make_message("Проверить человека"))
    await feed(dispatcher, bot, message=make_message(plate))

    assert "Тестов Андрей Сергеевич" in sent.joined, "госномер не поднял строку выгрузки"
    assert "RECOVERY SCORE" in sent.joined, "госномер на первом шаге не довёл до отчёта"
    assert "на телефон не похоже" not in sent.joined, "шаг отказал госномеру"


async def test_a_phone_nobody_has_leads_to_the_surname_question(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """По номеру никого — бот говорит об этом и спрашивает фамилию.

    Для телефона это единственный исход на проде: в выгрузке его нет ни у кого,
    а мост «телефон → ФИО» в этом развёртывании не подключён. Экран, который
    просит только телефон, приводит сюда каждый раз, поэтому ход обязан быть
    целым: «спросили и пусто» названо словами, отдельно от «не спрашивали», и
    следующий вопрос задан.

    Сломать это легко и незаметно: шаг, начавший отвергать всё, кроме телефона,
    или переставший двигать сценарий, оставил бы оператора на том же вопросе
    навсегда — с номером, по которому и не могло найтись.
    """
    await feed(dispatcher, bot, message=make_message("Проверить человека"))
    await feed(dispatcher, bot, message=make_message("79990001122"))

    screen = last(sent)
    assert "По номеру телефона никого не нашёл" in screen, "поиск был, а сказано о нём не было"
    assert "✎ Фамилия" in screen, "разговор упёрся в тот же вопрос"
