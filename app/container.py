"""Composition root.

Everything is constructed here, once, and handed to the handlers by the
dependency middleware. Nothing below this file reaches for a global.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.db.session import Database
from app.providers.registry import ProviderRegistry, build_registry
from app.services.identity import IdentityMatcher
from app.services.import_service import ImportService
from app.services.scoring import RecoveryScoreEngine
from app.services.search import SearchService
from app.services.subject_store import SubjectStore


@dataclass(slots=True)
class Container:
    """Application services, wired and ready."""

    settings: Settings
    database: Database
    registry: ProviderRegistry
    search_service: SearchService
    import_service: ImportService
    subject_store: SubjectStore

    async def dispose(self) -> None:
        await self.database.dispose()


def build_container(settings: Settings | None = None) -> Container:
    resolved = settings or get_settings()
    database = Database(resolved.database_url)
    registry = build_registry(resolved, database)
    search_service = SearchService(
        settings=resolved,
        database=database,
        registry=registry,
        matcher=IdentityMatcher(),
        score_engine=RecoveryScoreEngine(),
    )
    return Container(
        settings=resolved,
        database=database,
        registry=registry,
        search_service=search_service,
        import_service=ImportService(resolved, database),
        subject_store=SubjectStore(),
    )
