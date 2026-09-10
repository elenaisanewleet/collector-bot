"""Личность, найденная по номеру: доезжает ли она до карточки, таблицы и веба.

Три утверждения, и каждое было дефектом.

*   **Оплаченный ответ не теряется.** Мост «телефон → ФИО» приносит одним
    обращением ФИО, дату рождения, ИНН, паспорт и СНИЛС. Переносились из него
    только имя с датой: паспорт и СНИЛС молча пропадали, и владелец шёл искать
    те же документы руками — то есть платил дважды за одно.
*   **Проверка не исчезает вместе с карточкой.** Карточка — черновик одного
    должника, её затирает следующий номер. Вопрос «кого я пробил и кого из них
    в базе нет» задаётся про всех сразу, и ответ на него обязан пережить
    следующего должника.
*   **«Новый клиент» — замер своего дня.** Отметка считается в момент проверки
    и не пересчитывается: следующий импорт выгрузки изменит ответ, и молча
    переписанное прошлое соврало бы о том, что владелец видел, когда решал.
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest
import respx
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer

from app.bot import card_view, common, view
from app.config import Settings
from app.container import Container
from app.db.repository import DebtorRepository, PhoneLookupRepository
from app.domain.enums import MissingInput, ProviderName, ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import InternalDebtorRecord, ProviderResult
from app.providers.base import ProviderUnavailableError
from app.providers.identity_bridge import (
    InnBridgeProvider,
    InnBridgeResult,
    missing_bridge_fields,
)
from app.providers.newdb import QUEUE_PATIENCE_POLLS, NewDBClient
from app.providers.phone_bridge import PhoneNameProvider, PhoneNameResult
from app.providers.registry import ProviderRegistry
from app.services import card_identify
from app.services.deeplink import CHECK_PAYLOAD_PREFIX, debtor_id_from_payload
from app.services.phone_lookups import PhoneLookupService
from app.services.query_card import Card, QueryCardService, fill_from_bridge
from app.services.share import ShareKind, ShareLinkService, ShareTarget
from app.web.app import build_app

from .bot_harness import (
    CHAT_ID,
    OPERATOR_ID,
    SentMessages,
    dispatcher_for,
    feed,
    make_message,
    urls,
)


def last(sent: SentMessages) -> str:
    return sent.texts[-1]


PUBLIC_URL = "https://reports.example.test"
PHONE = "+79990001122"

FOUND = PersonName(last_name="Иванова", first_name="Мария", middle_name="Сергеевна")
#: Контрольная сумма сходится — иначе :func:`normalize_snils` его отвергнет, и
#: тест проверял бы отказ вместо переноса.
SNILS = "16011086811"
PASSPORT = "4510123456"
ISSUED = date(2015, 2, 20)
INN = "770123456789"


class _BridgeStub(PhoneNameProvider):
    """Мост, отдающий полную личность без сети."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        return PhoneNameResult(
            provider=ProviderName.PHONE_BRIDGE,
            status=ProviderStatus.SUCCESS,
            records=(),
            name=FOUND,
            birth_date=date(1985, 7, 5),
            inn=INN,
            passport=PASSPORT,
            snils=SNILS,
            passport_issued=ISSUED,
            note="ФИО определено по номеру",
        )


@pytest.fixture
def bridged(container: Container) -> Container:
    """Тот же бот, но мост по телефону отвечает и хранение документов включено.

    ``store_sensitive_identifiers`` поднят намеренно: на проде он поднят, и
    именно в этом режиме страница показывает документы целиком. С выключенным
    флагом тест проверял бы другую конфигурацию, чем та, что работает.
    """
    settings = container.settings.model_copy(
        update={"web_public_url": PUBLIC_URL, "store_sensitive_identifiers": True}
    )
    container.settings = settings
    container.share_service = ShareLinkService(settings, container.database)
    container.phone_lookups = PhoneLookupService(settings, container.database)
    # Карточка тоже читает флаг — без пересборки она осталась бы со старыми
    # настройками, и тесты проверяли бы не ту конфигурацию.
    container.query_cards = QueryCardService(container.database, settings)
    container.registry = ProviderRegistry(
        internal=container.registry.internal,
        external=container.registry.external,
        inn_bridge=container.registry.inn_bridge,
        phone_bridge=_BridgeStub(settings),
    )
    return container


@pytest.fixture
def bridged_dispatcher(bridged: Container) -> Dispatcher:
    return dispatcher_for(bridged)


# ------------------------------------------------------------ 1. карточка


async def test_a_phone_alone_runs_the_check_and_shows_the_documents(
    bridged: Container, bridged_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Один номер — и сразу отчёт со ссылкой, без единого нажатия.

    Требование владелицы дословно: «ссылка на веб-отчёт должна появиться, то
    есть сразу же по данным должны запросы дальше идти». Автопрогон в боте был
    и раньше, но жил ТОЛЬКО в ветке «опознали по выгрузке»: он работал для тех,
    кто уже заведён, и молчал для новых — при том, что новых как раз и заводят.

    Документы при этом обязаны остаться на виду. Карточка при автопрогоне не
    рисуется вовсе, поэтому паспорт, дату выдачи и СНИЛС показывает строка
    «Принял» — она едет с сообщением о ходе проверки и остаётся в чате.
    """
    await feed(bridged_dispatcher, bot, message=make_message(PHONE))

    answer = sent.joined
    assert "Иванова Мария Сергеевна" in answer
    assert f"паспорт {PASSPORT}" in answer, "паспорт не доехал до ответа"
    assert "выдан 20.02.2015" in answer, "дата выдачи не доехала до ответа"
    assert f"СНИЛС {SNILS}" in answer, "СНИЛС не доехал до ответа"
    # Проверка прошла сама: кнопку «Проверить» никто не нажимал.
    assert "Перспектива взыскания" in answer, "прогон по номеру не дошёл до отчёта"
    assert any(url.startswith(PUBLIC_URL) for url in urls(sent)), "ссылки на веб-отчёт нет"


async def test_a_new_client_is_named_in_the_report_not_on_a_form(
    bridged: Container, bridged_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """«В базе такого нет» — новость про дело, и стоит она рядом с результатом.

    Раньше на это место приезжала форма сбора с фразой «По номеру телефона
    никого не нашёл. Попробуйте фамилию с именем или госномер» — совет сделать
    то, что бот только что сделал сам: фамилия уже стояла в карточке. Владелица
    про весь этот экран сказала «вообще непонятно, о чём он».
    """
    await feed(bridged_dispatcher, bot, message=make_message(PHONE))

    assert sent.contains("это новый клиент")
    assert not sent.contains("Попробуйте фамилию с именем")
    # И ни одной строки, объясняющей форму самой себе.
    assert not sent.contains("Прочерк — это")
    assert not sent.contains("Сейчас спрошу")
    assert not sent.contains("допишу сюда же")


async def test_the_documents_survive_a_restart(
    bridged: Container, bridged_dispatcher: Dispatcher, bot: Bot
) -> None:
    """Перезапуск бота не стирает оплаченное — ради этого хранение и включили.

    Дословно: «нам надо наоборот сохранять эти номера». Раньше документы жили
    час в памяти процесса и после рестарта карточка просила прислать их заново
    — а прислать их неоткуда, они добыты платным обращением. Это и значило
    платить дважды за одно.
    """
    await feed(bridged_dispatcher, bot, message=make_message(PHONE))
    # Перезапуск: новая служба, память процесса пуста, строка в базе на месте.
    bridged.query_cards = QueryCardService(bridged.database, bridged.settings)

    card = await bridged.query_cards.load(OPERATOR_ID, CHAT_ID)

    assert card.shown("passport") == PASSPORT
    assert card.shown("snils") == SNILS
    assert card.shown("passport_issued") == "20.02.2015"
    assert not card.forgotten("passport")


async def test_without_the_flag_a_restart_leaves_the_mask_not_a_dash(
    bridged: Container, bridged_dispatcher: Dispatcher, bot: Bot
) -> None:
    """Развёртывание может документы не хранить — и тогда разница обязана быть видна.

    «Было, но не сохранилось» и «не спрашивали» — это разные новости, и вторая
    заставила бы владельца заново платить за уже полученное, думая, что бот не
    искал. Поэтому маска остаётся рядом с номером даже там, где номера нет.
    """
    plain = bridged.settings.model_copy(update={"store_sensitive_identifiers": False})
    bridged.query_cards = QueryCardService(bridged.database, plain)

    await feed(bridged_dispatcher, bot, message=make_message(PHONE))
    bridged.query_cards = QueryCardService(bridged.database, plain)
    card = await bridged.query_cards.load(OPERATOR_ID, CHAT_ID)

    assert card.shown("passport") == "45** ******"
    assert card.shown("snils") == "***-***-*** 11"
    assert card.forgotten("passport")
    # Дата выдачи переживает и это: сама по себе она никого не опознаёт, и
    # прятать её не за чем.
    assert card.shown("passport_issued") == "20.02.2015"


def test_the_operator_beats_the_bridge() -> None:
    """Введённое человеком старше найденного мостом.

    Он держит документ в руках, а мост собирает личность из чужих находок,
    объединённых одним номером телефона, — а номером пользуются и родственники,
    и прежние владельцы номера.
    """
    card = Card(telegram_user_id=OPERATOR_ID, chat_id=OPERATOR_ID)
    card.passport = "9999999999"
    card.passport_masked = "99** ******"
    card.inn = "770912345601"
    card.birth_date = date(1980, 3, 15)

    fill_from_bridge(
        card,
        name=FOUND,
        birth_date=date(1985, 7, 5),
        inn=INN,
        passport=PASSPORT,
        snils=SNILS,
        passport_issued=ISSUED,
    )

    assert card.passport == "9999999999", "мост переписал паспорт оператора"
    assert card.shown("passport") == "9999999999"
    assert card.inn == "770912345601"
    assert card.birth_date == date(1980, 3, 15)
    # А пустое поле он заполняет: спор был только там, где спорить было о чем.
    assert card.snils == SNILS


# ------------------------------------------------------------ 2. таблица


async def test_the_lookup_is_written_down_and_marked_a_new_client(
    bridged: Container, bridged_dispatcher: Dispatcher, bot: Bot
) -> None:
    """Проверка по номеру оставляет строку, и она переживает следующего должника."""
    await feed(bridged_dispatcher, bot, message=make_message(PHONE))

    async with bridged.database.session() as session:
        rows = await PhoneLookupRepository(session).recent(limit=10)

    assert len(rows) == 1
    row = rows[0]
    assert row.last_name == "Иванова"
    assert row.birth_date == date(1985, 7, 5)
    assert row.passport == PASSPORT, "при поднятом флаге документ пишется целиком"
    assert row.snils == SNILS
    assert row.passport_issued == ISSUED
    assert row.passport_masked == "45** ******"
    assert row.base_matches == 0
    assert row.is_new_client, "в базе нет никого с такой фамилией — это новый клиент"
    # Телефон — только маской и без хэша: искать по нему в этой таблице некому.
    assert row.phone_masked and PHONE not in row.phone_masked


async def test_a_surname_already_in_the_base_is_not_a_new_client(bridged: Container) -> None:
    """Однофамилец в выгрузке снимает отметку: этого человека мы, возможно, знаем.

    По фамилии, а не по ФИО целиком: у женщин в выгрузке заказчика фамилия
    бывает девичьей, отчество пропущенным, а имя сокращённым до буквы. Считать
    точным совпадением значило бы объявлять новым каждого второго.
    """
    await bridged.import_service.import_text(
        "ИД,ФИО,Дата рождения\n440466,Иванова Ольга Ивановна,01.01.1970"
    )

    lookup = await bridged.phone_lookups.record(
        telegram_user_id=OPERATOR_ID, phone=PHONE, name=FOUND, snils=SNILS
    )

    assert lookup.base_matches == 1
    assert not lookup.is_new_client


async def test_the_mark_is_not_recomputed_later(bridged: Container) -> None:
    """Импорт после проверки не переписывает то, что владелец видел тогда."""
    first = await bridged.phone_lookups.record(
        telegram_user_id=OPERATOR_ID, phone=PHONE, name=FOUND
    )
    await bridged.import_service.import_text(
        "ИД,ФИО,Дата рождения\n440466,Иванова Мария Сергеевна,05.07.1985"
    )

    async with bridged.database.session() as session:
        stored = await PhoneLookupRepository(session).recent(limit=10)

    assert first.is_new_client
    assert stored[0].is_new_client, "отметка задним числом переписана"


async def test_a_lookup_does_not_count_a_debtor_with_another_surname(
    bridged: Container,
) -> None:
    """Совпадение по фамилии, а не по любой строке с этими буквами."""
    await bridged.import_service.import_text(
        "ИД,ФИО,Дата рождения\n440466,Ивановский Пётр Ильич,01.01.1970"
    )

    async with bridged.database.session() as session:
        found = await DebtorRepository(session).count_by_surname("Иванова")

    assert found == 0


# ------------------------------------------------------------ 3. веб


def _path(url: str) -> str:
    return "/" + url.split("/", maxsplit=3)[3]


async def test_the_new_clients_page_shows_the_documents_and_the_mark(
    bridged: Container,
) -> None:
    """Страница существует затем, чтобы из неё заполнить заявление.

    Поэтому паспорт и СНИЛС на ней стоят целиком, а не масками: она живёт за
    подписанной ссылкой с коротким сроком, выдаётся только владельцу и гасится
    ``/revoke`` — ровно как справочник должников, где ФИО тоже не вычёркиваются.
    Замаскированный документ превратил бы её в напоминание о том, что документ
    где-то есть.
    """
    await bridged.phone_lookups.record(
        telegram_user_id=OPERATOR_ID,
        phone=PHONE,
        name=FOUND,
        birth_date=date(1985, 7, 5),
        inn=INN,
        passport=PASSPORT,
        snils=SNILS,
        passport_issued=ISSUED,
    )
    url = await bridged.share_service.issue(
        ShareTarget(ShareKind.LOOKUPS, 0), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(bridged))) as client:
        response = await client.get(_path(url))
        body = await response.text()

    assert response.status == 200
    assert "Иванова Мария Сергеевна" in body
    assert PASSPORT in body, "паспорт на странице обязан быть читаемым"
    assert "20.02.2015" in body, "без даты выдачи паспорт в заявлении неполон"
    assert SNILS in body
    assert "Новый клиент" in body
    # Персональные данные не индексируются и не кэшируются — как и везде.
    assert "noindex" in response.headers["X-Robots-Tag"]
    assert "no-store" in response.headers["Cache-Control"]


async def test_a_base_token_does_not_open_the_new_clients_page(bridged: Container) -> None:
    """Ссылка на справочник не открывает журнал проверок и наоборот.

    Это разные списки с разным содержимым: в справочнике выгрузка заказчика, в
    журнале — личности, поднятые из чужого источника. Один токен на оба означал
    бы, что отзыв одной ссылки не гасит вторую.
    """
    base_url = await bridged.share_service.issue(
        ShareTarget(ShareKind.BASE, 0), telegram_user_id=OPERATOR_ID
    )
    assert base_url is not None
    token = base_url.rsplit("/", maxsplit=1)[-1]

    async with TestClient(TestServer(build_app(bridged))) as client:
        response = await client.get(f"/n/{token}")
        body = await response.text()

    assert response.status == 404
    assert "Ссылка недоступна" in body


async def test_an_empty_journal_says_what_will_appear_there(bridged: Container) -> None:
    """Пустая страница объясняет, чем она заполнится, а не молчит прочерком."""
    url = await bridged.share_service.issue(
        ShareTarget(ShareKind.LOOKUPS, 0), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(bridged))) as client:
        body = await (await client.get(_path(url))).text()

    assert "Отправьте боту номер" in body


# ------------------------------------------------------------ 4. почему не вышло


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (ProviderStatus.NO_RESULTS, card_view.NAME_NOT_FOUND),
        (ProviderStatus.UNAVAILABLE, card_view.NAME_SOURCE_SILENT),
        (ProviderStatus.ERROR, card_view.NAME_SOURCE_SILENT),
        (ProviderStatus.NOT_CONFIGURED, card_view.NAME_LOOKUP_OFF),
    ],
    ids=["никого нет", "источник молчит", "источник ошибся", "мост не настроен"],
)
async def test_three_reasons_for_no_name_are_three_different_answers(
    bridged: Container, bot: Bot, sent: SentMessages, status: ProviderStatus, expected: str
) -> None:
    """Почему имя не определилось — говорится словами, и слова разные.

    Раньше на все три случая была одна фраза «По номеру не определилось», с
    прямо записанным доводом: молчание источника и его незнание для оператора
    одно и то же действие. Довод верен, пока поиск по номеру — удобство. Он
    перестал быть верным, когда номер стал главным входом продукта:

    * упавший источник и честное «никого нет» неразличимы, и владелец идёт
      набирать фамилию руками там, где помог бы повтор через минуту;
    * ненастроенный мост выглядит как пустой ответ — и на живом боте это не
      диагностируется вовсе.
    """

    class _Silent(PhoneNameProvider):
        @property
        def is_configured(self) -> bool:
            return True

        async def _fetch(self, subject: SearchSubject) -> ProviderResult:
            return PhoneNameResult(
                provider=ProviderName.PHONE_BRIDGE, status=status, records=(), name=None
            )

    bridged.registry = ProviderRegistry(
        internal=bridged.registry.internal,
        external=bridged.registry.external,
        inn_bridge=bridged.registry.inn_bridge,
        phone_bridge=_Silent(bridged.settings),
    )
    await feed(dispatcher_for(bridged), bot, message=make_message(PHONE))

    assert sent.contains(expected)


def test_the_interface_examples_name_nobody_real() -> None:
    """Примеры на экране — вымышленные, и это проверяется, а не подразумевается.

    До 09.09.2026 в подсказках стояли настоящие ФИО и дата рождения владелицы,
    снятые с её же проверки: их видел каждый, кто открывал бота, и лежали они в
    публичном репозитории. Тест сторожит именно возврат — подставить в пример
    живого человека проще всего как раз тогда, когда его данные под рукой.
    """
    shown = " ".join(card_view.ASK_EXAMPLES.values())

    for real in ("Клочков", "24.11.1994", "9851982945"):
        assert real not in shown, f"в примерах интерфейса снова настоящие данные: {real}"


# ------------------------------------------- 5. ожидание и обещания про ИНН


def test_the_progress_message_keeps_moving_after_the_stages_run_out() -> None:
    """Стадии кончились — сообщение обязано остаться живым.

    Четыре стадии проходят за пять секунд, а один источник вправе думать до
    полутора минут. Раньше полоса застывала на «Считаю перспективу…» и не
    менялась ни разу до самого отчёта — владелица прочитала это ровно так, как
    оно выглядит: «ну и всё зависло».
    """
    frozen = view.searching(3, subject_name="Иванова Мария Сергеевна")
    alive = view.searching(3, subject_name="Иванова Мария Сергеевна", waited_seconds=47)

    assert "Считаю перспективу…" in frozen
    assert "47 с" in alive, "сообщение не говорит, сколько уже идёт"
    assert view.WAITING_NOTE in alive, "не сказано, почему ждём"
    assert alive != frozen, "сообщение не изменилось — Telegram не покажет движения"


class _NoInnBridge(PhoneNameProvider):
    """Мост по телефону, отдающий паспорт, но НЕ ИНН — как живой depsearch.

    Это не упрощение ради теста: на живом ответе ИНН физлица нет вовсе (в поле
    ``inn`` там лежит одиннадцатизначный СНИЛС), и весь смысл моста
    «паспорт → ИНН» именно в этом случае.
    """

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        return PhoneNameResult(
            provider=ProviderName.PHONE_BRIDGE,
            status=ProviderStatus.SUCCESS,
            records=(),
            name=FOUND,
            birth_date=date(1985, 7, 5),
            passport=PASSPORT,
            snils=SNILS,
            passport_issued=ISSUED,
        )


class _FnsBridge(InnBridgeProvider):
    """Мост «паспорт → ИНН», который настроен и готов идти. Без сети."""

    name = ProviderName.INN_BRIDGE

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def is_configured(self) -> bool:
        return True

    def missing_input_for(self, subject: SearchSubject) -> tuple[MissingInput, ...]:
        return missing_bridge_fields(subject)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        return InnBridgeResult(
            provider=ProviderName.INN_BRIDGE,
            status=ProviderStatus.SUCCESS,
            records=(),
            inn=INN,
        )


async def test_the_bot_does_not_promise_to_skip_what_the_bridge_will_unlock(
    bridged: Container, bot: Bot, sent: SentMessages
) -> None:
    """С паспортом на руках «не спрошу без ИНН» — ложь, и ровно в удачном случае.

    Оговорка считается ДО поиска, а ИНН добывается ВНУТРИ него, мостом
    «паспорт → ИНН». Бот обещал не спрашивать банкротство, ИП и арбитраж — и
    тут же их спрашивал. Вопрос владелицы был именно об этом: «надо же ИНН
    доставать, почему в ФНС нельзя получить ИНН». Можно; врала строка.

    Что вышло на самом деле, говорит блок ИСТОЧНИКИ в отчёте: он пишется по
    факту, а не по прогнозу.
    """
    bridged.registry = ProviderRegistry(
        internal=bridged.registry.internal,
        external=bridged.registry.external,
        inn_bridge=_FnsBridge(bridged.settings),
        phone_bridge=_NoInnBridge(bridged.settings),
    )
    await feed(dispatcher_for(bridged), bot, message=make_message(PHONE))

    answer = sent.joined
    assert "Без ИНН не спрошу" not in answer, "обещание не спрашивать дано при живом мосте"
    assert "нужен ИНН физлица" not in answer, "источники объявлены неспрошенными заранее"


def test_the_promise_stays_when_there_is_no_passport_to_bridge_with(
    container: Container,
) -> None:
    """А без паспорта оговорка обязана остаться: мост не пойдёт, и это правда."""
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=FOUND,
        birth_date=date(1985, 7, 5),
    )

    assert common._progress_note(subject, container) == common.NO_INN_NOTE


# --------------------------------------- 6. что уже лежит в выгрузке — не терять


def test_the_export_row_hands_over_its_inn_and_passport() -> None:
    """ИНН и паспорт из строки 1С подставляются в карточку.

    До 09.09.2026 не подставлялись — ``absorb`` знал про ФИО, дату рождения,
    договор, адрес, госномер и VIN, а про два самых дорогих поля не знал.

    Цена молчаливая и большая. По ИНН ищут банкротство, статус ИП и арбитраж;
    без него все трое отвечают «нужен ИНН физлица (12 цифр)». Паспорт открывает
    мост «паспорт → ИНН», то есть те же три источника через шаг. В выгрузке
    заказчика паспорт есть у большинства должников — значит три раздела отчёта
    молчали там, где всё для них лежало в нашей же строке.

    Владелица увидела это на своём должнике: ввела ФИО человека, который в
    выгрузке ЕСТЬ, и получила «ИНН не найден».
    """
    card = Card(telegram_user_id=OPERATOR_ID, chat_id=CHAT_ID)
    record = InternalDebtorRecord(
        source="internal",
        fio="Иванова Мария Сергеевна",
        birth_date=date(1985, 7, 5),
        inn=INN,
        passport=PASSPORT,
        passport_masked="45** ******",
    )

    filled = card_identify.absorb(card, record)

    assert card.inn == INN, "ИНН из выгрузки не доехал до карточки"
    assert card.passport == PASSPORT, "паспорт из выгрузки не доехал до карточки"
    assert card.passport_masked == "45** ******"
    # Названо вслух: молча подставленное поле неотличимо от угаданного.
    assert "ИНН" in filled
    assert "паспорт" in filled


def test_what_the_operator_typed_survives_the_export_row() -> None:
    """Введённое руками сильнее выгрузки — правило то же, что у моста.

    Оператор смотрит в договор, выгрузка — это вчерашний импорт. Расхождение
    между ними факт, а не опечатка, и прятать его нельзя.
    """
    card = Card(telegram_user_id=OPERATOR_ID, chat_id=CHAT_ID)
    card.inn = "770912345601"
    card.passport = "9999999999"
    card.passport_masked = "99** ******"
    record = InternalDebtorRecord(
        source="internal", fio="Иванова Мария Сергеевна", inn=INN, passport=PASSPORT
    )

    card_identify.absorb(card, record)

    assert card.inn == "770912345601"
    assert card.passport == "9999999999"


# ------------------------------------- 7. задача, за которую не взялись


@respx.mock
async def test_a_task_that_never_leaves_the_queue_is_dropped_early(
    live_settings: Settings,
) -> None:
    """Задача простояла в очереди полминуты — дальше ждать нечего.

    Снято с прода: пять источников из пяти отвечали ``queued`` девяносто секунд
    и не двигались с места, а оператор всё это время смотрел на полосу. Один
    прогон занимал шесть-семь минут и заканчивался пятью пустыми разделами.

    Задача, не начавшаяся за полминуты, не начнётся и за пять. Ждать её —
    тратить не деньги (вызов уже оплачен), а чужое время.
    """
    settings = live_settings.model_copy(
        update={
            "newdb_api_key": "k",
            "newdb_base_url": "https://newdb.example.test",
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 50,
            "newdb_poll_interval_seconds": 0.001,
            "newdb_queue_patience_polls": QUEUE_PATIENCE_POLLS,
            "provider_max_retries": 0,
        }
    )
    calls = respx.post("https://newdb.example.test/v2").mock(
        return_value=httpx.Response(200, json={"state": "queued", "requestId": "x"})
    )

    with pytest.raises(ProviderUnavailableError) as caught:
        await NewDBClient(settings).call("fssp_person", {"lastname": "Иванов"})

    assert caught.value.code == "never_started"
    # Сдались на десятом опросе, а не на пятидесятом: разница в пять раз и есть
    # то время, которое возвращается оператору.
    assert calls.call_count <= QUEUE_PATIENCE_POLLS + 1, "ждали дольше терпения очереди"


@respx.mock
async def test_a_task_that_started_working_gets_the_whole_budget(
    live_settings: Settings,
) -> None:
    """А начавшую работу задачу не обрывают: вызов уже оплачен.

    Терпение кончается только для тех, кто НЕ НАЧИНАЛСЯ. Дошедшая до
    ``in_progress`` получает полный бюджет — иначе ранний обрыв выбрасывал бы
    результат, за который заплачено.
    """
    settings = live_settings.model_copy(
        update={
            "newdb_api_key": "k",
            "newdb_base_url": "https://newdb.example.test",
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 15,
            "newdb_poll_interval_seconds": 0.001,
            "provider_max_retries": 0,
        }
    )
    respx.post("https://newdb.example.test/v2").mock(
        return_value=httpx.Response(200, json={"state": "in_progress", "requestId": "x"})
    )

    with pytest.raises(ProviderUnavailableError) as caught:
        await NewDBClient(settings).call("fssp_person", {"lastname": "Иванов"})

    assert caught.value.code == "poll_timeout", "работающую задачу оборвали как незапущенную"


# ------------------------------------- 8. отчёт по клику со страницы базы


def test_the_check_link_carries_the_debtor_and_nothing_else() -> None:
    """Полезная нагрузка ``?start=`` — префикс и число, больше в неё нельзя.

    Ограничение задал Telegram: не больше 64 символов и только
    ``[A-Za-z0-9_-]``. Ни ФИО, ни кириллицы, ни двоеточий туда не положить —
    и хорошо, что нельзя: ссылку видно в адресной строке.
    """
    assert debtor_id_from_payload(f"{CHECK_PAYLOAD_PREFIX}42") == 42
    # Всё, что не «префикс плюс цифры», отвергается молча: нагрузку пишет кто
    # угодно, и «почти похожее» здесь не значит ничего.
    for junk in ("", None, "42", "chk", "chkabc", "chk4 2", "other7"):
        assert debtor_id_from_payload(junk) is None, f"принята чужая нагрузка {junk!r}"


async def test_a_click_from_the_base_runs_the_check_in_the_bot(
    bridged: Container, bridged_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Кнопка со страницы должника доводит до отчёта, ничего не переспрашивая.

    Требование владелицы дословно: «при переходе во всю базу и нажатии на
    любого человека сразу формировался отчёт».

    Проверка идёт в БОТЕ, а не со страницы, и это про деньги: страница живёт
    за токеном, который пересылают, и не знает, кто её открыл. Кнопка, бьющая в
    источники прямо оттуда, означала бы, что каждый получатель ссылки тратит
    баланс владельца кликами.
    """
    await bridged.import_service.import_text(
        "ИД,ФИО,Дата рождения,Госномер\n440466,Иванова Мария Сергеевна,05.07.1985,А123ВС777"
    )
    async with bridged.database.session() as session:
        rows = await DebtorRepository(session).all_by_name(limit=1)
    assert rows, "выгрузка не загрузилась"

    await feed(
        bridged_dispatcher,
        bot,
        message=make_message(f"/start {CHECK_PAYLOAD_PREFIX}{rows[0].id}"),
    )

    answer = sent.joined
    assert "Иванова Мария Сергеевна" in answer
    # Отчёт, а не приветствие и не форма сбора: человек уже нажал «Проверить».
    assert "Перспектива взыскания" in answer, "клик со страницы не дошёл до отчёта"
    assert not sent.contains("С чего начнём"), "вместо отчёта показано приветствие"


async def test_an_unknown_debtor_falls_back_to_the_welcome(
    bridged: Container, bridged_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Ссылка устарела — человек попадает на первый экран, а не в тупик.

    Ссылки живут в переписках и переживают чистку истории: должника могли
    удалить, базу переимпортировать. Сообщение об ошибке вместо приветствия —
    плохая встреча там, где всё, что нужно, это начать заново.
    """
    await feed(
        bridged_dispatcher, bot, message=make_message(f"/start {CHECK_PAYLOAD_PREFIX}999999")
    )

    assert not sent.contains("Перспектива взыскания")
    assert sent.joined, "бот не ответил вовсе"


def test_the_person_page_has_no_button_when_the_bot_is_unknown(bridged: Container) -> None:
    """Имя бота неизвестно — кнопки нет. Мёртвая ссылка хуже отсутствующей.

    Так бывает, когда веб поднят без бота: Telegram никто не спрашивал, и
    подставлять в ссылку пустое имя значило бы вести в никуда.
    """
    from app.web.app import _check_url

    assert bridged.bot_username == ""
    assert _check_url(bridged, 42) == ""

    bridged.bot_username = "proverka_dolga_bot"
    assert _check_url(bridged, 42) == (
        f"https://t.me/proverka_dolga_bot?start={CHECK_PAYLOAD_PREFIX}42"
    )
