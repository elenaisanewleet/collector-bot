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
from app.providers.arbitr_legal import NewDBLegalCasesProvider
from app.providers.base import BaseProvider
from app.providers.court import NewDBArbitrationProvider
from app.providers.fedresurs import FedresursProvider, NewDBBankruptcyProvider
from app.providers.fns import FNSProvider, NewDBBusinessProvider
from app.providers.fssp import FSSPProvider
from app.providers.future import build_future_providers
from app.providers.identity_bridge import InnBridgeProvider, PassportInnProvider
from app.providers.internal.base import InternalDebtorProvider
from app.providers.internal.composite import CompositeInternalDebtorProvider
from app.providers.internal.csv_provider import CSVInternalDebtorProvider
from app.providers.internal.db_provider import DatabaseInternalDebtorProvider
from app.providers.mock import DemoInnBridgeProvider, build_demo_providers
from app.providers.newdb import NewDBFieldMaps
from app.providers.pledge import NewDBPledgeProvider
from app.providers.property import NewDBPropertyProvider
from app.providers.vehicle import UnconfiguredVehicleProvider

logger = get_logger(__name__)


class DuplicateProviderError(ValueError):
    """Two providers registered under one name.

    Reports are addressed by provider name, so a duplicate means one of the two
    would silently never be read. Fail at wiring time instead.
    """


class ProviderRegistry:
    """Holds the internal provider plus every external source.

    ``inn_bridge`` is a named optional slot rather than a member of ``external``,
    and the difference matters twice over. It runs *before* the external wave,
    because three of those sources cannot be addressed until it answers — put it
    in ``external`` and ``asyncio.gather`` would start it in parallel with the
    very providers it exists to feed. And it must stay out of
    ``configured_names``, which is both the report's source list multiplier and
    the batch estimate's ``providers_per_debtor``: the bridge adds one call per
    debtor, not one per provider.
    """

    def __init__(
        self,
        *,
        internal: InternalDebtorProvider,
        external: Sequence[BaseProvider],
        inn_bridge: InnBridgeProvider | None = None,
    ) -> None:
        self._internal = internal
        self._external = list(external)
        self._inn_bridge = inn_bridge
        _reject_duplicates([*self._external, *([inn_bridge] if inn_bridge else [])])

    @property
    def internal(self) -> InternalDebtorProvider:
        return self._internal

    @property
    def external(self) -> list[BaseProvider]:
        return list(self._external)

    @property
    def inn_bridge(self) -> InnBridgeProvider | None:
        """Мост «паспорт → ИНН», если он собран для этой конфигурации."""
        return self._inn_bridge

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


def build_external_providers(
    settings: Settings, database: Database | None = None
) -> list[BaseProvider]:
    """Choose demo or live adapters, then append the not-yet-connected sources.

    ``database`` is needed by one source only: the chain over the debtor's
    companies caches by company ИНН rather than by subject, so two debtors from
    one holding do not pay for the same answer twice. Without it the chain still
    works and simply pays each time.
    """
    providers: list[BaseProvider] = []

    if settings.app_mode is AppMode.DEMO:
        providers.extend(build_demo_providers())
    else:
        field_maps = NewDBFieldMaps.load(settings.newdb_field_map)
        logger.info("newdb.methods_mapped", methods=sorted(field_maps.methods))
        providers.append(FSSPProvider(settings))
        providers.append(_fedresurs_provider(settings, field_maps))
        providers.append(_fns_provider(settings, field_maps))
        # Sources NewDB is the only carrier for. Constructed whether or not
        # their methods are mapped: unmapped, they answer NOT_CONFIGURED, which
        # is the same honest line the stub would print and one the operator can
        # act on ("опишите метод в NEWDB_FIELD_MAP").
        providers.append(NewDBPledgeProvider(settings, field_maps))
        providers.append(NewDBArbitrationProvider(settings, field_maps))
        # Разбираются кодом по живому ответу, поэтому гейт у них — настройка, а
        # не запись в карте. Выключенные, они отвечают NOT_CONFIGURED и говорят,
        # какой именно флаг это включает.
        providers.append(NewDBPropertyProvider(settings, field_maps))
        providers.append(NewDBLegalCasesProvider(settings, field_maps, database=database))

    providers.append(UnconfiguredVehicleProvider())
    providers.extend(build_future_providers(exclude={provider.name for provider in providers}))
    return providers


def _fedresurs_provider(settings: Settings, field_maps: NewDBFieldMaps) -> BaseProvider:
    if settings.fedresurs_backend is FedresursBackend.DEMO:
        from app.providers.mock import DemoFedresursProvider

        return DemoFedresursProvider()
    if settings.fedresurs_backend is FedresursBackend.NEWDB:
        return NewDBBankruptcyProvider(settings, field_maps)
    return FedresursProvider(settings)


def _fns_provider(settings: Settings, field_maps: NewDBFieldMaps) -> BaseProvider:
    if settings.fns_provider is FNSBackend.DEMO:
        from app.providers.mock import DemoFNSProvider

        return DemoFNSProvider()
    if settings.fns_provider is FNSBackend.NEWDB:
        return NewDBBusinessProvider(settings, field_maps)
    return FNSProvider(settings)


def build_inn_bridge(settings: Settings) -> InnBridgeProvider:
    """Мост «паспорт → ИНН» под текущий режим.

    В демо — детерминированный провайдер без единого сетевого обращения: демо
    обязано работать без ключа. В live — настоящий метод NewDB, который без
    ключа или без ``INN_BRIDGE_ENABLED`` отвечает ``NOT_CONFIGURED`` и не тратит
    ничего.
    """
    if settings.app_mode is AppMode.DEMO:
        return DemoInnBridgeProvider()
    return PassportInnProvider(settings)


def build_registry(settings: Settings, database: Database) -> ProviderRegistry:
    registry = ProviderRegistry(
        internal=build_internal_provider(settings, database),
        external=build_external_providers(settings, database),
        inn_bridge=build_inn_bridge(settings),
    )
    bridge = registry.inn_bridge
    logger.info(
        "providers.ready",
        mode=settings.app_mode.value,
        configured=[name.value for name in registry.configured_names],
        inn_bridge=bool(bridge and bridge.is_configured),
    )
    return registry
