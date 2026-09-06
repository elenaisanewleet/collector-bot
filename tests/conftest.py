"""Shared fixtures.

Every test runs against an isolated in-memory database and an explicitly
constructed :class:`Settings`, so nothing depends on the developer's ``.env``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties

from app.config import NO_OWNERS, AppMode, Settings
from app.container import Container
from app.db.session import Database
from app.domain.enums import ProviderName, ProviderStatus, Region, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import (
    BankruptcyRecord,
    BusinessRelation,
    CourtCase,
    EnforcementProceeding,
    FactRecord,
    InternalDebtorRecord,
    PledgeRecord,
    ProviderResult,
)
from app.providers.registry import (
    ProviderRegistry,
    build_external_providers,
    build_inn_bridge,
    build_internal_provider,
)
from app.services.access import AccessService
from app.services.batch import BatchService
from app.services.import_service import ImportService
from app.services.query_card import QueryCardService
from app.services.scoring import RecoveryScoreEngine
from app.services.search import SearchService
from app.services.share import ShareLinkService
from app.services.subject_store import SubjectStore
from app.services.verdict import VerdictEngine

from .bot_harness import FAKE_TOKEN, SentMessages, dispatcher_for, intercept

DEMO_CSV = Path("data/demo_debtors.csv")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Demo-mode settings pointing at throwaway paths."""
    return Settings(
        app_mode=AppMode.DEMO,
        app_name="Test Bot",
        telegram_bot_token="test-token",
        allowed_telegram_user_ids="111,222",
        # 111 — не просто допущенный, а владелец: прогон по всей базе, импорт и
        # выгрузка очереди открыты только владельцу, и стенд обязан изображать
        # того, кто ими пользуется. Кому этого не надо — ``unowned_container``.
        owner_telegram_user_ids="111",
        database_url="sqlite+aiosqlite:///:memory:",
        internal_csv_path=DEMO_CSV,
        cache_ttl_hours=24,
        request_timeout_seconds=5,
        provider_max_retries=0,
        provider_retry_backoff_seconds=0.0,
        log_level="CRITICAL",
        _env_file=None,
    )


@pytest.fixture
def live_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"app_mode": AppMode.LIVE})


@pytest_asyncio.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    # A single shared in-memory connection: SQLite gives each connection its own
    # database otherwise, and the schema would vanish between statements.
    db = Database("sqlite+aiosqlite:///:memory:")
    await db.create_all()
    try:
        yield db
    finally:
        await db.dispose()


@pytest_asyncio.fixture
async def container(settings: Settings, database: Database) -> AsyncIterator[Container]:
    registry = ProviderRegistry(
        internal=build_internal_provider(settings, database),
        external=build_external_providers(settings),
        inn_bridge=build_inn_bridge(settings),
    )
    search_service = SearchService(settings=settings, database=database, registry=registry)
    instance = Container(
        settings=settings,
        database=database,
        registry=registry,
        search_service=search_service,
        import_service=ImportService(settings, database),
        batch_service=BatchService(
            settings=settings, database=database, search_service=search_service
        ),
        verdict_engine=VerdictEngine(settings),
        share_service=ShareLinkService(settings, database),
        subject_store=SubjectStore(),
        access_service=AccessService(settings, database),
        query_cards=QueryCardService(database),
    )
    yield instance


@pytest.fixture
def unowned_container(container: Container) -> Container:
    """Тот же бот, но владельцев у него нет вовсе.

    Нужен там, где проверяется поведение бота без владельца: незнакомец упирается
    в стену вместо заявки, ``/access`` объясняет, что режим не настроен. Пустая
    строка для этого не годится — она означает «настройку не заполнили» и
    отдаёт владельцев по умолчанию (см. :data:`app.config.NO_OWNERS`).
    """
    settings = container.settings.model_copy(update={"owner_telegram_user_ids": NO_OWNERS})
    return replace(
        container, settings=settings, access_service=AccessService(settings, container.database)
    )


@pytest.fixture
def unowned_dispatcher(unowned_container: Container) -> Dispatcher:
    return dispatcher_for(unowned_container)


@pytest.fixture
def score_engine() -> RecoveryScoreEngine:
    return RecoveryScoreEngine()


@pytest.fixture
def person_subject() -> SearchSubject:
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
        birth_date=date(1985, 3, 12),
        regions=(Region.MOSCOW.value,),
    )


@pytest.fixture
def nameless_subject() -> SearchSubject:
    return SearchSubject(search_type=SearchType.CONTRACT.value, contract_number="EV-1")


def make_proceeding(
    number: str = "1/26/77001-ИП",
    *,
    amount: str = "10000",
    name: str | None = "Тестов Андрей Сергеевич",
    birth_date: date | None = date(1985, 3, 12),
    confidence: float = 1.0,
    active: bool = True,
) -> EnforcementProceeding:
    from decimal import Decimal

    from app.domain.enums import ProceedingStatus

    record = EnforcementProceeding(
        proceeding_number=number,
        debtor_name=name,
        debtor_birth_date=birth_date,
        amount=Decimal(amount),
        status=ProceedingStatus.ACTIVE if active else ProceedingStatus.CLOSED,
    )
    record.match_confidence = confidence
    return record


def make_bankruptcy(*, active: bool = True, confidence: float = 1.0) -> BankruptcyRecord:
    from app.domain.enums import BankruptcyStatus

    record = BankruptcyRecord(
        debtor_name="Тестов Андрей Сергеевич",
        case_number="А40-1/2026",
        procedure="Реализация имущества гражданина",
        status=BankruptcyStatus.ACTIVE if active else BankruptcyStatus.COMPLETED,
        completed_at=None if active else date(2025, 1, 1),
    )
    record.match_confidence = confidence
    return record


def make_business(
    *,
    active: bool = True,
    sole_proprietor: bool = True,
    confidence: float = 1.0,
) -> BusinessRelation:
    from app.domain.enums import BusinessRole, BusinessStatus, EntityType

    record = BusinessRelation(
        inn="770912345601" if sole_proprietor else "5038123456",
        name="ИП Тестов Андрей Сергеевич" if sole_proprietor else 'ООО "Демонстрационные решения"',
        # Тип сущности здесь не украшение: от него зависит, сопоставляется ли
        # запись с человеком по ФИО и ИНН. ИП — это сам должник, юрлицо — нет.
        entity_type=EntityType.SOLE_PROPRIETOR if sole_proprietor else EntityType.LEGAL_ENTITY,
        role=BusinessRole.SOLE_PROPRIETOR if sole_proprietor else BusinessRole.DIRECTOR,
        status=BusinessStatus.ACTIVE if active else BusinessStatus.TERMINATED,
        linked_by_identifier=not sole_proprietor,
    )
    record.match_confidence = confidence
    return record


def make_pledge(
    *,
    active: bool = True,
    vin: str | None = "XTA1234567890ABCD",
    confidence: float = 1.0,
) -> PledgeRecord:
    from app.domain.enums import PledgeStatus

    record = PledgeRecord(
        registration_number="2022-006-123456-789",
        registered_at=date(2022, 4, 11),
        terminated_at=None if active else date(2025, 6, 1),
        pledgor_name="Тестов Андрей Сергеевич",
        pledgor_birth_date=date(1985, 3, 12),
        pledgee_name='АО "Демонстрационный банк"',
        subject="Автомобиль LADA VESTA, 2021",
        vin=vin,
        status=PledgeStatus.ACTIVE if active else PledgeStatus.TERMINATED,
    )
    record.match_confidence = confidence
    return record


def make_court_case(
    case_number: str = "А40-227414/2026",
    *,
    defendant: bool = True,
    closed: bool = False,
    amount: str | None = "1180400",
    confidence: float = 1.0,
) -> CourtCase:
    from decimal import Decimal

    from app.domain.enums import CourtCaseRole

    record = CourtCase(
        case_number=case_number,
        court_name="Арбитражный суд города Москвы",
        amount=Decimal(amount) if amount is not None else None,
        filed_at=date(2026, 5, 20),
        participant_name="Тестов Андрей Сергеевич",
        inn="770912345601",
        role=CourtCaseRole.DEFENDANT if defendant else CourtCaseRole.PLAINTIFF,
        is_closed=closed,
    )
    record.match_confidence = confidence
    return record


def make_internal(
    *,
    debt: str | None = "38400",
    full_name: str = "Тестов Андрей Сергеевич",
    confidence: float = 1.0,
) -> InternalDebtorRecord:
    from decimal import Decimal

    record = InternalDebtorRecord(
        debtor_id="DEM-001",
        full_name=full_name,
        birth_date=date(1985, 3, 12),
        contract_number="EV-20481",
        debt_amount=Decimal(debt) if debt is not None else None,
    )
    record.match_confidence = confidence
    return record


def provider_result(
    provider: ProviderName,
    status: ProviderStatus,
    records: Sequence[FactRecord] | None = None,
) -> ProviderResult:
    return ProviderResult(
        provider=provider,
        status=status,
        records=list(records or []),
    )


@pytest.fixture
def anyio_backend() -> Iterator[str]:
    yield "asyncio"


# ---------------------------------------------------------------- bot harness


@pytest.fixture
def sent() -> SentMessages:
    return SentMessages()


@pytest.fixture
def bot(sent: SentMessages, monkeypatch: pytest.MonkeyPatch) -> Iterator[Bot]:
    """A Bot whose outbound calls are intercepted instead of sent."""
    instance = Bot(token=FAKE_TOKEN, default=DefaultBotProperties(parse_mode=None))
    monkeypatch.setattr(Bot, "__call__", intercept(sent), raising=True)
    yield instance


@pytest.fixture
def dispatcher(container: Container) -> Dispatcher:
    return dispatcher_for(container)
