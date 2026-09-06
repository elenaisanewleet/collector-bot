"""Лицо бота: приветствие с баннером, экран «Откуда данные», меню команд.

Проверяется не вёрстка, а обещания. Приветствие не должно обещать того, чего бот
не делает; экран источников не должен показывать подключённым то, что не
подключено; картинка не должна уносить с собой ``/start``.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher

from app.bot import banner
from app.bot.commands import BOT_COMMANDS, commands_help, telegram_commands
from app.bot.handlers.start import welcome_text
from app.bot.sources import sources_screen
from app.container import Container
from app.utils.formatting import split_message

from .bot_harness import BANNER_FILE_ID, SentMessages, feed, make_callback, make_message


@pytest.fixture(autouse=True)
def forget_banner_file_id() -> Iterator[None]:
    """file_id живёт в модуле; без сброса тесты видели бы чужую отправку."""
    banner.forget_file_id()
    yield
    banner.forget_file_id()


# ---------------------------------------------------------------- баннер


def test_banner_ships_with_the_package() -> None:
    """Картинка лежит внутри ``app``, иначе её не будет ни в wheel, ни в образе.

    ``packages = ["app"]`` в pyproject и ``COPY app ./app`` в Dockerfile берут
    только этот каталог: баннер, положенный в корневой ``assets/``, собрался бы
    локально и упал бы в проде.
    """
    assert banner.BANNER_PATH.is_file()
    assert banner.BANNER_PATH.stat().st_size > 0
    assert banner.BANNER_PATH.parent.parent.name == "app"


def test_build_carries_the_assets_directory() -> None:
    """Картинка, не попавшая в образ, роняет приветствие только в проде.

    Локально и в тестах файл лежит на диске и всё зелено, поэтому проверяем не
    его наличие, а то, что сборка вообще забирает каталог: ``packages = ["app"]``
    в wheel и ``COPY app ./app`` в образе.
    """
    pyproject = Path("pyproject.toml").read_text()
    assert 'packages = ["app"]' in pyproject

    dockerfile = Path("Dockerfile").read_text()
    assert "COPY --chown=app:app app ./app" in dockerfile

    ignored = Path(".dockerignore").read_text().split()
    assert not {"*.jpg", "*.png", "app/assets/", "assets/"} & set(ignored)


def test_welcome_fits_into_a_photo_caption(container: Container) -> None:
    """Подпись длиннее лимита Telegram отвергает всё сообщение целиком."""
    assert len(welcome_text(container)) <= banner.CAPTION_LIMIT


async def test_start_sends_the_banner_with_the_menu(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, message=make_message("/start"))

    assert len(sent.photos) == 1
    path, caption = sent.photos[0]
    assert path.endswith("welcome.jpg")
    assert caption is not None
    assert container.settings.app_name in caption
    assert sent.markups[0] is not None


async def test_second_start_reuses_the_uploaded_file_id(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Файл перезаливается только один раз: дальше Telegram хранит его сам."""
    await feed(dispatcher, bot, message=make_message("/start"))
    await feed(dispatcher, bot, message=make_message("/start"))

    assert sent.photos[0][0].endswith("welcome.jpg")
    assert sent.photos[1][0] == BANNER_FILE_ID


async def test_start_survives_a_missing_banner(
    dispatcher: Dispatcher,
    bot: Bot,
    sent: SentMessages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Украшение не имеет права выключать главную команду бота."""
    monkeypatch.setattr(banner, "BANNER_PATH", Path("/nonexistent/welcome.jpg"))

    await feed(dispatcher, bot, message=make_message("/start"))

    assert sent.photos == []
    assert sent.contains("стоит ли тратить пошлину")
    assert sent.markups[0] is not None


async def test_start_survives_a_telegram_refusal(
    dispatcher: Dispatcher,
    bot: Bot,
    sent: SentMessages,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aiogram.types import Message

    async def refuse(self: Message, *args: Any, **kwargs: Any) -> Message:
        raise RuntimeError("Telegram отказал в загрузке фото")

    monkeypatch.setattr(Message, "answer_photo", refuse, raising=True)

    await feed(dispatcher, bot, message=make_message("/start"))

    assert sent.contains("стоит ли тратить пошлину")


# ---------------------------------------------------------------- приветствие


def test_welcome_promises_only_what_the_bot_does(container: Container) -> None:
    text = welcome_text(container).lower()
    for overclaim in ("узнай всё", "по номеру телефона", "счета", "имущество", "пробив"):
        assert overclaim not in text
    assert "не проверено" in text


def test_welcome_says_what_to_do_and_stays_short(container: Container) -> None:
    """Первый экран говорит, что нажать, а не описывает себя.

    Были три пронумерованных шага с эмодзи — экран, который читают по диагонали.
    Осталось одно действие: нажать кнопку и прислать телефон. Оговорка про «не
    проверено» остаётся при любом сокращении: разницу между «не смотрели» и
    «чисто» надо узнать до первого отчёта.
    """
    text = welcome_text(container)

    assert "Проверить человека" in text
    assert "номер телефона" in text
    assert "не проверено" in text
    # Ненавязчиво — значит без пиктограмм в тексте.
    assert not any(mark in text for mark in ("1️⃣", "2️⃣", "3️⃣", "⚠️"))
    assert len(text.splitlines()) <= 10


# ---------------------------------------------------------------- откуда данные


def test_sources_screen_names_the_customers_own_1c(container: Container) -> None:
    screen = sources_screen(container, debtors=0)

    assert "ВАША 1С — ПРО ДОЛГ" in screen
    assert "/import" in screen
    # 1С — про долг, реестры — про должника; иначе экран не отвечает на вопрос.
    assert "Ваша 1С знает, сколько должны вам" in screen
    assert "О вашем долге они не знают ничего" in screen


def test_sources_screen_shows_live_state(container: Container) -> None:
    """Состояние берётся из реестра, а не из списка в тексте."""
    screen = sources_screen(container, debtors=0)

    for provider in container.registry.external:
        title = provider.name.value
        expected = "подключено" if provider.is_configured else "не подключено"
        assert expected in screen, title
    # Заглушки в демо остаются заглушками.
    assert "○ Объект по адресу (ЕГРН) — не подключено" in screen
    assert "○ Наследственные дела — не подключено" in screen


def test_sources_screen_keeps_the_scope_notes(container: Container) -> None:
    """«Залоги — подключено» без оговорки обещает больше, чем бот проверяет."""
    from app.services.reporting import (
        COURT_SCOPE_NOTE,
        INHERITANCE_SCOPE_NOTE,
        PLEDGE_SCOPE_NOTE,
    )

    screen = sources_screen(container, debtors=0)
    assert PLEDGE_SCOPE_NOTE in screen
    assert COURT_SCOPE_NOTE in screen
    # Реестр наследственных дел ищет по одному ФИО и отвечает про всех
    # однофамильцев: без этой оговорки экран обещает проверку конкретного
    # человека, которой источник не делает.
    assert INHERITANCE_SCOPE_NOTE in screen


def test_sources_screen_separates_unchecked_from_clean(container: Container) -> None:
    from app.services.reporting import EMPTY_LABEL, NOT_CONFIGURED_REPORT_LINE

    screen = sources_screen(container, debtors=0)
    # Подписи те же слово в слово, что в отчёте и на веб-странице.
    assert NOT_CONFIGURED_REPORT_LINE in screen
    assert EMPTY_LABEL in screen


def test_sources_screen_states_the_limits(container: Container) -> None:
    screen = sources_screen(container, debtors=0)
    assert "не найдёт незнакомого человека по номеру телефона" in screen
    assert "не покажет банковские счета" in screen
    assert "ЕГРН не подключён" in screen


def test_internal_state_is_live_not_a_constant_tick(container: Container) -> None:
    empty = sources_screen(container, debtors=0)
    filled = sources_screen(container, debtors=812)

    assert "812 записей в базе" in filled
    # Демо-настройки указывают на существующий файл выгрузки, и пустая база при
    # живом файле — это всё ещё подключённый источник, но с другой подписью.
    assert "812 записей" not in empty


def test_internal_state_reports_an_absent_export(container: Container) -> None:
    from dataclasses import replace

    without_csv = replace(
        container,
        settings=container.settings.model_copy(
            update={"internal_csv_path": Path("/nonexistent/debtors.csv")}
        ),
    )
    line = sources_screen(without_csv, debtors=0)
    assert "не подключено: база пуста, выгрузка не загружена (/import)" in line


def test_sources_screen_fits_telegram(container: Container) -> None:
    """Экран длинный, но в чат он обязан уйти целиком, а не обрезком."""
    chunks = split_message(sources_screen(container, debtors=812))
    assert chunks
    assert all(len(chunk) <= 4096 for chunk in chunks)


async def test_sources_command_answers(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/sources"))
    assert sent.contains("ОТКУДА ДАННЫЕ")


async def test_sources_button_answers(dispatcher: Dispatcher, bot: Bot, sent: SentMessages) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:sources"))
    assert sent.contains("ОТКУДА ДАННЫЕ")
    assert sent.callback_answers  # часики погашены, а не висят до таймаута


async def test_sources_works_in_the_middle_of_a_dialog(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Спросить «куда пойдут эти данные» человек вправе на шаге ввода ФИО."""
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("/sources"))

    assert sent.contains("ОТКУДА ДАННЫЕ")
    assert not sent.contains("Нужно как минимум фамилия и имя")


async def test_help_works_in_the_middle_of_a_dialog(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("/help"))

    assert sent.contains("как это работает")
    assert not sent.contains("Нужно как минимум фамилия и имя")


# ---------------------------------------------------------------- меню и кнопки


async def test_new_search_button_is_not_a_dead_end(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """«Новая проверка» под отчётом раньше висела часиками до таймаута."""
    await feed(dispatcher, bot, callback_query=make_callback("menu:back"))

    assert sent.contains("Что делаем?")
    assert sent.callback_answers


async def test_new_search_button_leaves_the_card_alone(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """«Новая проверка» возвращает в меню, но собранного должника не стирает.

    Карточку очищает только её собственная кнопка: потерять наполовину
    введённого человека от нажатия на меню — это ровно та потеря ввода, ради
    прекращения которой карточка и заведена.

    Человек взят заведомо не из выгрузки. В выгрузке бот его опознал бы и по
    своему главному правилу пошёл бы готовить отчёт сам — а здесь проверяется
    ровно обратное: строка, никого не опознавшая, денег не тратит.
    """
    await feed(dispatcher, bot, callback_query=make_callback("menu:person"))
    await feed(dispatcher, bot, callback_query=make_callback("menu:back"))
    sent.texts.clear()

    await feed(dispatcher, bot, message=make_message("Неизвестнов Пётр Петрович"))

    assert sent.contains("Фамилия: Неизвестнов")
    assert not sent.contains("RECOVERY SCORE")


def test_report_button_and_handler_share_one_payload() -> None:
    # report_keyboard переехала в report_actions: в keyboards оставался дубль,
    # и main его убрал. Тест сторожит связку кнопки и обработчика, а не место.
    from app.bot.keyboards import BACK_CALLBACK
    from app.bot.report_actions import report_keyboard
    from app.domain.identity import SearchSubject, parse_fio

    markup = report_keyboard(
        url="https://example.test/r/1",
        refresh_token=None,
        subject=SearchSubject(search_type="person", name=parse_fio("Тестов Андрей Сергеевич")),
        bridge=None,
    )
    assert markup is not None
    payloads = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert BACK_CALLBACK in payloads


def test_menu_offers_the_reading_screens() -> None:
    """Справочные экраны достижимы кнопкой, но не в главном меню.

    В главном меню было одиннадцать кнопок, и это назвали кучей. Каждый день
    нажимают две; справку читают один раз, поэтому она за «Другие способы
    поиска» — достижима без слеша, но не мозолит глаза.
    """
    from app.bot.keyboards import MENU_MORE, main_menu, more_menu

    main = [button.callback_data for row in main_menu().inline_keyboard for button in row]
    assert main == ["menu:person", "batch:start", MENU_MORE]

    more = [button.callback_data for row in more_menu().inline_keyboard for button in row]
    assert "menu:sources" in more
    assert "menu:help" in more


# ---------------------------------------------------------------- меню команд


def test_command_menu_and_help_cannot_diverge(container: Container) -> None:
    """Один список на синюю кнопку и на справку — второй разъехался бы."""
    from app.bot.handlers.help import help_text

    text = help_text(container)
    for name, _title in BOT_COMMANDS:
        assert f"/{name}" in text
    assert commands_help().startswith("КОМАНДЫ")


def test_commands_are_valid_for_telegram() -> None:
    """Telegram отвергает список целиком, если хоть одна запись не по формату."""
    for command in telegram_commands():
        assert command.command.islower()
        assert command.command.replace("_", "").isalnum()
        assert 1 <= len(command.command) <= 32
        assert 3 <= len(command.description) <= 256


def test_every_menu_command_has_a_handler() -> None:
    """Команда в синем меню, за которой ничего нет, — обещание без покрытия."""
    import re

    handlers = Path("app/bot/handlers")
    declared = set()
    for module in handlers.glob("*.py"):
        declared.update(re.findall(r'Command\("([a-z_]+)"\)', module.read_text()))
    declared.add("start")  # CommandStart()

    assert {name for name, _ in BOT_COMMANDS} <= declared


async def test_publish_commands_sets_the_blue_menu(
    bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Без этого вызова команды есть, но увидеть их негде."""
    from aiogram.methods import SetChatMenuButton, SetMyCommands

    from app.main import publish_commands

    calls: list[Any] = []

    async def capture(self: Bot, method: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", capture, raising=True)
    await publish_commands(bot)

    kinds = [type(call) for call in calls]
    assert SetMyCommands in kinds
    assert SetChatMenuButton in kinds
    published = next(call for call in calls if isinstance(call, SetMyCommands))
    assert [command.command for command in published.commands] == [name for name, _ in BOT_COMMANDS]


async def test_publish_commands_survives_a_refusal(
    bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Список команд — удобство, а приём сообщений — работа."""
    from aiogram.exceptions import TelegramBadRequest

    from app.main import publish_commands

    async def refuse(self: Bot, method: Any, *args: Any, **kwargs: Any) -> Any:
        raise TelegramBadRequest(method=method, message="nope")

    monkeypatch.setattr(Bot, "__call__", refuse, raising=True)

    await publish_commands(bot)  # не бросает — бот обязан запуститься
