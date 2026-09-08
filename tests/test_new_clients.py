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

from app.config import Settings
from app.container import Container
from app.db.repository import DebtorRepository, PhoneLookupRepository
from app.domain.enums import ProviderName, ProviderStatus
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import ProviderResult
from app.providers.phone_bridge import PhoneNameProvider, PhoneNameResult
from app.providers.registry import ProviderRegistry
from app.services.phone_lookups import PhoneLookupService
from app.services.query_card import Card, fill_from_bridge
from app.services.share import ShareKind, ShareLinkService, ShareTarget
from app.web.app import build_app

from .bot_harness import OPERATOR_ID, SentMessages, dispatcher_for, feed, make_message


def last(sent: SentMessages) -> str:
    return sent.texts[-1]


PUBLIC_URL = "https://reports.example.test"
PHONE = "+79851982945"

FOUND = PersonName(last_name="Клочкова", first_name="Елена", middle_name="Николаевна")
#: Контрольная сумма сходится — иначе :func:`normalize_snils` его отвергнет, и
#: тест проверял бы отказ вместо переноса.
SNILS = "16011086811"
PASSPORT = "4510123456"
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
            birth_date=date(1994, 11, 24),
            inn=INN,
            passport=PASSPORT,
            snils=SNILS,
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
    """Номер телефона — и в карточке появляются паспорт со СНИЛСом.

    Раньше здесь были только фамилия, имя, отчество и дата рождения: перенос
    был написан на четыре поля, а ответ приносил семь. Строки «Паспорт» и
    «СНИЛС» карточка не показывала вовсе — и вопрос владелицы «почему паспорт
    не доезжает» был ровно про это.

    Масками, а не номерами: карточка живёт в переписке, которую пересылают, и
    ради того же ``_set_passport`` удаляет сообщение оператора. Сами документы
    лежат на странице проверок — см. тест ниже.
    """
    await feed(bridged_dispatcher, bot, message=make_message(PHONE))

    screen = last(sent)
    assert "Фамилия: Клочкова" in screen
    assert "Дата рождения: 24.11.1994" in screen
    assert "Паспорт: 45** ******" in screen, "паспорт из ответа не доехал до карточки"
    assert "СНИЛС: ***-***-*** 11" in screen, "СНИЛС из ответа не доехал до карточки"
    assert PASSPORT not in screen, "паспорт целиком в переписке остаться не должен"
    assert SNILS not in screen


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
        birth_date=date(1994, 11, 24),
        inn=INN,
        passport=PASSPORT,
        snils=SNILS,
    )

    assert card.passport == "9999999999", "мост переписал паспорт оператора"
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
    assert row.last_name == "Клочкова"
    assert row.birth_date == date(1994, 11, 24)
    assert row.passport == PASSPORT, "при поднятом флаге документ пишется целиком"
    assert row.snils == SNILS
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
        "ИД,ФИО,Дата рождения\n440466,Клочкова Мария Ивановна,01.01.1970"
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
        "ИД,ФИО,Дата рождения\n440466,Клочкова Елена Николаевна,24.11.1994"
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
        "ИД,ФИО,Дата рождения\n440466,Клочковский Пётр Ильич,01.01.1970"
    )

    async with bridged.database.session() as session:
        found = await DebtorRepository(session).count_by_surname("Клочкова")

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
        birth_date=date(1994, 11, 24),
        inn=INN,
        passport=PASSPORT,
        snils=SNILS,
    )
    url = await bridged.share_service.issue(
        ShareTarget(ShareKind.LOOKUPS, 0), telegram_user_id=OPERATOR_ID
    )
    assert url is not None

    async with TestClient(TestServer(build_app(bridged))) as client:
        response = await client.get(_path(url))
        body = await response.text()

    assert response.status == 200
    assert "Клочкова Елена Николаевна" in body
    assert PASSPORT in body, "паспорт на странице обязан быть читаемым"
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
