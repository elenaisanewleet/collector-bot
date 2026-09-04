"""Sources that are planned but not connected.

Each is present in the registry so that every report lists it explicitly as
"не подключено". Naming a gap is more useful than hiding it: an operator reading
the report knows the score was computed without court data, rather than assuming
the courts were clean.

None of these do any scraping. A source gets an implementation when a lawful,
stable interface to it exists.
"""

from __future__ import annotations

from app.domain.enums import ProviderName
from app.providers.base import StubProvider


def build_future_providers() -> list[StubProvider]:
    return [
        StubProvider(
            ProviderName.COURT,
            "Суды",
            "Интеграция с судебными источниками не подключена",
        ),
        StubProvider(
            ProviderName.PROPERTY,
            "Недвижимость",
            "Проверка недвижимости требует законного доступа к ЕГРН",
        ),
        StubProvider(
            ProviderName.PLEDGE,
            "Залоги",
            "Реестр залогов движимого имущества не подключён",
        ),
        StubProvider(
            ProviderName.INHERITANCE,
            "Наследственные дела",
            "Реестр наследственных дел не подключён",
        ),
    ]
