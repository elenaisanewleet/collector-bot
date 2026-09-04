"""Provider wiring.

The registry is the single place that decides which concrete sources exist for a
given configuration. Services receive a registry, never a specific provider, so
adding or swapping a source touches this file alone.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from pathlib import Path

from app.config import AppMode, FedresursBackend, FNSBackend, Settings
from app.db.session import Database
from app.domain.enums import ProviderName
from app.logging_setup import get_logger
from app.providers.base import BaseProvider
from app.providers.fedresurs import FedresursProvider
from app.providers.fns import FNSProvider
from app.providers.fssp import FSSPProvider
from app.providers.future import build_future_providers
from app.providers.internal.base import InternalDebtorProvider
from app.providers.internal.composite import CompositeInternalDebtorProvider
from app.providers.internal.csv_provider import CSVInternalDebtorProvider
from app.providers.internal.db_provider import DatabaseInternalDebtorProvider
from app.providers.mock import build_demo_providers
from app.providers.vehicle import UnconfiguredVehicleProvider

logger = get_logger(__name__)


class DuplicateProviderError(ValueError):
    """Two providers registered under one name.

    Reports are addressed by provider name, so a duplicate means one of the two
    would silently never be read. Fail at wiring time instead.
    """


class ProviderRegistry:
    """Holds the internal provider plus every external source."""

    def __init__(
        self,
        *,
        internal: InternalDebtorProvider,
        external: Sequence[BaseProvider],
    ) -> None:
        self._internal = internal
        self._external = list(external)
        _reject_duplicates(self._external)

    @property
    def internal(self) -> InternalDebtorProvider:
        return self._internal

    @property
    def external(self) -> list[BaseProvider]:
        return list(self._external)

    def __iter__(self) -> Iterator[BaseProvider]:
        return iter(self._external)

    def get(self, name: ProviderName) -> BaseProvider | None:
        return next((provider for provider in self._external if provider.name is name), None)

    @property
    def configured_names(self) -> list[ProviderName]:
        return [provider.name for provider in self._external if provider.is_configured]


def _reject_duplicates(providers: Sequence[BaseProvider]) -> None:
    seen: set[ProviderName] = set()
    for provider in providers:
        if provider.name in seen:
            raise DuplicateProviderError(
                f"provider {provider.name.value!r} is registered more than once"
            )
        seen.add(provider.name)


def build_internal_provider(settings: Settings, database: Database) -> InternalDebtorProvider:
    """CSV bootstrap export + everything imported into the database.

    A future ``OneCODataProvider`` joins this list; nothing else changes.
    """
    sources: list[InternalDebtorProvider] = [DatabaseInternalDebtorProvider(database)]
    csv_path = Path(settings.internal_csv_path)
    if csv_path.is_file():
        sources.append(CSVInternalDebtorProvider(csv_path))
    else:
        logger.info("internal_csv.absent", path=str(csv_path))
    return CompositeInternalDebtorProvider(sources)


def build_external_providers(settings: Settings) -> list[BaseProvider]:
    """Choose demo or live adapters, then append the not-yet-connected sources."""
    providers: list[BaseProvider] = []

    if settings.app_mode is AppMode.DEMO:
        providers.extend(build_demo_providers())
    else:
        providers.append(FSSPProvider(settings))
        providers.append(_fedresurs_provider(settings))
        providers.append(_fns_provider(settings))

    providers.append(UnconfiguredVehicleProvider())
    providers.extend(build_future_providers())
    return providers


def _fedresurs_provider(settings: Settings) -> BaseProvider:
    if settings.fedresurs_backend is FedresursBackend.DEMO:
        from app.providers.mock import DemoFedresursProvider

        return DemoFedresursProvider()
    return FedresursProvider(settings)


def _fns_provider(settings: Settings) -> BaseProvider:
    if settings.fns_provider is FNSBackend.DEMO:
        from app.providers.mock import DemoFNSProvider

        return DemoFNSProvider()
    return FNSProvider(settings)


def build_registry(settings: Settings, database: Database) -> ProviderRegistry:
    registry = ProviderRegistry(
        internal=build_internal_provider(settings, database),
        external=build_external_providers(settings),
    )
    logger.info(
        "providers.ready",
        mode=settings.app_mode.value,
        configured=[name.value for name in registry.configured_names],
    )
    return registry
