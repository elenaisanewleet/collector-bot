"""Scratch probes — review only."""
from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import ProviderName, ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import DebtorReport
from app.providers.base import BaseProvider
from app.providers.court import NewDBArbitrationProvider
from app.providers.fedresurs import NewDBBankruptcyProvider
from app.providers.pledge import NewDBPledgeProvider
from app.providers.newdb import NewDBFieldMaps
from app.services.aggregation import Aggregator
from app.services.reporting import render_report
from app.services.scoring import RecoveryScoreEngine

BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"
SHIPPED_MAP = Path("config/field_maps/example_newdb.json")
LIVE = Path(__file__).parent / "data"

LITIGANT = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Викторович"),
    inn="644600011111",
)


def live(name: str) -> dict[str, Any]:
    return json.loads((LIVE / f"newdb_live_{name}.json").read_text(encoding="utf-8"))


@pytest.fixture
def shipped_maps() -> NewDBFieldMaps:
    return NewDBFieldMaps.load(SHIPPED_MAP)


@pytest.fixture
def shipped_settings(live_settings: Settings) -> Settings:
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_field_map": SHIPPED_MAP,
            "provider_max_retries": 0,
            "provider_retry_backoff_seconds": 0.0,
        }
    )


async def report_of(provider: BaseProvider, subject: SearchSubject, response: dict[str, Any]) -> DebtorReport:
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=response))
    result = await provider.fetch(subject)
    report = Aggregator().build(subject, [result])
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


@respx.mock
async def test_probe_case_without_number(shipped_settings, shipped_maps) -> None:
    body = live("arbitr_person")
    wrapper = body["results"]["arbitr_person"]["result"]["data"][0]
    case = wrapper["cases"][0]
    # Три дела, ни у одного нет номера — всё остальное на месте.
    broken = []
    for i in range(3):
        c = copy.deepcopy(case)
        c.pop("case_number")
        c["card"].pop("case_number", None)
        c["case_url"] = f"https://kad.arbitr.ru/Card/x{i}"
        broken.append(c)
    wrapper["cases"] = broken
    wrapper["total_count"] = 3
    wrapper["pagination"]["returned"] = 3
    report = await report_of(NewDBArbitrationProvider(shipped_settings, shipped_maps), LITIGANT, body)
    res = report.result_for(ProviderName.COURT)
    print("STATUS", res.status, "partial", res.is_partial, "notes", res.notes, "records", len(res.records))
    text = render_report(report)
    idx = text.find("Суды")
    print(text[idx:idx+400])
    print("FACTORS", {f.name: f.delta for f in report.recovery_score.factors})
