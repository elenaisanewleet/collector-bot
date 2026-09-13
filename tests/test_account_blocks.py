"""Блокировки счетов ФНС.

Единственный законный ответ на «счета в банках» из ТЗ, и раздел, в котором проще
всего соврать: стоит поставить находку выше отказа — и отчёт пообещает остатки,
которых не покажет никогда.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import Settings
from app.domain.enums import ProviderName, ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import AccountBlockRecord, DebtorReport
from app.providers.account_block import NEWDB_METHOD, NewDBAccountBlockProvider
from app.providers.newdb import NewDBFieldMaps
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

ROW = {
    "DATASTART": "03.03.2022",
    "NOMER": "990",
    "DATA": "03.03.2022",
    "KODOSNOV": "02",
    "SALDO": "(Отсутствует значение)",
    "IFNS": "1326",
    "BIK": "048952615",
    "DATABI": "06.03.2022 04:06:09",
}


def envelope(data: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "state": "complete",
        "requestId": "00000000-0000-4000-8000-000000000001",
        "results": {NEWDB_METHOD: {"result": {"status": 200, "data": list(data or [])}}},
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
async def test_the_bank_is_read_from_the_decision(settings: Settings, maps: NewDBFieldMaps) -> None:
    """БИК — содержание записи: ради него источник и подключён."""
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=envelope([ROW])))

    result = await NewDBAccountBlockProvider(settings, maps).fetch(SUBJECT)

    assert result.status is ProviderStatus.SUCCESS
    record = result.records[0]
    assert isinstance(record, AccountBlockRecord)
    assert record.bank_bic == "048952615"
    assert record.decision_number == "990"
    assert record.decision_date == date(2022, 3, 3)
    assert record.started_at == date(2022, 3, 3)


@respx.mock
async def test_the_saldo_never_becomes_a_balance(settings: Settings, maps: NewDBFieldMaps) -> None:
    """SALDO картой не берётся, и это не забывчивость.

    Живьём там встречается «(Отсутствует значение)» — то есть признак
    отсутствия суммы, а не сумма. Показать это как остаток по счёту значило бы
    напечатать в отчёте то, чего источник не знает и знать не может: остатки —
    банковская тайна.
    """
    respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=envelope([ROW])))

    result = await NewDBAccountBlockProvider(settings, maps).fetch(SUBJECT)

    assert "Отсутствует значение" not in str(result.records[0].model_dump())
    assert not hasattr(result.records[0], "balance")


async def test_without_an_inn_the_source_is_not_queried(
    settings: Settings, maps: NewDBFieldMaps
) -> None:
    """Без ИНН — «не проверено», а не «блокировок не найдено».

    Источник ищет по ИНН физлица, которого в выгрузке нет ни у кого: до него
    добирается цепочка мостов. Пустой ответ на непосланный запрос прочитался бы
    как «счета чистые».
    """
    result = await NewDBAccountBlockProvider(settings, maps).fetch(
        SUBJECT.model_copy(update={"inn": None})
    )

    assert result.error_code == "insufficient_query"
    assert not result.status.is_answered


def _report_with(*records: AccountBlockRecord) -> DebtorReport:
    from app.domain.models import ProviderResult

    result = ProviderResult(
        provider=ProviderName.ACCOUNT_BLOCK,
        status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
        records=list(records),
    )
    report = Aggregator().build(SUBJECT, [result])
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


def test_the_refusal_about_balances_survives_the_finding() -> None:
    """Находка встаёт ПОД отказом, а не вместо него.

    Это главная проверка раздела. Остатки — банковская тайна, и так будет
    всегда; блокировки говорят только о том, в каком банке счёт есть. Раздел,
    начавшийся с находки, пообещал бы счета и оговорился бы мелким шрифтом.
    """
    report = _report_with(AccountBlockRecord(bank_bic="048952615", decision_number="990"))

    text = reporting._bank_block(report)

    assert reporting.BANK_NO_SOURCE_LINE in text
    assert reporting.BANK_ACCESS_LINE in text
    assert text.index(reporting.BANK_NO_SOURCE_LINE) < text.index("048952615")


def test_nothing_found_is_said_out_loud() -> None:
    """«Блокировок нет» печатается словами.

    Раздел начинается с отказа, и без этой строки читающий не отличит «ФНС
    счета не блокировала» от «мы и это не смотрели» — а весь смысл раздела в
    том, чтобы эти два случая различать.
    """
    text = reporting._bank_block(_report_with())

    assert reporting.BANK_NO_BLOCKS_LINE in text


def test_a_block_is_both_good_news_and_bad_news() -> None:
    """Два фактора на одну запись, и оба видны оператору.

    Счёт найден — редкий случай, когда отчёт подтверждает наличие имущества, а
    не его отсутствие. И одновременно ФНС стоит в очереди впереди. Усреднить их
    в одно число значило бы скрыть от оператора первую половину.
    """
    report = _report_with(AccountBlockRecord(bank_bic="048952615", decision_number="990"))
    assert report.recovery_score is not None
    names = {factor.name for factor in report.recovery_score.factors}

    assert "bank_account_found" in names
    assert "bank_account_blocked" in names


def test_an_empty_answer_earns_no_bonus() -> None:
    """За «блокировок нет» баллов не начисляется.

    Отсутствие решения ФНС не говорит о наличии счёта ничего: у большинства
    людей счета есть и не заблокированы.
    """
    report = _report_with()
    assert report.recovery_score is not None
    names = {factor.name for factor in report.recovery_score.factors}

    assert "bank_account_found" not in names
    assert "bank_account_blocked" not in names
