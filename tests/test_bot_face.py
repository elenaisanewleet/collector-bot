"""Лицо бота: приветствие с баннером, экран «Откуда данные», меню команд.

Проверяется не вёрстка, а обещания. Приветствие не должно обещать того, чего бот
не делает; экран источников не должен показывать подключённым то, что не
подключено; картинка не должна уносить с собой ``/start``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from aiogram import Bot, Dispatcher

from app.bot import banner
from app.bot.commands import BOT_COMMANDS, commands_help, telegram_commands
from app.bot.handlers.start import WELCOME_STEPS, welcome_text
from app.bot.sources import sources_screen
from app.container import Container
from app.utils.formatting import split_message

from .bot_harness import (
    BANNER_FILE_ID,
    SentMessages,
    dispatcher_for,
    feed,
    make_callback,
    make_message,
)


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
    """Подпись длиннее лимита Telegram отвергает всё сообщение целиком.

    Считается вместе со строкой базы: она приезжает в ту же подпись и несёт
    полный адрес с токеном в тридцать два байта. Перебрать лимит — значит не
    показать заказчику приветствие вовсе.
    """
    from decimal import Decimal

    from app.bot.handlers.start import base_html
    from app.bot.keyboards import BaseListing

    assert len(welcome_text(container)) <= banner.CAPTION_LIMIT

    base = BaseListing(
        url="https://proverka-dolga.shop/b/" + "T" * 43,
        total=2052,
        amount=Decimal("13679650"),
    )
    assert len(welcome_text(container, base=base_html(base))) <= banner.CAPTION_LIMIT


async def test_start_sends_the_banner_with_the_menu(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages, container: Container
) -> None:
    await feed(dispatcher, bot, message=make_message("/start"))

    assert len(sent.photos) == 1
    path, caption = sent.photos[0]
    assert path.endswith("welcome.jpg")
    assert caption is not None
    # Названия приложения в приветствии нет: «Collector Bot» латиницей над
    # русским текстом ничего не сообщает — имя бота Telegram печатает в шапке
    # чата сам.
    assert container.settings.app_name not in caption
    assert "стоит ли подавать и платить пошлину" in caption
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
    assert sent.contains("стоит ли подавать и платить пошлину")
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

    assert sent.contains("стоит ли подавать и платить пошлину")


# ---------------------------------------------------------------- приветствие


def test_welcome_promises_only_what_the_bot_does(container: Container) -> None:
    text = welcome_text(container).lower()
    for overclaim in ("узнай всё", "по номеру телефона", "счета", "имущество"):
        assert overclaim not in text


def test_the_welcome_is_one_phrase(container: Container) -> None:
    """Первый экран — одна фраза, и это требование владелицы, повторённое много раз.

    Что делать, экран не объясняет намеренно: под полем ввода стоят две кнопки, а
    в самом поле — подсказка «Напишите номер телефона должника». Инструкция
    словами поверх этого была третьим объяснением одного и того же.

    Оговорка «не проверено ≠ чисто» отсюда ушла, но из продукта не делась: она
    стоит на карточке под каждым прочерком, в отчёте и в справке — там, где
    человек читает её по делу, а не до первого своего действия.

    Из чего собран ответ, первый экран не рекламирует: ни «реестров», ни базы по
    имени. Это показывает экран «Откуда данные» — тому, кто спросил.
    """
    from app.config import AppMode

    live = container.settings.model_copy(update={"app_mode": AppMode.LIVE})
    text = welcome_text(replace(container, settings=live))

    assert text.count(".") == 1, f"фраз больше одной: {text}"
    assert "\n" not in text
    assert "реестр" not in text.lower()
    assert "1С" not in text and "1с" not in text
    # Ненавязчиво — значит без пиктограмм в тексте.
    assert not any(mark in text for mark in ("1️⃣", "2️⃣", "3️⃣", "⚠️"))


def test_the_demo_note_is_the_only_thing_allowed_to_join_it(container: Container) -> None:
    """В демо-режиме к фразе добавляется предупреждение, и только оно.

    Демо-стенд показывают заказчику, и «данные вымышленные» он обязан прочитать
    до того, как поверит первому же отчёту.
    """
    text = welcome_text(container)

    assert text.startswith(WELCOME_STEPS)
    assert "Демо-режим" in text


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
    """Границы названы тем, что не меняется, а не текущей настройкой.

    Раньше здесь стояло «ЕГРН не подключён» — правда ровно до дня, когда его
    подключат, после чего экран начинает врать в обратную сторону. Настоящая
    граница другая и вечная: правообладателя ЕГРН не называет никому. Метод
    подключается, «что принадлежит должнику» — нет.
    """
    from app.bot.sources import LIMITS

    screen = sources_screen(container, debtors=0)
    assert "не найдёт незнакомого человека по номеру телефона" in screen
    assert "не покажет банковские счета" in screen
    assert "правообладателя ЕГРН не называет" in screen
    # Проверяется именно блок границ, а не весь экран: строкой ниже каждый
    # источник честно пишет своё состояние, и «не подключено» там уместно —
    # это про сегодняшнюю настройку, а не про закон.
    assert "не подключён" not in LIMITS, "граница описана настройкой, а не законом"


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
    """«Новая проверка» под отчётом ведёт к чистой карточке следующего должника.

    Раньше она висела часиками до таймаута, потом показывала меню — и через
    меню оператор попадал в карточку ПРЕДЫДУЩЕГО человека. Одноимённая кнопка
    на самой карточке при этом чистила. Одна подпись обязана значить одно.
    """
    await feed(dispatcher, bot, callback_query=make_callback("menu:back"))

    # Чистая карточка узнаётся по первому вопросу, а он спрашивает телефон.
    assert sent.contains("✎ Номер или ФИО")
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
    """Справочные экраны достижимы кнопкой, но не первым рядом.

    В главном меню было одиннадцать кнопок, и это назвали кучей. Потом семь в
    столбик — тоже куча: столбик из семи читается как список всего, что бот
    умеет, а меню должно быть списком того, зачем сюда пришли.

    Рядов теперь пять, и парами стоят кнопки, которые и по смыслу пара: обе
    про базу целиком, обе про «найти уже сделанное или найти иначе», обе про
    «объясни».
    """
    from app.bot.keyboards import MENU_MORE, main_menu, more_menu

    rows = main_menu(owner=True).inline_keyboard
    main = [button.callback_data for row in rows for button in row]

    # Первый ряд — один, и это то, что нажимают каждый день.
    assert [b.callback_data for b in rows[0]] == ["menu:person"]
    assert len(rows) <= 5, "меню снова растёт в столбик"
    assert "batch:start" in main
    assert MENU_MORE in main
    assert "menu:sources" in main
    assert "menu:help" in main

    # За «Другими способами» — только способы поиска: справка и история
    # переехали в само меню, и держать их в двух местах значило бы иметь по две
    # кнопки на каждое действие.
    more = [button.callback_data for row in more_menu(owner=True).inline_keyboard for button in row]
    assert "menu:contract" in more
    assert "menu:sources" not in more


def test_the_menu_carries_the_link_to_the_base() -> None:
    """Ссылка на весь список — вторым рядом, кнопкой, а не текстом.

    «Где ссылка на базу?» — вопрос владелицы после выкладки: на приветствии
    она есть, но приветствие пролистывают, а в меню возвращаются. Здесь ссылка
    может быть настоящей кнопкой: меню — инлайн-сообщение, нижняя клавиатура с
    ним не спорит.
    """
    from decimal import Decimal

    from app.bot.keyboards import BaseListing, main_menu

    base = BaseListing(url="https://example.test/b/tok", total=2052, amount=Decimal("13679650"))
    rows = main_menu(owner=True, base=base).inline_keyboard

    button = rows[1][0]
    assert button.url == "https://example.test/b/tok"
    assert button.text == "Вся база — 2052 должника, 13,7 млн ₽"
    # И без ссылки меню обязано собираться: база бывает пустой, веб — выключен.
    assert all(b.url is None for row in main_menu(owner=True).inline_keyboard for b in row)


def test_the_expensive_buttons_are_not_shown_to_a_plain_operator() -> None:
    """Кнопка, отвечающая «нельзя», — обещание, которого бот не держит.

    Прогон по всей базе и загрузка выгрузки закрыты владельцу; меню обязано это
    повторять, иначе сотрудник жмёт первую же кнопку и получает отказ.
    """
    from app.bot.keyboards import main_menu, more_menu

    main = [
        button.callback_data for row in main_menu(owner=False).inline_keyboard for button in row
    ]
    assert "batch:start" not in main
    assert "menu:person" in main

    assert "menu:history" in main

    more = [
        button.callback_data for row in more_menu(owner=False).inline_keyboard for button in row
    ]
    assert "menu:import" not in more


# ---------------------------------------------------------------- меню команд


def test_command_menu_and_help_cannot_diverge(container: Container) -> None:
    """Один список на синюю кнопку и на справку — второй разъехался бы."""
    from app.bot.handlers.help import help_text

    text = help_text(container, owner=True)
    for name, _title in BOT_COMMANDS:
        assert f"/{name}" in text
    assert commands_help(owner=True).startswith("КОМАНДЫ")


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


async def test_publish_sets_the_description_the_user_sees_before_start(
    bot: Bot, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Описание бота ставит код, а не человек в @BotFather.

    Это экран, который человек видит ДО кнопки «Начать», не написав боту ни
    слова, — и он единственный жил на стороне Telegram, а не в репозитории.
    Разошлось немедленно: пока из текстов бота убирали перечисление источников,
    описание продолжало обещать «проверю по официальным источникам» и называть
    их поимённо. Выкладка такое не чинит — код должен ставить описание сам.
    """
    from aiogram.methods import SetMyDescription, SetMyShortDescription

    from app.bot.commands import BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION
    from app.main import publish_commands

    calls: list[Any] = []

    async def capture(self: Bot, method: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(method)
        return True

    monkeypatch.setattr(Bot, "__call__", capture, raising=True)
    await publish_commands(bot)

    described = next(call for call in calls if isinstance(call, SetMyDescription))
    assert described.description == BOT_DESCRIPTION
    short = next(call for call in calls if isinstance(call, SetMyShortDescription))
    assert short.short_description == BOT_SHORT_DESCRIPTION


def test_the_description_keeps_the_same_rules_as_the_first_screen() -> None:
    """То же правило, что и на первом экране: из чего собран ответ — не реклама.

    Плюс лимиты Telegram: описание длиннее 512 знаков он отвергает целиком, и
    бот остаётся с прежним текстом, ничего не сказав.
    """
    from app.bot.commands import BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION

    for text in (BOT_DESCRIPTION, BOT_SHORT_DESCRIPTION):
        assert "реестр" not in text.lower()
        assert "источник" not in text.lower()
        assert "1С" not in text and "1с" not in text
    assert len(BOT_DESCRIPTION) <= 512
    assert len(BOT_SHORT_DESCRIPTION) <= 120
    # Одна фраза: экран видит тот, кто ещё не нажал «Начать». Оговорка про
    # «не проверено» ждёт его на первом экране после старта, где она к месту, —
    # здесь она была бы абзацем в витрине.
    assert BOT_DESCRIPTION.count(".") == 1
    assert "\n" not in BOT_DESCRIPTION


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


# ------------------------------------------------- ссылка на базу в приветствии


def _with_web(container: Container, *, owner: bool) -> Container:
    """Тот же бот, но с включёнными веб-ссылками — и с владельцем или без.

    Без публичного адреса ``share_service`` молчит, и проверка «незнакомцу
    ссылку не дали» прошла бы сама собой, ничего не проверив: ссылки не было
    бы ни у кого. Здесь она есть, и отказ незнакомцу — настоящий.
    """
    from app.config import NO_OWNERS
    from app.services.access import AccessService
    from app.services.share import ShareLinkService

    update: dict[str, object] = {"web_public_url": "https://example.test"}
    if not owner:
        update["owner_telegram_user_ids"] = NO_OWNERS
    settings = container.settings.model_copy(update=update)
    return replace(
        container,
        settings=settings,
        share_service=ShareLinkService(settings, container.database),
        access_service=AccessService(settings, container.database),
    )


async def _seed_debtors(container: Container, amounts: list[str]) -> None:
    from decimal import Decimal

    from app.db.models import Debtor
    from app.db.repository import DebtorRepository

    async with container.database.session() as session:
        repo = DebtorRepository(session)
        for index, amount in enumerate(amounts):
            await repo.upsert(
                Debtor(
                    dedup_key=f"seed-{index}",
                    fio=f"Иванов Иван Иванович {index}",
                    debt_amount=Decimal(amount) if amount else None,
                )
            )


def _welcome_of(sent: SentMessages) -> str:
    """Текст приветствия — из подписи к баннеру или из обычного сообщения."""
    return " ".join(sent.texts + [caption or "" for _, caption in sent.photos])


async def test_the_welcome_carries_the_link_to_the_whole_base(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Первая строка после фразы — ссылка на весь список с числом и суммой.

    Требование владелицы дословно: «заказчику ссылку на базу сразу». Половина
    её вопросов к боту — не «проверь этого», а «кто у меня вообще есть», и
    сегодня за этим лезут в 1С.

    Ссылка живёт текстом, а не кнопкой, потому что у сообщения бывает либо
    инлайн-клавиатура, либо нижняя, а приветствие несёт нижнюю — те самые две
    кнопки. Отдельным сообщением её уже пробовали слать: кнопок тогда не
    увидел никто.
    """
    owned = _with_web(container, owner=True)
    await _seed_debtors(owned, ["5000000", "8679650", ""])

    await feed(dispatcher_for(owned), bot, message=make_message("/start"))

    text = _welcome_of(sent)
    assert "Вся база" in text
    assert "3 должника" in text
    # Сумма — коротко и без вранья: третий должник без суммы в неё не входит.
    assert "13,7 млн ₽" in text
    assert 'href="' in text, "ссылка обязана быть кликабельной, а не голым адресом"


async def test_a_stranger_does_not_get_the_link_to_the_base(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Не владелец получает одну фразу — и ни одной ссылки на чужие данные.

    За ссылкой имена, адреса и суммы всей базы, а ``ALLOWED_TELEGRAM_USER_IDS``
    на проде стоит в ``*``: в бота может написать кто угодно. Ссылка на первом
    экране для всех — это отдача базы первому нажавшему «Start».
    """
    stranger = _with_web(container, owner=False)
    await _seed_debtors(stranger, ["5000000"])

    await feed(dispatcher_for(stranger), bot, message=make_message("/start"))

    text = _welcome_of(sent)
    assert "стоит ли подавать" in text
    assert "Вся база" not in text
    assert "http" not in text


async def test_an_empty_base_does_not_offer_a_link_to_itself(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Пока выгрузку не загрузили, ссылки нет: открывать нечего.

    «Вся база — 0 должников» — это приглашение на пустую страницу и повод
    решить, что бот сломан.
    """
    empty = _with_web(container, owner=True)

    await feed(dispatcher_for(empty), bot, message=make_message("/start"))

    text = _welcome_of(sent)
    assert "Вся база" not in text
    assert "стоит ли подавать" in text


async def test_every_main_menu_carries_the_same_link(
    bot: Bot, sent: SentMessages, container: Container
) -> None:
    """Меню собирается в одном месте, а не по-разному в каждом обработчике.

    Ссылка на базу появлялась только на /start и по кнопке «Главное меню», а
    после справки, после импорта и после отмены — нет. Заказчик видел её то
    там, то нет и решал, что она пропала.

    Единственное исключение — меню под «в базе никого нет»: там открывать по
    ссылке нечего, и это сказано в коде прямо.
    """
    owned = _with_web(container, owner=True)
    await _seed_debtors(owned, ["5000000"])
    dispatcher = dispatcher_for(owned)

    for opening in ("Главное меню", "Как это работает", "Откуда данные"):
        sent.markups.clear()
        await feed(dispatcher, bot, message=make_message(opening))

        urls = [
            button.url
            for markup in sent.markups
            if markup is not None and getattr(markup, "inline_keyboard", None)
            for row in markup.inline_keyboard
            for button in row
            if button.url
        ]
        assert urls, f"после «{opening}» меню приехало без ссылки на базу"


def test_the_phone_limit_disappears_when_the_bridge_is_wired(container: Container) -> None:
    """Строка про телефон зависит от настройки, а не от памяти правившего.

    «Не найдёт незнакомого человека по номеру телефона» — правда, пока мост
    «телефон → ФИО» не подключён. Подключат — перестанет быть правдой в ту же
    минуту, а экран, обещающий уже неверное, хуже отсутствующего. Поэтому
    строка не константа, а следствие ``phone_bridge_configured``.
    """
    from dataclasses import replace as replace_container

    from app.bot.sources import sources_screen

    assert "не найдёт незнакомого человека по номеру телефона" in sources_screen(
        container, debtors=0
    )

    wired = container.settings.model_copy(
        update={
            "phone_bridge_enabled": True,
            "phone_bridge_base_url": "https://example.test",
            "phone_bridge_path": "/lookup/{phone}",
            "phone_bridge_field_map": Path("config/field_maps/example_phone_bridge.json"),
        }
    )
    screen = sources_screen(replace_container(container, settings=wired), debtors=0)

    assert "не найдёт незнакомого человека по номеру телефона" not in screen
    # Остальные границы на месте: они от настроек не зависят.
    assert "не покажет банковские счета" in screen
