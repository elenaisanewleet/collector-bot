"""Временный зонд: проверка утверждений о мосте. Файл удаляется после прогона."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import respx

from app.config import FedresursBackend, FNSBackend, Settings
from app.db.repository import DebtorRepository
from app.db.session import Database
from app.domain.enums import ProviderName, ProviderStatus
from app.providers.identity_bridge import PassportInnProvider
from app.providers.newdb import NewDBFieldMaps
from app.providers.registry import ProviderRegistry, build_internal_provider
from app.services.batch import BatchService, _subject_for
from app.services.import_service import ImportService
from app.services.scoring import RecoveryScoreEngine
from app.services.search import SearchService
from app.services.verdict import VerdictEngine

HEADER = "debtor_id,fio,birth_date,passport,contract_number,debt_amount"
ROW = "PR-1,Тестов Андрей Сергеевич,12.03.1985,4015 350278,EV-1,100000"
BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"
INN = "272116001938"


def _settings(base: Settings, *, store: bool) -> Settings:
    return base.model_copy(
        update={
            "app_mode": "live",
            "newdb_api_key": "k",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 2,
            "newdb_poll_interval_seconds": 0.01,
            "inn_bridge_enabled": True,
            "store_sensitive_identifiers": store,
            "fedresurs_backend": FedresursBackend.NONE,
            "fns_provider": FNSBackend.NONE,
            "cache_ttl_hours": 0,
        }
    )


def _envelope() -> dict[str, Any]:
    return {
        "state": "complete",
        "results": {"company": {"result": {"status": 200, "data": [{"innfiz": INN}]}}},
    }


@respx.mock
@pytest.mark.asyncio
async def test_probe(settings: Settings, database: Database) -> None:
    live = _settings(settings, store=True)
    await ImportService(live, database).import_text("\n".join([HEADER, ROW]))

    async with database.session() as session:
        rows = await DebtorRepository(session).find_by_external_id("PR-1")
    row = rows[0]
    print("STORED passport:", row.passport, "| masked:", row.passport_masked)

    subject = _subject_for(row)
    assert subject is not None
    print("BATCH SUBJECT passport:", subject.passport)

    registry = ProviderRegistry(
        internal=build_internal_provider(live, database),
        external=[],
        inn_bridge=PassportInnProvider(live),
    )
    search = SearchService(settings=live, database=database, registry=registry)
    batch = BatchService(
        settings=live,
        database=database,
        search_service=search,
        verdict_engine=VerdictEngine(live),
    )
    estimate = await batch.estimate()
    print("ESTIMATE bridge_calls:", estimate.bridge_calls, "| enabled:", estimate.bridge_enabled)

    route = respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=_envelope()))
    report = await search.search(subject, telegram_user_id=1)
    print("HTTP CALLS DURING RUN:", route.call_count)
    bridge = report.result_for(ProviderName.INN_BRIDGE)
    print("BRIDGE RESULT:", None if bridge is None else bridge.status)
    assert bridge is not None and bridge.status is ProviderStatus.SUCCESS

    # Второй заход: флаг выключен на импорте — паспорта в базе нет.
    off = _settings(settings, store=False)
    await ImportService(off, database).import_text(
        "\n".join([HEADER, "PR-2,Тестова Мария Ивановна,01.02.1980,4015 350279,EV-2,100000"])
    )
    async with database.session() as session:
        rows2 = await DebtorRepository(session).find_by_external_id("PR-2")
    print("OFF-FLAG passport:", rows2[0].passport, "| masked:", rows2[0].passport_masked)

    # Повторный импорт того же файла при включённом флаге разворачивает маску?
    await ImportService(live, database).import_text(
        "\n".join([HEADER, "PR-2,Тестова Мария Ивановна,01.02.1980,4015 350279,EV-2,100000"])
    )
    async with database.session() as session:
        rows3 = await DebtorRepository(session).find_by_external_id("PR-2")
    print("AFTER REIMPORT passport:", rows3[0].passport)
    _ = NewDBFieldMaps, RecoveryScoreEngine
