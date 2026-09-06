"""Ретеншен удаляет то, что обещал удалить.

Эти тесты считают строки в ДОЧЕРНИХ таблицах, а не rowcount родителя. Дефект
жил именно потому, что проверялось «purged requests: 1», а рядом оставались
``search_results`` и ``debtor_reports`` с ФИО должников: SQLite не включает
внешние ключи сам, и объявленный в схеме ``ON DELETE CASCADE`` не исполнялся.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from sqlalchemy import delete, event, func, select, text

from app.config import Settings
from app.container import Container
from app.db.models import DebtorReportRow, SearchRequest, SearchResult
from app.db.repository import SearchRepository
from app.db.session import Database, _enable_sqlite_foreign_keys
from app.domain.enums import ProviderName, ProviderStatus, Region, SearchType
from app.domain.identity import SearchSubject, parse_fio
from app.services.retention import purge_once
from app.utils.dates import utcnow
from tests.conftest import provider_result

OPERATOR_ID = 111


def subject_for(fio: str) -> SearchSubject:
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio(fio),
        birth_date=date(1985, 3, 12),
        regions=(Region.MOSCOW.value,),
    )


async def count_rows(database: Database, model: type[object]) -> int:
    async with database.session() as session:
        return await session.scalar(select(func.count()).select_from(model)) or 0


async def age_every_request(database: Database, days: int) -> None:
    """Отодвинуть историю в прошлое, чтобы она попала под срок хранения."""
    async with database.session() as session:
        requests = await session.scalars(select(SearchRequest))
        for request in requests:
            request.created_at = utcnow() - timedelta(days=days)


# ---------------------------------------------------------------- каскад


async def test_purging_history_removes_provider_results_and_reports(
    container: Container, settings: Settings
) -> None:
    """Родитель без детей — это не удаление, а осиротевшие персональные данные."""
    await container.search_service.search(
        subject_for("Тестов Андрей Сергеевич"), telegram_user_id=OPERATOR_ID
    )
    database = container.database
    assert await count_rows(database, SearchResult) > 0
    assert await count_rows(database, DebtorReportRow) > 0

    await age_every_request(database, settings.history_retention_days + 1)
    requests, _links, _cards = await purge_once(settings, database)

    assert requests == 1
    assert await count_rows(database, SearchRequest) == 0
    assert await count_rows(database, SearchResult) == 0
    assert await count_rows(database, DebtorReportRow) == 0


async def test_fresh_history_keeps_its_children(container: Container, settings: Settings) -> None:
    """Обратная сторона: каскад не должен подметать то, что ещё в сроке."""
    await container.search_service.search(
        subject_for("Тестов Андрей Сергеевич"), telegram_user_id=OPERATOR_ID
    )
    database = container.database
    results_before = await count_rows(database, SearchResult)

    requests, _links, _cards = await purge_once(settings, database)

    assert requests == 0
    assert await count_rows(database, SearchRequest) == 1
    assert await count_rows(database, SearchResult) == results_before
    assert await count_rows(database, DebtorReportRow) == 1


async def test_foreign_keys_are_on_for_every_connection(tmp_path: Path) -> None:
    """Прагма живёт одно соединение, а пул открывает их заново."""
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'fk.db'}")
    await database.create_all()
    try:
        for _ in range(3):
            async with database.session() as session:
                assert await session.scalar(text("PRAGMA foreign_keys")) == 1
    finally:
        await database.dispose()


# ---------------------------------------------------------------- сироты


async def test_rows_orphaned_before_the_fix_are_swept(settings: Settings, tmp_path: Path) -> None:
    """У сироты нет родителя, значит под cutoff она не попадает никогда.

    Строки, оставленные прошлой версией, иначе пролежали бы в базе бессрочно —
    ровно те данные, которые ретеншен уже отчитался удалившим.
    """
    url = f"sqlite+aiosqlite:///{tmp_path / 'orphans.db'}"
    legacy = Database(url)
    # Тот самый прод до починки: внешние ключи выключены, каскад не работает.
    event.remove(legacy.engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
    await legacy.create_all()
    async with legacy.session() as session:
        repo = SearchRepository(session)
        request = await repo.create_request(
            telegram_user_id=OPERATOR_ID,
            search_type=SearchType.PERSON.value,
            normalized_query_hash="hash",
            masked_query="Т***",
            subject_json="{}",
        )
        request_id = request.id
        await repo.save_provider_results(
            request_id, [provider_result(ProviderName.FSSP, ProviderStatus.SUCCESS)]
        )
    async with legacy.session() as session:
        await session.execute(delete(SearchRequest).where(SearchRequest.id == request_id))
    await legacy.dispose()

    fixed = Database(url)
    try:
        assert await count_rows(fixed, SearchRequest) == 0
        assert await count_rows(fixed, SearchResult) > 0

        await purge_once(settings, fixed)

        assert await count_rows(fixed, SearchResult) == 0
    finally:
        await fixed.dispose()
