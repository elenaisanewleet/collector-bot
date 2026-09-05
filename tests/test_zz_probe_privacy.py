"""Проверка: адрес проживания должника в сохранённом теле arbitr_person."""
from __future__ import annotations

import json

import pytest
import respx

from app.config import Settings
from app.domain.enums import ProviderName
from app.providers.court import NewDBArbitrationProvider
from app.providers.newdb import NewDBFieldMaps
from tests.test_newdb_live_answers import (  # noqa: F401
    LITIGANT,
    live,
    report_of,
    result_of,
    shipped_maps,
    shipped_settings,
)

HOME = "Саратовская обл., г. Ртищево, ул. Луговая, д.1, кв.1"


@respx.mock
async def test_arbitration_raw_keeps_the_home_address(
    shipped_settings: Settings, shipped_maps: NewDBFieldMaps
) -> None:
    body = live("arbitr_person")
    assert HOME in json.dumps(body, ensure_ascii=False)

    settings = shipped_settings.model_copy(update={"store_raw_responses": True})
    provider = NewDBArbitrationProvider(settings, shipped_maps)
    report = await report_of(provider, LITIGANT, body)

    raw = result_of(report, ProviderName.COURT).raw_response
    assert raw is not None
    print("\n_redacted_fields present:", "_redacted_fields" in raw)
    print("debtor home address occurrences in stored body:", raw.count(HOME))
    print("third party home address:", raw.count("Дегтярный пер"))
    assert HOME in raw
