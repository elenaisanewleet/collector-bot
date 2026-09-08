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

import pytest
from aiogram import Bot, Dispatcher
from aiohttp.test_utils import TestClient, TestServer

from app.bot import card_view
from app.config import Settings
from app.container import Container
from app.db.repository import DebtorRepository, PhoneLookupRepository
from app.domain.enums import ProviderName, ProviderStatus
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import ProviderResult
from app.providers.phone_bridge import PhoneNameProvider, PhoneNameResult
from app.providers.registry import ProviderRegistry
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


async def test_the_documents_the_lookup_paid_for_reach_the_card(
    bridged: Container, bridged_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Номер телефона — и в карточке сразу вся личность, документами.

    Раньше здесь были только фамилия, имя, отчество и дата рождения: перенос
    был написан на четыре поля, а ответ приносил семь. Строк «Паспорт»,
    «Паспорт выдан» и «СНИЛС» карточка не показывала вовсе — и вопрос
    владелицы «почему паспорт не доезжает» был ровно про это.

    Целиком, а не масками: обещание «номер не сохраняю и сообщение удалю»
    владелица отменила, и вместе с ним отпало основание для маски. Заявление в
    суд подают с серией и номером, не с их тенью.
    """
    await feed(bridged_dispatcher, bot, message=make_message(PHONE))

    screen = last(sent)
    assert "Фамилия: Иванова" in screen
    assert "Дата рождения: 05.07.1985" in screen
    assert f"Паспорт: {PASSPORT}" in screen, "паспорт из ответа не доехал до карточки"
    assert "Паспорт выдан: 20.02.2015" in screen, "дата выдачи не доехала до карточки"
    assert f"СНИЛС: {SNILS}" in screen, "СНИЛС из ответа не доехал до карточки"


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
