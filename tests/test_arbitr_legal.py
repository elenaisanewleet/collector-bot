"""Арбитраж компаний должника — цепочка ФНС → ИНН юрлиц → arbitr_legal.

Здесь проверяется не столько разбор, сколько границы: что источник не
запускается сам, что предел цепочки объявляется вслух, что дело компании не
превращается в иск к человеку и что найденное доезжает до отчёта даже при
нулевом совпадении личности — потому что совпадать ему не с чем.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import (
    BusinessRole,
    BusinessStatus,
    CourtCaseRole,
    EntityType,
    ProviderName,
    ProviderStatus,
)
from app.domain.identity import SearchSubject
from app.domain.models import BusinessRelation, LegalEntityCase, ProviderResult
from app.providers.arbitr_legal import NewDBLegalCasesProvider
from app.providers.base import FetchContext
from app.providers.newdb import NewDBFieldMaps

BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"
LIVE_FIXTURES = Path(__file__).parent / "fixtures" / "newdb"

COMPANY_INN = "9728012826"
OPPONENT_INN = "7716863554"


def live_fixture(name: str) -> Any:
    return json.loads((LIVE_FIXTURES / name).read_text(encoding="utf-8"))


def company(
    inn: str = COMPANY_INN,
    *,
    name: str = 'ООО "СТАЛЬНОЕ СЕРДЦЕ"',
    role: BusinessRole = BusinessRole.DIRECTOR,
    bankrupt: bool = True,
) -> BusinessRelation:
    return BusinessRelation(
        inn=inn,
        name=name,
        entity_type=EntityType.LEGAL_ENTITY,
        role=role,
        status=BusinessStatus.ACTIVE,
        bankruptcy_flag=bankrupt,
        linked_by_identifier=True,
    )


def fns_result(*relations: BusinessRelation) -> ProviderResult:
    return ProviderResult(
        provider=ProviderName.FNS,
        status=ProviderStatus.SUCCESS if relations else ProviderStatus.NO_RESULTS,
        records=list(relations),
    )


@pytest.fixture
def legal_settings(live_settings: Settings) -> Settings:
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 2,
            "newdb_poll_interval_seconds": 0.01,
            "provider_max_retries": 0,
            "provider_retry_backoff_seconds": 0.0,
            "arbitr_legal_enabled": True,
            "arbitr_legal_in_batch": True,
            "cache_ttl_hours": 0,
        }
    )


@pytest.fixture
def provider(legal_settings: Settings) -> NewDBLegalCasesProvider:
    return NewDBLegalCasesProvider(legal_settings, NewDBFieldMaps())


@pytest.fixture
def person() -> SearchSubject:
    return SearchSubject(search_type="person", inn="770600089967")


# ---------------------------------------------------------------- разбор


@respx.mock
async def test_wrapper_is_unwrapped(
    provider: NewDBLegalCasesProvider, person: SearchSubject
) -> None:
    """``data[0]`` — обёртка, дела в ``detailed_cases``, счётчик в ``total_count``."""
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("arbitr_legal.json"))
    )

    result = await provider.fetch(person, FetchContext(upstream=(fns_result(company()),)))

    assert result.status is ProviderStatus.SUCCESS
    cases = [record for record in result.records if isinstance(record, LegalEntityCase)]
    assert len(cases) == 3
    assert cases[0].total_count == 3
    assert cases[0].analyzed_count == 3


@respx.mock
async def test_company_inn_comes_from_the_query_not_from_parties(
    provider: NewDBLegalCasesProvider, person: SearchSubject
) -> None:
    """``parties.debtor.inn`` — это ИНН оппонента, а не нашей компании.

    Карта, взявшая ``inn`` оттуда, привязала бы дело к постороннему юрлицу: в
    живом ответе там ООО «Космос Лоджистик», а проверялось «Стальное сердце».
    """
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("arbitr_legal.json"))
    )

    result = await provider.fetch(person, FetchContext(upstream=(fns_result(company()),)))

    cases = [record for record in result.records if isinstance(record, LegalEntityCase)]
    assert {case.company_inn for case in cases} == {COMPANY_INN}
    assert OPPONENT_INN in {case.opponent_inn for case in cases}


@respx.mock
async def test_company_is_plaintiff_in_the_live_answer(
    provider: NewDBLegalCasesProvider, person: SearchSubject
) -> None:
    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("arbitr_legal.json"))
    )

    result = await provider.fetch(person, FetchContext(upstream=(fns_result(company()),)))

    cases = [record for record in result.records if isinstance(record, LegalEntityCase)]
    assert {case.case_role for case in cases} == {CourtCaseRole.PLAINTIFF}
    assert any(case.amount == Decimal("2105643.84") for case in cases)
    assert any(case.enforcement_signal for case in cases)


# ---------------------------------------------------------------- границы


async def test_chain_is_off_by_default(live_settings: Settings, person: SearchSubject) -> None:
    settings = live_settings.model_copy(update={"newdb_api_key": "k", "newdb_base_url": BASE_URL})
    provider = NewDBLegalCasesProvider(settings, NewDBFieldMaps())

    assert not provider.is_configured
    result = await provider.fetch(person, FetchContext(upstream=(fns_result(company()),)))
    assert result.status is ProviderStatus.NOT_CONFIGURED


@respx.mock
async def test_no_companies_is_not_the_same_as_fns_silent(
    provider: NewDBLegalCasesProvider, person: SearchSubject
) -> None:
    """Два разных статуса и два разных текста: «нечего проверять» ≠ «не знаем»."""
    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json={}))

    silent = await provider.fetch(
        person,
        FetchContext(
            upstream=(ProviderResult(provider=ProviderName.FNS, status=ProviderStatus.UNAVAILABLE),)
        ),
    )
    empty = await provider.fetch(person, FetchContext(upstream=(fns_result(),)))

    assert silent.status is ProviderStatus.NOT_CONFIGURED
    assert "ФНС не ответила" in (silent.error_message or "")
    assert empty.status is ProviderStatus.NO_RESULTS
    assert any("проверять нечего" in note for note in empty.notes)
    assert route.call_count == 0


@respx.mock
async def test_cap_is_reported_not_silent(
    provider: NewDBLegalCasesProvider, person: SearchSubject
) -> None:
    """Упёрлись в предел — отчёт обязан это сказать, а не промолчать."""
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("arbitr_legal.json"))
    )
    companies = [
        company(inn=f"972801282{digit}", name=f"ООО {digit}", bankrupt=False) for digit in range(7)
    ]

    result = await provider.fetch(person, FetchContext(upstream=(fns_result(*companies),)))

    assert route.call_count == 3
    assert any("Остальные 4 не проверялись" in note for note in result.notes)


@respx.mock
async def test_batch_needs_its_own_flag(legal_settings: Settings, person: SearchSubject) -> None:
    settings = legal_settings.model_copy(update={"arbitr_legal_in_batch": False})
    provider = NewDBLegalCasesProvider(settings, NewDBFieldMaps())
    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json={}))

    result = await provider.fetch(
        person, FetchContext(batch=True, upstream=(fns_result(company()),))
    )

    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert "ARBITR_LEGAL_IN_BATCH" in (result.error_message or "")
    assert route.call_count == 0
    assert provider.max_planned_calls(person, FetchContext(batch=True)) == 0


@respx.mock
async def test_only_active_director_or_founder_roles_are_chased(
    provider: NewDBLegalCasesProvider, person: SearchSubject
) -> None:
    route = respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("arbitr_legal.json"))
    )
    relations = [
        company(inn="1000000001", role=BusinessRole.OTHER, bankrupt=False),
        BusinessRelation(
            inn="1000000002",
            name="ООО Ликвидировано",
            entity_type=EntityType.LEGAL_ENTITY,
            role=BusinessRole.DIRECTOR,
            status=BusinessStatus.TERMINATED,
        ),
        BusinessRelation(
            inn="770600089967",
            name="ИП Парфененко",
            entity_type=EntityType.SOLE_PROPRIETOR,
            role=BusinessRole.SOLE_PROPRIETOR,
            status=BusinessStatus.ACTIVE,
        ),
    ]

    result = await provider.fetch(person, FetchContext(upstream=(fns_result(*relations),)))

    assert route.call_count == 0
    assert result.status is ProviderStatus.NO_RESULTS


# ---------------------------------------------------------------- отчёт и балл


@respx.mock
async def test_legal_case_survives_identity_matching(
    provider: NewDBLegalCasesProvider, person: SearchSubject
) -> None:
    """Дело компании не сопоставляется с человеком — и не должно за это пропадать."""
    from app.services.aggregation import Aggregator

    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("arbitr_legal.json"))
    )
    result = await provider.fetch(person, FetchContext(upstream=(fns_result(company()),)))

    report = Aggregator().build(person, [result])

    assert len(report.legal_entity_cases) == 3
    assert report.court_cases == []


@respx.mock
async def test_legal_case_is_never_a_court_case(
    provider: NewDBLegalCasesProvider, person: SearchSubject
) -> None:
    """Иск компании — не иск к должнику, и в скоринге его быть не должно."""
    from app.services.aggregation import Aggregator
    from app.services.scoring import RecoveryScoreEngine

    respx.post(NEWDB_URL).mock(
        return_value=httpx.Response(200, json=live_fixture("arbitr_legal.json"))
    )
    result = await provider.fetch(person, FetchContext(upstream=(fns_result(company()),)))
    report = Aggregator().build(person, [result])

    score = RecoveryScoreEngine().evaluate(report)

    assert report.claims_against_debtor == []
    assert "claims_against_debtor" not in {factor.name for factor in score.factors}
    assert not [factor for factor in score.factors if factor.delta < 0]
