"""Нижняя клавиатура: она есть, она не пропадает, и её кнопки никого не грабят.

Три вопроса, на которые отвечают эти тесты.

*   Приходит ли клавиатура вообще и с теми ли флагами — свёрнутая или
    одноразовая клавиатура ничем не лучше её отсутствия, а отсутствие и было
    жалобой.
*   Ведёт ли каждая кнопка туда, что на ней написано. Кнопка, за которой ничего
    нет, — это ровно та «🔍 Новая проверка», что висела часиками до таймаута.
*   Не крадёт ли обработчик кнопки чужой ввод. Этот риск в нижней клавиатуре
    единственный настоящий: Telegram присылает нажатие обычным текстом, и
    небрежный фильтр съел бы номер договора или фамилию.
"""

from __future__ import annotations

import string

import pytest
from aiogram import Bot, Dispatcher
from aiogram.types import ReplyKeyboardMarkup

from app.bot.handlers.start import KEYBOARD_HINT
from app.bot.keyboards import (
    BUTTON_BATCH,
    BUTTON_HELP,
    BUTTON_HISTORY,
    BUTTON_SEARCH,
    BUTTON_SOURCES,
    REPLY_BUTTONS,
    main_reply_keyboard,
)
from app.container import Container

from .bot_harness import SentMessages, feed, make_callback, make_message


def _labels(markup: ReplyKeyboardMarkup) -> list[str]:
    return [button.text for row in markup.keyboard for button in row]


def _bottom_keyboards(sent: SentMessages) -> list[ReplyKeyboardMarkup]:
    return [markup for markup in sent.markups if isinstance(markup, ReplyKeyboardMarkup)]


# ---------------------------------------------------------------- сама клавиатура


def test_keyboard_stays_open_and_does_not_collapse() -> None:
    """Флаги — это и есть «постоянно на экране», всё остальное здесь косметика."""
    markup = main_reply_keyboard()

    assert markup.resize_keyboard is True
    assert markup.is_persistent is True
    # one_time_keyboard=True закрывал бы клавиатуру после первого же нажатия.
    assert not markup.one_time_keyboard


def test_keyboard_holds_between_three_and_five_buttons() -> None:
    """Нижняя клавиатура занимает экран всегда, поэтому в ней только частое."""
    labels = _labels(main_reply_keyboard())

    assert 3 <= len(labels) <= 5
    assert labels == list(REPLY_BUTTONS)


# Всё, что человек может набрать с русской или английской раскладки. Значок в
# начале подписи не должен состоять из этого — на том и держится точное
# совпадение.
TYPEABLE_LETTERS = frozenset(string.ascii_letters + "ёЁ") | frozenset(
    chr(code) for code in range(ord("А"), ord("я") + 1)
)


@pytest.mark.parametrize("label", REPLY_BUTTONS)
def test_every_label_starts_with_an_emoji(label: str) -> None:
    """Эмодзи — не украшение, а то, чем нажатие отличается от набранного текста.

    Подпись без значка человек однажды наберёт руками, и обработчик кнопки
    украдёт этот ввод у сценария, в котором человек находится. Поэтому проверяем
    не «красиво», а «ни одной буквы с клавиатуры в первом слове».
    """
    mark, space, words = label.partition(" ")

    assert space, f"{label}: значок и подпись слитно"
    assert words
    assert not (frozenset(mark) & TYPEABLE_LETTERS), f"{label}: значок набирается руками"


def test_nothing_in_the_bot_takes_the_keyboard_away() -> None:
    """«Не пропадает» — это отсутствие ``ReplyKeyboardRemove``, а не обещание.

    Клавиатура держится на стороне Telegram, пока её не снимут явно. Снять её
    может только этот класс, и в коде бота его быть не должно.
    """
    from pathlib import Path

    sources = list(Path("app").rglob("*.py"))
    assert sources
    offenders = [path for path in sources if "ReplyKeyboardRemove" in path.read_text()]
    assert offenders == []


# ---------------------------------------------------------------- доставка


async def test_start_delivers_the_keyboard(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/start"))

    keyboards = _bottom_keyboards(sent)
    assert len(keyboards) == 1
    assert _labels(keyboards[0]) == list(REPLY_BUTTONS)
    # И сказано, что это такое: «нажми туда» без «туда» — половина подсказки.
    assert sent.contains(KEYBOARD_HINT)


async def test_the_welcome_keeps_its_inline_menu(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """У сообщения не бывает обеих клавиатур сразу, и инлайн-меню важнее.

    Поэтому нижняя приезжает следующим сообщением, а не вместо приветственного
    меню: там десять пунктов, из которых в нижние кнопки влезло пять.
    """
    await feed(dispatcher, bot, message=make_message("/start"))

    first = sent.markups[0]
    assert first is not None
    assert not isinstance(first, ReplyKeyboardMarkup)


async def test_the_keyboard_is_sent_once_per_start(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Клавиатура — не сообщение, а состояние чата: дописывать её к каждому
    ответу бота значило бы отбирать инлайн-кнопки у отчётов и меню."""
    await feed(dispatcher, bot, message=make_message("/start"))
    await feed(dispatcher, bot, message=make_message(BUTTON_SOURCES))
    await feed(dispatcher, bot, message=make_message(BUTTON_HELP))

    assert len(_bottom_keyboards(sent)) == 1


# ---------------------------------------------------------------- куда ведут кнопки


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        # Пустая база — это и есть ответ массовой проверки на пустую базу,
        # то есть кнопка дошла до /batch.
        (BUTTON_BATCH, "Внутренняя база пуста"),
        (BUTTON_SEARCH, "Выберите тип проверки"),
        (BUTTON_HISTORY, "История пуста"),
        (BUTTON_SOURCES, "ОТКУДА ДАННЫЕ"),
        (BUTTON_HELP, "как это работает"),
    ],
)
async def test_each_button_answers_like_its_command(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, label: str, expected: str
) -> None:
    await feed(dispatcher, bot, message=make_message(label))

    assert sent.contains(expected), f"{label} никуда не привела: {sent.texts}"


async def test_the_batch_button_shows_the_estimate(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Прогон тратит платные запросы, и кнопка обязана вести к смете, а не к запуску."""
    await container.import_service.import_file(container.settings.internal_csv_path)

    await feed(dispatcher, bot, message=make_message(BUTTON_BATCH))

    assert sent.contains("Массовая проверка")
    assert sent.contains("списываются с вашего баланса")
    assert not sent.contains("Проверка завершена")


async def test_the_search_button_keeps_the_rarer_searches_reachable(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Кнопка ведёт в меню типов, а не сразу в ввод ФИО.

    Иначе проверка по номеру договора — самый частый вход у заказчицы — стала бы
    недостижима с клавиатуры вовсе.
    """
    await feed(dispatcher, bot, message=make_message(BUTTON_SEARCH))

    markup = sent.markups[-1]
    assert markup is not None
    payloads = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "menu:contract" in payloads
    assert "menu:person" in payloads


# ---------------------------------------------------------------- чужой ввод


async def test_a_typed_label_without_the_emoji_is_not_a_press(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Текст, похожий на подпись кнопки, остаётся вводом того, кто его набрал.

    Нажатие присылает подпись байт в байт, вместе со значком. Всё остальное —
    свободный ввод, и забрать его себе обработчик кнопки не вправе: три слова на
    шаге ввода ФИО — попытка ввести ФИО, чем бы они ни были.
    """
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("Проверить всю базу"))

    assert not sent.contains("Массовая проверка")
    assert not sent.contains("Внутренняя база пуста")
    # Ввод достался карточке, а не кнопке: набранный текст — это данные про
    # должника, чем бы они ни были похожи на подпись кнопки.
    assert sent.contains("Собираю проверку")


async def test_a_contract_number_that_reads_like_a_button_still_searches(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Свободный ввод принимает что угодно, и слово «История» здесь — запрос."""
    await feed(dispatcher, bot, callback_query=make_callback("menu:contract"))
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("История"))

    assert sent.contains("Во внутренней базе ничего не найдено")
    assert not sent.contains("История пуста")


async def test_a_button_press_survives_a_dialog_that_wants_text(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Обратная сторона: посреди ввода ФИО нажатие обязано сработать.

    Это причина, по которой роутер кнопок подключён первым: хендлер состояния
    забирает себе весь текст, дошедший до его роутера, и разобрал бы «🕘 История»
    как фамилию.
    """
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message(BUTTON_HISTORY))

    assert sent.contains("История пуста")
    assert not sent.contains("как минимум фамилия и имя")


# ---------------------------------------------------------------- и что со сценарием


async def test_reading_buttons_do_not_abandon_a_half_finished_search(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Спросить «откуда данные» посреди ввода — не повод потерять введённое."""
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message(BUTTON_SOURCES))
    assert sent.contains("ОТКУДА ДАННЫЕ")
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("Тестов Андрей Сергеевич"))

    # Человек вернулся ровно к своей карточке, а не к пустому месту.
    assert sent.contains("Фамилия: Тестов")


async def test_starting_buttons_do_not_lose_the_card(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Кнопка другого сценария чистит СОСТОЯНИЕ, но не карточку.

    Карточка — не диалог: она живёт в базе, а не в FSM, и «посмотрел историю»
    не повод стирать наполовину собранного должника. Строка, никого не
    опознавшая в выгрузке, денег по-прежнему не тратит: платит кнопка.
    """
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, message=make_message(BUTTON_HISTORY))
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("Неизвестнов Пётр Петрович"))

    assert sent.contains("Фамилия: Неизвестнов")
    assert not sent.contains("RECOVERY SCORE")
