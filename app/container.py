"""Composition root.

Everything is constructed here, once, and handed to the handlers by the
dependency middleware. Nothing below this file reaches for a global.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings, get_settings
from app.db.session import Database
from app.providers.registry import ProviderRegistry, build_registry
from app.services.access import AccessService
from app.services.batch import BatchService
from app.services.identity import IdentityMatcher
from app.services.import_service import ImportService
from app.services.phone_lookups import PhoneLookupService
from app.services.query_card import QueryCardService
from app.services.scoring import RecoveryScoreEngine
from app.services.search import SearchService
from app.services.share import ShareLinkService
from app.services.subject_store import SubjectStore
from app.services.verdict import VerdictEngine


@dataclass(slots=True)
class Container:
    """Application services, wired and ready."""

    settings: Settings
    database: Database
    registry: ProviderRegistry
    search_service: SearchService
    import_service: ImportService
    batch_service: BatchService
    verdict_engine: VerdictEngine
    share_service: ShareLinkService
    subject_store: SubjectStore
    access_service: AccessService
    query_cards: QueryCardService
    phone_lookups: PhoneLookupService

    async def dispose(self) -> None:
        await self.database.dispose()


def build_container(settings: Settings | None = None) -> Container:
    resolved = settings or get_settings()
    database = Database(resolved.database_url)
    registry = build_registry(resolved, database)
    verdict_engine = VerdictEngine(resolved)
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
        batch_service=BatchService(
            settings=resolved,
            database=database,
            search_service=search_service,
            verdict_engine=verdict_engine,
        ),
        verdict_engine=verdict_engine,
        share_service=ShareLinkService(resolved, database),
        subject_store=SubjectStore(),
        access_service=AccessService(resolved, database),
        query_cards=QueryCardService(database, resolved),
        phone_lookups=PhoneLookupService(resolved, database),
    )
