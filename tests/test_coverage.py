"""«Что откроется от этого поля» считается, а не пишется.

Захардкоженный список источников рядом с кнопкой разъезжается с провайдером при
первой правке гейта, и разъехавшись, врёт. Здесь сторожатся две вещи: что
:mod:`app.services.coverage` берёт ответ у самих провайдеров, и что предикат
провайдера согласован с его же ``_fetch``.

Второе — важнее. ``missing_input_for`` существует ровно для того, чтобы карточка
и прогон говорили одно и то же; если предикат разойдётся с гейтом, карточка
пообещает источник, который откажется отвечать, — то есть сделает ровно то, что
этот инструмент существует запрещать.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.config import FedresursBackend, FNSBackend, Settings
from app.db.session import Database
from app.domain.enums import ProviderName
from app.domain.identity import PersonName, SearchSubject
from app.providers.base import BaseProvider
from app.providers.registry import (
    ProviderRegistry,
    build_external_providers,
    build_inn_bridge,
    build_internal_provider,
)
from app.services import coverage

SHIPPED_MAP = Path("config/field_maps/example_newdb.json")


@pytest.fixture
def wired_settings(live_settings: Settings) -> Settings:
    """Живой набор источников с ключом: только у такого есть чему открываться.

    В демо все источники отвечают по одному ФИО, и разница между «нужен ИНН» и
    «нужна дата» там не наблюдаема вовсе.
    """
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": "https://api.example.test",
            "newdb_field_map": SHIPPED_MAP,
            "fedresurs_backend": FedresursBackend.NEWDB,
            "fns_provider": FNSBackend.NEWDB,
            "inn_bridge_enabled": True,
        }
    )


@pytest.fixture
def wired_registry(wired_settings: Settings, database: Database) -> ProviderRegistry:
    return ProviderRegistry(
        internal=build_internal_provider(wired_settings, database),
        external=build_external_providers(wired_settings),
        inn_bridge=build_inn_bridge(wired_settings),
    )


@pytest.fixture
def with_fio() -> SearchSubject:
    return SearchSubject(
        search_type="person",
        name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
    )


@pytest.fixture
def with_fio_and_date(with_fio: SearchSubject) -> SearchSubject:
    return with_fio.model_copy(update={"birth_date": date(1985, 3, 12)})


def test_the_inn_opens_exactly_three_sources(
    with_fio_and_date: SearchSubject, wired_registry: ProviderRegistry
) -> None:
    """Банкротство, статус ИП и арбитраж — и больше ничего.

    Ровно эти три ищут только по ИНН физлица и больше ничем не открываются.
    """
    assert set(coverage.unlocked_by("inn", with_fio_and_date, wired_registry)) == {
        ProviderName.FEDRESURS,
        ProviderName.FNS,
        ProviderName.COURT,
    }


def test_the_birth_date_opens_fssp_and_pledges(
    with_fio: SearchSubject, wired_registry: ProviderRegistry
) -> None:
    assert set(coverage.unlocked_by("birth_date", with_fio, wired_registry)) == {
        ProviderName.FSSP,
        ProviderName.PLEDGE,
    }


def test_the_phone_opens_nothing_at_all(
    with_fio_and_date: SearchSubject, wired_registry: ProviderRegistry
) -> None:
    """Ни один внешний реестр по телефону не ищет — и карточка не смеет обещать.

    Это и есть причина, по которой телефон не стоит в блоке «Не спрошу»
    наравне с ИНН: он там соврал бы формой.
    """
    assert coverage.unlocked_by("phone", with_fio_and_date, wired_registry) == ()


def test_the_passport_opens_the_bridge_not_the_three_sources(
    with_fio_and_date: SearchSubject, wired_registry: ProviderRegistry
) -> None:
    """Паспорт открывает мост, а «если» между мостом и тремя источниками — не
    формальность: ФНС по этим данным вполне может не найти ИНН."""
    assert coverage.unlocked_by("passport", with_fio_and_date, wired_registry) == (
        ProviderName.INN_BRIDGE,
    )


def test_a_known_inn_makes_the_passport_pointless(
    with_fio_and_date: SearchSubject, wired_registry: ProviderRegistry
) -> None:
    """Продавать платный вызов за уже имеющийся ответ нельзя."""
    with_inn = with_fio_and_date.model_copy(update={"inn": "770912345601"})
    assert coverage.unlocked_by("passport", with_inn, wired_registry) == ()


def test_the_vin_opens_pledges_without_a_birth_date(
    with_fio: SearchSubject, wired_registry: ProviderRegistry
) -> None:
    """Единственный путь к залогам в обход даты рождения."""
    assert ProviderName.PLEDGE in coverage.unlocked_by("vin", with_fio, wired_registry)


def test_blocked_groups_sources_by_a_common_reason(
    with_fio: SearchSubject, wired_registry: ProviderRegistry
) -> None:
    """«ЕФРСБ, ФНС, Суды — нужен ИНН» это одно действие, а не три беды."""
    groups = coverage.blocked(with_fio, wired_registry)
    by_inn = next(names for reason, names in groups.items() if reason == ("inn",))
    assert set(by_inn) == {ProviderName.FEDRESURS, ProviderName.FNS, ProviderName.COURT}


def test_the_internal_source_always_answers(
    with_fio: SearchSubject, wired_registry: ProviderRegistry
) -> None:
    """Наша собственная база ключа не требует и «нечем спросить» сказать не может."""
    assert ProviderName.INTERNAL in coverage.will_answer(with_fio, wired_registry)


# ------------------------------------------------- предикат против гейта


def _gated_providers(registry: ProviderRegistry) -> list[BaseProvider]:
    bridge = registry.inn_bridge
    return [*registry.external, *([bridge] if bridge is not None else [])]


@pytest.mark.parametrize(
    "subject",
    [
        SearchSubject(search_type="person"),
        SearchSubject(search_type="person", name=PersonName(last_name="А", first_name="Б")),
        SearchSubject(
            search_type="person",
            name=PersonName(last_name="А", first_name="Б"),
            birth_date=date(1985, 3, 12),
        ),
    ],
    ids=["пусто", "только ФИО", "ФИО и дата"],
)
async def test_the_predicate_agrees_with_the_gate(
    subject: SearchSubject, wired_registry: ProviderRegistry
) -> None:
    """``missing_input_for`` и ``_fetch`` — один и тот же код, и это проверяется.

    Провайдер, у которого предикат говорит «спросим», обязан не ответить
    ``insufficient_query``; и наоборот, отказавшийся обязан назвать в
    ``missing_input`` ровно то, что назвал предикат. Сетевых вызовов здесь не
    происходит: все ветки, до которых доходит дело, отсекаются гейтом раньше
    HTTP, а те, что не отсекаются, ловятся как ошибка соединения и в проверку
    не входят.
    """
    for provider in _gated_providers(wired_registry):
        if not provider.is_configured:
            continue
        predicted = provider.missing_input_for(subject)
        if not predicted:
            continue
        result = await provider.fetch(subject)
        assert result.error_code == "insufficient_query", provider.name
        assert result.missing_input == tuple(item.value for item in predicted), provider.name
