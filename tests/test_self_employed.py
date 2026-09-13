"""Самозанятость (НПД).

Источник подключён целиком, кроме карты полей: поставщик документирует запрос и
не документирует ответ. Пока карты нет, метод обязан честно молчать — и
проверяется здесь именно это молчание, а не выдуманный разбор.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.config import Settings
from app.domain.enums import ProviderName, ProviderStatus, SearchType
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import DebtorReport, ProviderResult, SelfEmployedRecord
from app.providers.mapping import FieldMap
from app.providers.newdb import MethodMap, NewDBFieldMaps
from app.providers.self_employed import NEWDB_METHOD, NewDBSelfEmployedProvider, to_self_employed
from app.services import reporting
from app.services.aggregation import Aggregator
from app.services.scoring import RecoveryScoreEngine

SUBJECT = SearchSubject(
    search_type=SearchType.PERSON.value,
    name=PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич"),
    birth_date=date(1985, 3, 12),
    inn="770912345601",
)


def test_the_map_was_written_from_a_live_answer() -> None:
    """Карта собрана по живому ответу, а не по документации.

    Формы ответа поставщик не описал вовсе, поэтому источник сначала вышел БЕЗ
    карты и честно молчал. 13.09.2026 сделан один живой вызов, и пути взяты из
    него. Соседний nalog_debt показал цену другого порядка действий: карта,
    написанная по примеру из спецификации, не прочитала ни одного поля и роняла
    источник на каждом должнике — платно и молча.

    Даты постановки на учёт среди путей нет намеренно: источник её не
    возвращает, а ``request_date`` — это дата запроса.
    """
    shipped = json.loads(Path("config/field_maps/example_newdb.json").read_text(encoding="utf-8"))

    fields = shipped[NEWDB_METHOD]["fields"]
    assert fields["is_active"] == "is_self_employed"
    assert "registered_at" not in fields


async def test_without_a_map_the_source_says_it_is_not_connected(
    live_settings: Settings,
) -> None:
    """Молчание честное: источник не притворяется, что смотрел.

    Это общее правило продукта — метод без описания отвечает NOT_CONFIGURED, — и
    здесь оно единственное, что отделяет «мы пока не умеем» от «самозанятым не
    является».
    """
    settings = live_settings.model_copy(
        update={"newdb_api_key": "k", "newdb_base_url": "https://newdb.example.test"}
    )
    provider = NewDBSelfEmployedProvider(settings, NewDBFieldMaps())

    assert not provider.is_configured

    result = await provider.fetch(SUBJECT)
    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert not result.status.is_answered


async def test_without_an_inn_the_source_is_not_queried(live_settings: Settings) -> None:
    settings = live_settings.model_copy(
        update={"newdb_api_key": "k", "newdb_base_url": "https://newdb.example.test"}
    )
    maps = NewDBFieldMaps({NEWDB_METHOD: _stub_map()})

    result = await NewDBSelfEmployedProvider(settings, maps).fetch(
        SUBJECT.model_copy(update={"inn": None})
    )

    assert result.error_code == "insufficient_query"


def _stub_map() -> MethodMap:
    """Заглушка карты: нужна только чтобы дойти до проверки ИНН.

    Настоящих путей здесь нет и быть не может — форму ответа поставщик не
    описал. Тест ниже проверяет ветку «нечем спросить», а не разбор.
    """
    return MethodMap(
        method=NEWDB_METHOD,
        field_map=FieldMap(fields={"is_active": "status", "registered_at": "date"}),
    )


def test_silence_about_the_status_is_not_a_denial() -> None:
    """``None`` и ``False`` — разные ответы, и разница стоит балл.

    За подтверждённый статус начисляется плюс. Прочитать «источник не сказал»
    как «не самозанятый» значило бы молча снять этот плюс у человека, чей доход
    мы просто не спросили.
    """
    assert to_self_employed({"is_active": None}) is None

    denied = to_self_employed({"is_active": "нет"})
    assert denied is not None
    assert denied.is_active is False


def _report_with(*records: SelfEmployedRecord) -> DebtorReport:
    result = ProviderResult(
        provider=ProviderName.SELF_EMPLOYED,
        status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
        records=list(records),
    )
    report = Aggregator().build(SUBJECT, [result])
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


def test_a_confirmed_status_is_a_bonus_and_a_section() -> None:
    report = _report_with(SelfEmployedRecord(is_active=True))
    assert report.recovery_score is not None

    names = {factor.name for factor in report.recovery_score.factors}
    assert "self_employed" in names

    text = reporting._self_employed_block(report)
    assert "профессиональный доход" in text


def test_not_being_self_employed_prints_nothing_and_costs_nothing() -> None:
    """Раздела нет, штрафа нет.

    «Самозанятым не является» верно для подавляющего большинства людей: строка
    об этом под каждым должником была бы шумом, а штраф — наказанием за
    обычность.
    """
    report = _report_with(SelfEmployedRecord(is_active=False))
    assert report.recovery_score is not None

    assert reporting._self_employed_block(report) == ""
    assert "self_employed" not in {f.name for f in report.recovery_score.factors}
