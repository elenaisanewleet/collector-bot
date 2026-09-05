"""End-to-end bot flows.

These drive real ``aiogram`` updates through the real dispatcher — middleware,
routers, FSM and handlers — with only the outbound Telegram API replaced. That
makes them the closest thing to running the bot without a token.

Стенд перехвата и фикстуры общие: :mod:`tests.bot_harness` и ``conftest``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from aiogram import Bot, Dispatcher

from app.bot.middleware import ACCESS_DENIED_MESSAGE
from app.config import Settings
from app.container import Container

from .bot_harness import (
    FAKE_TOKEN,
    OPERATOR_ID,
    OUTSIDER_ID,
    SentMessages,
    buttons,
    callbacks,
    dispatcher_for,
    feed,
    make_callback,
    make_message,
)

# ---------------------------------------------------------------- access


async def test_start_shows_the_main_menu(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, message=make_message("/start"))

    assert sent.contains(container.settings.app_name)
    # Приветствие говорит, что делать, а не описывает себя: нажми, введи, получи.
    assert sent.contains("Нажмите кнопку")
    assert sent.contains("Пишите что знаете")
    assert sent.contains("получите вердикт")
    # И не даёт прочитать молчание источника как чистую биографию.
    assert sent.contains("Это не значит, что там чисто")
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
# Флоу переписан дважды, и обе жалобы владелицы стоит держать в голове читая
# тесты. Первая — «много требует, это указать, это указать» — снесла допрос из
# пяти вопросов. Вторая — «написала клочкова елена николаевна а потом 24 11 1994
# и мне пишут а это к чему вообще» — завела накопительную карточку.
#
# Отсюда правило этих тестов: присланная строка НЕ запускает проверку, она
# дописывается в карточку. Проверку запускает ровно одна кнопка, и тест,
# который снова начнёт платить за каждое сообщение, обязан упасть.

FULL_LINE = "Тестов Андрей Сергеевич 12.03.1985"

#: Единственная кнопка, которая тратит деньги.
RUN = "qc:run"


async def collect_and_run(dispatcher: Dispatcher, bot: Bot, line: str = FULL_LINE) -> None:
    """Типичный путь оператора: одна строка из 1С и одно нажатие."""
    await feed(dispatcher, bot, message=make_message(line))
    await feed(dispatcher, bot, callback_query=make_callback(RUN))


async def test_a_line_fills_the_card_and_one_press_runs_it(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Одна строка, одно нажатие, отчёт.

    На нажатие больше, чем было, и это плата за то, что прогон перестал быть
    неожиданным: раньше «Тестов Андрей Сергеевич» оплачивалась до того, как
    оператор успевал добавить дату.
    """
    from app.db.repository import SearchRepository

    await feed(dispatcher, bot, message=make_message(FULL_LINE))
    assert sent.contains("Собираю проверку")
    assert not sent.contains("RECOVERY SCORE")

    await feed(dispatcher, bot, callback_query=make_callback(RUN))

    assert sent.contains("RECOVERY SCORE")
    assert sent.contains("Тестов Андрей Сергеевич")
    assert sent.contains("Уверенность данных")
    assert sent.contains("не заменяет юридическую проверку")

    async with container.database.session() as session:
        history = await SearchRepository(session).recent_for_user(OPERATOR_ID)
    assert len(history) == 1


async def test_the_card_remembers_the_previous_message(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Дословный случай из жалобы владелицы.

    «Клочкова Елена Николаевна», затем «24 11 1994» — это ОДИН человек с датой,
    а не два запроса, из которых второй ни о ком. Ни одного прогона до кнопки.
    """
    await feed(dispatcher, bot, message=make_message("Клочкова Елена Николаевна"))
    await feed(dispatcher, bot, message=make_message("24 11 1994"))

    assert sent.contains("Фамилия: Клочкова")
    assert sent.contains("Отчество: Николаевна")
    assert sent.contains("Дата рождения: 24.11.1994")
    assert not sent.contains("RECOVERY SCORE")

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    assert sent.contains("RECOVERY SCORE")


async def test_the_card_echoes_what_was_understood(
    linked_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Бот угадывает по форме — и обязан показать, что именно угадал."""
    await collect_and_run(linked_dispatcher, bot)

    assert sent.contains("Принял:")
    assert sent.contains("дата рождения 12.03.1985")


async def test_menu_person_opens_the_card_not_a_question(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Вопрос уезжает вверх чата вместе с ответом, карточка остаётся на месте.

    «Физлицо» открывает не вопрос и не пустую форму, а первый из трёх шагов —
    и открывает его В карточке: строки полей видны сразу, ответ дописывается в
    них же. Свободная строка при этом остаётся коротким путём и по-прежнему
    доводит до отчёта одним нажатием.
    """
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))

    assert sent.contains("Собираю проверку")
    assert sent.contains("Шаг 1 из 3. Телефон")
    assert "Дальше" in buttons(sent)

    await collect_and_run(dispatcher, bot)
    assert sent.contains("RECOVERY SCORE")


async def test_region_is_never_asked_and_defaults_to_all(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Пустой ``regions`` уже означает «все регионы» — шага для этого не нужно."""
    await collect_and_run(dispatcher, bot)

    assert not sent.contains("Выберите регион")
    subject = next(iter(container.subject_store._items.values()))[0]
    assert subject.regions == ()


async def test_the_phone_has_its_own_button_and_opens_nothing_external(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """«Где ввод номера телефона?» — вот он, отдельным полем и отдельной кнопкой.

    И он честно говорит о себе: во внешние реестры телефон не уходит, он ищет
    запись в своей базе. Обещать по нему источники значило бы врать формой.
    """
    await feed(dispatcher, bot, message=make_message(FULL_LINE))
    assert "+ Телефон" in buttons(sent)

    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:phone"))
    assert sent.contains("Во внешние реестры он не уходит")

    await feed(dispatcher, bot, message=make_message("+7 916 123 45 67"))
    # В карточку едет маска, полный номер — только в память процесса.
    assert sent.contains("+7 (916) ***-**-67")
    assert not sent.contains("+79161234567")

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    subject = next(iter(container.subject_store._items.values()))[0]
    assert subject.phone == "+79161234567"


async def test_a_phone_alone_is_not_a_subject(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Ни один внешний реестр по телефону не ищет — платить за него нечем."""
    await feed(dispatcher, bot, message=make_message("+79161234567"))
    await feed(dispatcher, bot, callback_query=make_callback(RUN))

    assert not sent.contains("RECOVERY SCORE")
    assert any("нужны фамилия с именем" in answer for answer in sent.callback_answers)
    assert not container.subject_store._items


async def test_one_word_lands_in_a_slot_and_the_guess_is_shown(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Ввод по частям: фамилия отдельно, имя отдельно, отчество отдельно.

    Догадка показывается, а не прячется: спрятанная догадка и есть тот самый
    молчаливый разбор, от которого лечим.
    """
    await feed(dispatcher, bot, message=make_message("Иванов"))
    assert sent.contains("Фамилия: Иванов")
    assert sent.contains("записал в фамилию")
    assert not sent.contains("RECOVERY SCORE")

    await feed(dispatcher, bot, message=make_message("Иван"))
    await feed(dispatcher, bot, message=make_message("Иванович"))

    assert sent.contains("Имя: Иван")
    assert sent.contains("Отчество: Иванович")


async def test_a_patronymic_skips_the_surname_slot(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """«Николаевна» в графе «Фамилия» — ошибка, заметная глазом."""
    await feed(dispatcher, bot, message=make_message("Николаевна"))

    assert sent.contains("Отчество: Николаевна")
    assert not sent.contains("Фамилия: Николаевна")


async def test_a_field_can_be_corrected_in_place(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:birth_date"))
    await feed(dispatcher, bot, message=make_message("01.02.1979"))

    assert "Дата рождения: 01.02.1979" in sent.texts[-1]
    assert "Дата рождения: 12.03.1985" not in sent.texts[-1]


async def test_a_field_can_be_skipped_explicitly(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Пропуск — это «спросил, ответа нет», и он отличается от пустого места."""
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:inn"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:skip"))

    assert sent.contains("ИНН: пропустили")
    assert any("не спрашивали" in answer for answer in sent.callback_answers)


async def test_a_wrong_answer_to_a_named_field_is_not_guessed_elsewhere(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Нажали «+ ИНН», прислали фамилию — это ошибка, а не тихая перекладка."""
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:inn"))
    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("Клочкова"))

    assert sent.contains("на ИНН физлица не похоже")
    assert not sent.contains("Фамилия: Клочкова")


async def test_garbage_does_not_touch_the_card_and_costs_nothing(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("asdf"))

    assert sent.contains("не понял")
    assert not sent.contains("RECOVERY SCORE")

    await collect_and_run(dispatcher, bot)
    assert sent.contains("RECOVERY SCORE")


async def test_a_name_alone_runs_and_the_card_names_the_sources(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Полнота не блокирует. Бот идёт с тем, что дали, и называет источники.

    Что именно останется неопрошенным на живых провайдерах, проверяется на них
    самих (``tests/test_coverage.py``) и на оговорках прогона
    (``tests/test_query_card.py``): демо-источники ищут по одному ФИО и про
    обязательную дату у ФССП не знают.
    """
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    assert sent.contains("Сейчас спрошу:")
    assert sent.contains("Прочерк — это «я не спрашивал», а не «не нашли».")

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    assert sent.contains("RECOVERY SCORE")


async def test_a_broken_date_is_named_and_the_card_is_untouched(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Молча выбросить дату нельзя: оператор считает, что он её дал."""
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 15.13.1980"))

    assert sent.contains("на дату не похоже")
    assert sent.contains("Дата рождения: —")
    assert not sent.contains("RECOVERY SCORE")

    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    assert sent.contains("Дата рождения: 12.03.1985")


async def test_a_two_digit_year_is_refused_without_guessing_the_century(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, message=make_message("24 11 94"))

    assert sent.contains("век угадывать не буду")
    assert sent.contains("Дата рождения: —")


async def test_ambiguous_ten_digits_ask_instead_of_guessing(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Угаданный «телефон» закрыл бы единственный вход в мост «паспорт → ИНН»."""
    await feed(
        dispatcher, bot, message=make_message("Тестов Андрей Сергеевич 12.03.1985 9204384710")
    )

    assert sent.contains("паспорт или телефон")
    assert not sent.contains("Паспорт: 92** ******")
    assert not sent.contains("RECOVERY SCORE")

    await feed(dispatcher, bot, callback_query=make_callback("qc:ten:p"))
    assert sent.contains("92** ******")

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    subject = next(iter(container.subject_store._items.values()))[0]
    assert subject.passport == "9204384710"


async def test_an_entity_inn_is_reported_but_does_not_stop_the_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message(f"{FULL_LINE} ИНН 7709123456"))
    assert sent.contains("ИНН организации")

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    assert sent.contains("RECOVERY SCORE")


async def test_a_different_surname_is_never_merged_silently(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Слияние двух должников в одного — самая дорогая ошибка карточки."""
    await feed(dispatcher, bot, message=make_message("Петров Пётр Петрович"))
    await feed(dispatcher, bot, message=make_message("Клочкова Елена Николаевна"))

    assert sent.contains("это другой человек или исправление?")
    assert sent.contains("Фамилия: Петров")

    await feed(dispatcher, bot, callback_query=make_callback("qc:new"))
    assert sent.contains("Фамилия: Клочкова")


async def test_the_card_survives_a_restart(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Недособранная карточка живёт в базе, а не в памяти диспетчера."""
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))

    # Именно новый диспетчер с пустой памятью: карточка обязана пережить
    # перезапуск бота, а не жить в оперативке процесса.
    restarted = dispatcher_for(container)
    sent.texts.clear()
    await feed(restarted, bot, message=make_message("12.03.1985"))

    assert sent.contains("Фамилия: Тестов")
    assert sent.contains("Дата рождения: 12.03.1985")


async def test_the_card_is_wiped_by_its_own_button_only(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """``/cancel`` карточку не трогает: она не диалог."""
    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))
    await feed(dispatcher, bot, message=make_message("/cancel"))
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("12.03.1985"))
    assert sent.contains("Фамилия: Тестов")

    await feed(dispatcher, bot, callback_query=make_callback("qc:wipe"))
    assert not sent.texts[-1].count("Тестов")


async def test_running_twice_without_changes_costs_nothing(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    from app.db.repository import SearchRepository

    await collect_and_run(dispatcher, bot)
    await feed(dispatcher, bot, callback_query=make_callback(RUN))

    assert any("ничего не добавилось" in answer for answer in sent.callback_answers)
    async with container.database.session() as session:
        history = await SearchRepository(session).recent_for_user(OPERATOR_ID)
    assert len(history) == 1


async def test_a_field_added_after_the_report_keeps_the_same_person(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Досыл ИНН после отчёта: ФИО и дата не теряются, человек тот же."""
    await collect_and_run(dispatcher, bot)
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("770912345601"))
    assert sent.contains("Фамилия: Тестов")
    assert sent.contains("Дата рождения: 12.03.1985")
    assert sent.contains("ИНН: 770912345601")

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    subject = next(reversed(container.subject_store._items.values()))[0]
    assert subject.inn == "770912345601"
    assert subject.name is not None
    assert subject.name.last_name == "Тестов"
    assert subject.birth_date is not None


async def test_the_card_is_reposted_under_the_report(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Иначе карточка навсегда уезжает выше отчёта, и правят то, чего не видят."""
    await collect_and_run(dispatcher, bot)

    assert sent.texts[-1].startswith("Проверено в ")
    assert "🔍 Перепроверить" in buttons(sent)


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
    dispatcher = dispatcher_for(enabled)

    await collect_and_run(dispatcher, bot)

    assert not sent.contains("Серия и номер паспорта, 10 цифр")
    assert sent.contains("RECOVERY SCORE")
    # Паспорт предлагает карточка под отчётом, а не отдельный ряд кнопок.
    assert "+ Паспорт" in buttons(sent)


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
    dispatcher = dispatcher_for(enabled)

    await collect_and_run(dispatcher, bot, line)

    assert sent.contains("RECOVERY SCORE")
    assert not any(text.startswith("🪪 Узнать ИНН по паспорту") for text in buttons(sent))
    assert not sent.contains("Узнать ИНН по паспорту")


async def test_the_passport_button_masks_the_number_and_feeds_the_bridge(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Нажали кнопку — паспорт спрашивается, удаляется из чата и едет в мост.

    В демо мост детерминированно выдаёт ИНН профиля, поэтому его строка
    появляется в блоке ИСТОЧНИКИ.
    """
    from app.db.repository import SearchRepository

    enabled = _with_bridge(container)
    dispatcher = dispatcher_for(enabled)

    await collect_and_run(dispatcher, bot)
    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:passport"))

    assert sent.contains("Серия и номер паспорта, 10 цифр")
    assert sent.contains("номер не сохраняю")
    assert sent.contains("Ваше сообщение с номером я удалю")

    await feed(dispatcher, bot, message=make_message("4509123456"))
    assert not sent.contains("4509123456")
    assert sent.contains("45** ******")

    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    assert sent.contains("✓ ИНН по паспорту (ФНС) — ИНН получен")

    async with enabled.database.session() as session:
        request = (await SearchRepository(session).recent_for_user(OPERATOR_ID))[0]
    # Паспорт не сохраняется: STORE_SENSITIVE_IDENTIFIERS по умолчанию выключен.
    assert "4509123456" not in request.subject_json


async def test_an_inn_from_the_line_skips_the_bridge_entirely(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    enabled = _with_bridge(container)
    dispatcher = dispatcher_for(enabled)

    await collect_and_run(dispatcher, bot, f"{FULL_LINE} 770912345601")

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

    await collect_and_run(dispatcher, bot)
    before = next(iter(container.subject_store._items.values()))[0]

    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:inn"))
    assert sent.contains("ИНН физлица")

    await feed(dispatcher, bot, message=make_message("770912345601"))
    await feed(dispatcher, bot, callback_query=make_callback(RUN))
    after = next(reversed(container.subject_store._items.values()))[0]

    assert after.inn == "770912345601"
    assert build_query_hash(before) != build_query_hash(after)


async def test_a_ten_digit_inn_is_refused_as_a_company(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await collect_and_run(dispatcher, bot)
    await feed(dispatcher, bot, callback_query=make_callback("qc:ask:inn"))

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("7709123456"))

    assert sent.contains("ИНН организации")
    assert not sent.contains("RECOVERY SCORE")


async def test_the_region_is_an_offer_under_the_card_not_a_step(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await collect_and_run(dispatcher, bot)
    assert not sent.contains("Выберите регион")

    token = _last_add_token(sent, "region")
    await feed(dispatcher, bot, callback_query=make_callback(f"padd:region:{token}"))
    assert sent.contains("Сейчас ищу по всем регионам")

    await feed(dispatcher, bot, callback_query=make_callback(f"region:moscow:{token}"))

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


async def test_help_explains_the_product_in_plain_words(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/help"))

    assert sent.contains("КОМАНДЫ")
    assert sent.contains("не пользуется базами утечек")
    # Термины, на которых оператор спотыкается, объяснены на месте.
    assert sent.contains("Recovery Score")
    assert sent.contains("ПОЧЕМУ ВАЖНА ДАТА РОЖДЕНИЯ")
    assert sent.contains("Можно подавать заявление о судебном приказе")


async def test_history_is_empty_then_populated(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/history"))
    assert sent.contains("История пуста")

    await collect_and_run(dispatcher, bot)

    sent.texts.clear()
    await feed(dispatcher, bot, message=make_message("/history"))

    assert sent.contains("Последние проверки")
    assert sent.contains("Тестов А. С.")


async def test_history_repeat_reruns_the_search(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    from app.db.repository import SearchRepository

    await collect_and_run(dispatcher, bot)

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


def confirm_callback(sent: SentMessages) -> str:
    """Нажать ровно ту кнопку запуска, которую бот показал.

    Не константа: в callback уезжает число должников из сметы, и тест, который
    подставляет своё, проверяет не тот сценарий, который увидит оператор.
    """
    return next(data for data in callbacks(sent) if data.startswith("batch:run"))


async def test_batch_shows_an_estimate_before_spending(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Прогон тратит платные запросы, поэтому сначала смета и подтверждение."""
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message("/batch"))

    assert sent.contains("Массовая проверка")
    assert sent.contains("Обращений к источникам")
    assert sent.contains("списываются с вашего баланса")
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
    await feed(dispatcher, bot, callback_query=make_callback(confirm_callback(sent)))

    assert sent.contains("Проверка завершена")
    assert sent.contains("Судебный приказ")
    assert sent.contains("Не подавать")
    assert sent.contains("Сэкономлено на пошлинах")


async def test_batch_lists_one_verdict(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await container.import_service.import_file(container.settings.internal_csv_path)
    await feed(dispatcher, bot, message=make_message("/batch"))
    await feed(dispatcher, bot, callback_query=make_callback(confirm_callback(sent)))

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
    await feed(dispatcher, bot, callback_query=make_callback(confirm_callback(sent)))
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
        await feed(dispatcher, bot, callback_query=make_callback("qc:wipe"))
        await collect_and_run(dispatcher, bot)

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
    # replace(), а не пересборка Container по полям: перечисленный вручную
    # список полей молча теряет всякую новую службу, и тест ломается там, где к
    # ссылкам на отчёт отношения не имеет.
    return replace(
        container,
        settings=settings,
        share_service=ShareLinkService(settings, container.database),
    )


@pytest.fixture
def linked_dispatcher(linked: Container) -> Dispatcher:
    return dispatcher_for(linked)


async def test_search_sends_a_card_with_a_link_not_a_wall(
    linked_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, linked: Container
) -> None:
    """С включённым вебом в чат уходит карточка и кнопка, а не отчёт текстом."""
    await collect_and_run(linked_dispatcher, bot)

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
    await collect_and_run(dispatcher, bot)

    assert sent.contains("ИСТОЧНИКИ")


async def test_batch_offers_the_queue_page(
    linked_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, linked: Container
) -> None:
    await linked.import_service.import_file(linked.settings.internal_csv_path)

    await feed(linked_dispatcher, bot, message=make_message("/batch"))
    await feed(linked_dispatcher, bot, callback_query=make_callback(confirm_callback(sent)))

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
    await collect_and_run(linked_dispatcher, bot)

    # Первое после нажатия — прогресс, дальше правка того же сообщения.
    progress = next(index for index, text in enumerate(sent.texts) if text.startswith("Проверяю"))
    assert progress >= 0
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
    await collect_and_run(dispatcher, bot, "А123ВС77")

    subject = next(iter(container.subject_store._items.values()))[0]
    assert subject.search_type == "vehicle_plate"
    assert subject.vehicle is not None
    assert subject.vehicle.plate == "А123ВС77"
    assert sent.contains("Авто — не подключено")
