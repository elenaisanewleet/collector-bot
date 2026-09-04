"""Shared fixtures.

Every test runs against an isolated in-memory database and an explicitly
constructed :class:`Settings`, so nothing depends on the developer's ``.env``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Sequence
from datetime import date
from pathlib import Path

import pytest
import pytest_asyncio

from app.config import AppMode, Settings
from app.container import Container
from app.db.session import Database
from app.domain.enums import ProviderName, ProviderStatus, Region, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import (
    BankruptcyRecord,
    BusinessRelation,
    EnforcementProceeding,
    FactRecord,
    ProviderResult,
)
from app.providers.registry import (
    ProviderRegistry,
    build_external_providers,
    build_internal_provider,
)
from app.services.import_service import ImportService
from app.services.scoring import RecoveryScoreEngine
from app.services.search import SearchService
from app.services.subject_store import SubjectStore

DEMO_CSV = Path("data/demo_debtors.csv")


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Demo-mode settings pointing at throwaway paths."""
    return Settings(
        app_mode=AppMode.DEMO,
        app_name="Test Bot",
        telegram_bot_token="test-token",
        allowed_telegram_user_ids="111,222",
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
    )
    instance = Container(
        settings=settings,
        database=database,
        registry=registry,
        search_service=SearchService(settings=settings, database=database, registry=registry),
        import_service=ImportService(settings, database),
        subject_store=SubjectStore(),
    )
    yield instance


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
    from app.domain.enums import BusinessRole, BusinessStatus

    record = BusinessRelation(
        inn="770912345601",
        name="ИП Тестов Андрей Сергеевич",
        role=BusinessRole.SOLE_PROPRIETOR if sole_proprietor else BusinessRole.DIRECTOR,
        status=BusinessStatus.ACTIVE if active else BusinessStatus.TERMINATED,
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
