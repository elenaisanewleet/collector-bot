"""Налоговая задолженность.

Источник отвечает на вопрос «кто ещё стоит в очереди на те же деньги», и у него
три исхода вместо двух: сумма, подтверждённый ноль и ответ без суммы. Смешать
последние два — значит начислить должнику премию за неразговорчивость источника.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import ProviderName, ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import DebtorReport, ProviderResult, TaxDebtRecord
from app.providers.newdb import NewDBFieldMaps
from app.providers.tax_debt import NEWDB_METHOD, NewDBTaxDebtProvider
from app.services import reporting
from app.services.aggregation import Aggregator
from app.services.scoring import RecoveryScoreEngine

BASE_URL = "https://newdb.example.test"
NEWDB_URL = f"{BASE_URL}/v2"

SUBJECT = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
    birth_date=date(1985, 3, 12),
    inn="770912345601",
)


def envelope(total: str | None, *, items_count: int | None = None) -> dict[str, Any]:
    debt: dict[str, Any] = {}
    if total is not None:
        debt["total"] = total
    if items_count is not None:
        debt["items_count"] = items_count
    return {
        "state": "complete",
        "requestId": "00000000-0000-4000-8000-000000000001",
        "results": {NEWDB_METHOD: {"result": {"status": 200, "data": [{"debt": debt}]}}},
    }


@pytest.fixture
def settings(live_settings: Settings) -> Settings:
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 2,
            "newdb_poll_interval_seconds": 0.01,
            "provider_max_retries": 0,
        }
    )


@pytest.fixture
def maps() -> NewDBFieldMaps:
    return NewDBFieldMaps.load(Path("config/field_maps/example_newdb.json"))


@respx.mock
async def test_the_amount_is_read(settings: Settings, maps: NewDBFieldMaps) -> None:
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=envelope("125300.50")))

    result = await NewDBTaxDebtProvider(settings, maps).fetch(SUBJECT)

    assert result.status is ProviderStatus.SUCCESS
    record = result.records[0]
    assert isinstance(record, TaxDebtRecord)
    assert record.amount == Decimal("125300.50")


@respx.mock
async def test_a_zero_is_an_answer_not_an_absence(settings: Settings, maps: NewDBFieldMaps) -> None:
    """Ноль сохраняется как ноль.

    «Задолженности нет» — полноценная хорошая новость для взыскателя, и потерять
    её, приняв ноль за пустоту, значило бы выбросить ответ, за который заплачено.
    """
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=envelope("0.00")))

    result = await NewDBTaxDebtProvider(settings, maps).fetch(SUBJECT)

    record = result.records[0]
    assert isinstance(record, TaxDebtRecord)
    assert record.amount == Decimal(0)


@respx.mock
async def test_an_answer_without_an_amount_is_never_a_zero(
    settings: Settings, maps: NewDBFieldMaps
) -> None:
    """Ответ без суммы не становится ни нулём, ни пустотой.

    Разница ценой в балл: за подтверждённый ноль отчёт начисляет плюс, и выдать
    его за ответ, в котором суммы не было, значит заплатить должнику за
    неразговорчивость источника.

    Здесь срабатывает общее правило продукта: строка, из которой карта полей не
    прочла НИ ОДНОГО поля, роняет источник целиком. Ответ без ``debt.total`` —
    это ответ не той формы, которую мы описали, и «не проверено» тут честнее, чем
    догадка о его смысле. Проверяется именно это: не пустой успех, а видимый
    отказ.
    """
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=envelope(None)))

    result = await NewDBTaxDebtProvider(settings, maps).fetch(SUBJECT)

    assert result.records == []
    assert result.error_code == "unexpected_schema"
    assert not result.status.is_answered, "пустой ответ выдан за проверенный"


async def test_without_an_inn_the_source_is_not_queried(
    settings: Settings, maps: NewDBFieldMaps
) -> None:
    result = await NewDBTaxDebtProvider(settings, maps).fetch(
        SUBJECT.model_copy(update={"inn": None})
    )

    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered


def _report_with(*records: TaxDebtRecord, answered: bool = True) -> DebtorReport:
    result = ProviderResult(
        provider=ProviderName.TAX_DEBT,
        status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
    )
    if answered:
        result = result.model_copy(update={"records": list(records)})
    report = Aggregator().build(SUBJECT, [result])
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


def test_a_debt_is_penalised_and_the_reason_says_why() -> None:
    """Штраф объясняет механизм, а не просто называет сумму.

    Взыскателю важно не «долгов много», а конкретное полномочие: налоговая
    списывает со счёта без суда и исполнительного листа, то есть окажется
    впереди него в очереди на те же деньги.
    """
    report = _report_with(TaxDebtRecord(amount=Decimal("600000")))
    assert report.recovery_score is not None

    factor = next(f for f in report.recovery_score.factors if f.name == "tax_debt")
    assert factor.delta == -14
    assert "бесспорно" in factor.reason


def test_a_confirmed_zero_earns_a_bonus() -> None:
    report = _report_with(TaxDebtRecord(amount=Decimal(0)))
    assert report.recovery_score is not None
    names = {f.name for f in report.recovery_score.factors}

    assert "no_tax_debt" in names


def test_an_answer_without_an_amount_earns_nothing() -> None:
    """Ни плюса, ни минуса: утверждения не было."""
    report = _report_with(TaxDebtRecord(amount=None, items_count=3))
    assert report.recovery_score is not None
    names = {f.name for f in report.recovery_score.factors}

    assert "no_tax_debt" not in names
    assert "tax_debt" not in names


def test_the_section_says_the_zero_out_loud() -> None:
    """Ноль печатается словами, а не пустым местом."""
    text = reporting._tax_debt_block(_report_with(TaxDebtRecord(amount=Decimal(0))))

    assert reporting.TAX_DEBT_NONE_LINE in text


def test_the_section_separates_silence_from_zero() -> None:
    """Запись есть, суммы в ней нет — раздел говорит именно это.

    Достижимо, когда карта прочла хоть что-то (например, число позиций), а сумму
    нет. Печатать в этом случае «задолженности не найдено» значило бы выдать
    молчание за утверждение.
    """
    text = reporting._tax_debt_block(_report_with(TaxDebtRecord(amount=None, items_count=2)))

    assert reporting.TAX_DEBT_UNKNOWN_LINE in text
    assert reporting.TAX_DEBT_NONE_LINE not in text
