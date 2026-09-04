"""Sources that are planned but not connected.

Each is present in the registry so that every report lists it explicitly as
"не подключено". Naming a gap is more useful than hiding it: an operator reading
the report knows the score was computed without court data, rather than assuming
the courts were clean.

None of these do any scraping. A source gets an implementation when a lawful,
stable interface to it exists.
"""

from __future__ import annotations

from collections.abc import Collection

from app.domain.enums import ProviderName
from app.providers.base import StubProvider

_STUBS: tuple[tuple[ProviderName, str, str], ...] = (
    (
        ProviderName.COURT,
        "Суды",
        "Интеграция с судебными источниками не подключена",
    ),
    (
        ProviderName.PROPERTY,
        "Недвижимость",
        "Проверка недвижимости требует законного доступа к ЕГРН",
    ),
    (
        ProviderName.PLEDGE,
        "Залоги",
        "Реестр залогов движимого имущества не подключён",
    ),
    (
        ProviderName.INHERITANCE,
        "Наследственные дела",
        "Реестр наследственных дел не подключён",
    ),
)


def build_future_providers(*, exclude: Collection[ProviderName] = ()) -> list[StubProvider]:
    """Stubs for every source no real adapter was registered for.

    ``exclude`` names the sources that now have one. Courts and pledges left
    this list the moment their NewDB methods became mappable, and the stub must
    step aside rather than shadow the real provider: a report is addressed by
    provider name, so two entries under one name would silently drop one.
    """
    return [StubProvider(name, title, note) for name, title, note in _STUBS if name not in exclude]
