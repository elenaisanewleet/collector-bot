"""Мост «паспорт → ИНН» (метод NewDB ``passport_fns``).

Все тесты здесь проверяют одно и то же с разных сторон: полученный ИНН
открывает три источника, а **любой** другой исход — пустой ответ ФНС,
отклонённый ключ, чужая схема, выключенный флаг — оставляет их честно
непроверенными и никогда не превращается в «записей нет».

Ключа NewDB у проекта нет, живой ответ ``passport_fns`` никем не видан, поэтому
контракт здесь зафиксирован по документации и проверяется на замоканном HTTP:
счётчик обращений на маршруте — это и есть доказательство, что ноль вызовов
означает ноль списаний.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from app.config import AppMode, FedresursBackend, FNSBackend, Settings
from app.db.repository import SearchRepository
from app.db.session import Database
from app.domain.enums import (
    BankruptcyStatus,
    ProviderName,
    ProviderStatus,
    Region,
    SearchType,
)
from app.domain.identity import PersonName, SearchSubject, parse_fio
from app.domain.models import BankruptcyRecord, DebtorReport, ProviderResult
from app.providers.base import BaseProvider
from app.providers.court import NewDBArbitrationProvider
from app.providers.fedresurs import NewDBBankruptcyProvider
from app.providers.fns import NewDBBusinessProvider
from app.providers.identity_bridge import (
    InnBridgeProvider,
    InnBridgeResult,
    PassportInnProvider,
)
from app.providers.newdb import NewDBFieldMaps, scrub_passport
from app.providers.registry import (
    DuplicateProviderError,
    ProviderRegistry,
    build_inn_bridge,
    build_internal_provider,
)
from app.services.reporting import render_report
from app.services.scoring import RecoveryScoreEngine
from app.services.search import SearchService, build_query_hash

BASE_URL = "https://api.example.test"
NEWDB_URL = f"{BASE_URL}/v2"
OPERATOR_ID = 111

PASSPORT = "4015350278"
SERIA = "4015"
NUMBER = "350278"
INN = "272116001938"
OTHER_INN = "111111111111"

# Три метода, которые ищут только по ИНН и ради которых мост существует.
FIELD_MAP: dict[str, Any] = {
    "bankrot_person": {
        "fields": {
            "debtor_name": "Debtor",
            "inn": "INN",
            "case_number": "CaseNumber",
            "procedure": "Procedure",
            "status": "ProcedureStatus",
        }
    },
    "egrul_ip": {"fields": {"inn": "INN", "name": "Name", "status": "Status"}},
    "arbitr_person": {"fields": {"case_number": "CaseNumber", "inn": "INN"}},
}


# ---------------------------------------------------------------- фикстуры


@pytest.fixture
def field_map_path(tmp_path: Path) -> Path:
    path = tmp_path / "newdb.json"
    path.write_text(json.dumps(FIELD_MAP, ensure_ascii=False), encoding="utf-8")
    return path


@pytest.fixture
def bridge_settings(live_settings: Settings, field_map_path: Path) -> Settings:
    """Деплой с ключом NewDB и явно включённым мостом."""
    return live_settings.model_copy(
        update={
            "newdb_api_key": "test-key",
            "newdb_base_url": BASE_URL,
            "newdb_method_path": "/v2",
            "newdb_poll_attempts": 2,
            "newdb_poll_interval_seconds": 0.01,
            "newdb_field_map": field_map_path,
            "provider_max_retries": 0,
            "provider_retry_backoff_seconds": 0.0,
            "inn_bridge_enabled": True,
            "fedresurs_backend": FedresursBackend.NEWDB,
            "fns_provider": FNSBackend.NEWDB,
        }
    )


@pytest.fixture
def passport_subject() -> SearchSubject:
    """ФИО, дата рождения и паспорт — то, что есть у взыскателя в договоре."""
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=PersonName(last_name="Малина", first_name="Александр", middle_name="Сергеевич"),
        birth_date=date(1990, 12, 17),
        passport=PASSPORT,
        regions=(Region.MOSCOW.value,),
    )


def envelope(
    *,
    section: str = "company",
    state: str = "complete",
    data: Sequence[Mapping[str, Any]] | None = None,
    errors_info: Sequence[Mapping[str, Any]] | None = None,
    echo_params: bool = False,
) -> dict[str, Any]:
    """Конверт NewDB в форме, описанной страницей ``passport_fns``.

    Секция называется ``company``, а не именем метода, — единственный известный
    метод, у которого они расходятся.
    """
    params: dict[str, Any] = {"method": "passport_fns", "country": "ru"}
    if echo_params:
        # Вендор возвращает присланные параметры эхом — вместе с паспортом.
        params |= {"seria": SERIA, "number": NUMBER, "lastname": "Малина"}
    payload: dict[str, Any] = {
        "params": params,
        "requestId": "00000000-0000-4000-8000-000000000001",
        "state": state,
    }
    if errors_info is not None:
        payload["errors_info"] = list(errors_info)
    if state == "complete":
        payload["results"] = {
            section: {"result": {"status": 200, "data": list(data) if data is not None else []}}
        }
    return payload


def route(response: dict[str, Any]) -> respx.Route:
    return respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json=response))


# ---------------------------------------------------------------- транспорт


@respx.mock
async def test_inn_is_read_from_the_company_section(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-1. Ответ с ИНН — успех, и ИНН доезжает до вызывающего."""
    route(envelope(data=[{"innfiz": INN}]))

    result = await PassportInnProvider(bridge_settings).fetch(passport_subject)

    assert result.status is ProviderStatus.SUCCESS
    assert isinstance(result, InnBridgeResult)
    assert result.inn == INN


@respx.mock
async def test_the_method_named_section_is_not_read(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-2. Регресс на ``results.<method>``.

    Если однажды кто-то «поправит» секцию на имя метода, ответ перестанет
    читаться — и это должно быть ``unexpected_schema``, а не молчаливый успех.
    """
    route(envelope(section="passport_fns", data=[{"innfiz": INN}]))

    result = await PassportInnProvider(bridge_settings).fetch(passport_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"
    assert not isinstance(result, InnBridgeResult)


@respx.mock
async def test_empty_data_is_an_answer_and_leaves_three_sources_unchecked(
    bridge_settings: Settings, database: Database, passport_subject: SearchSubject
) -> None:
    """T-3. Пустой ``data[]`` — это ответ ФНС, а не сбой.

    И при этом ни один из трёх зависимых источников не отвечает ``NO_RESULTS``:
    их никто не спрашивал.
    """
    route(envelope(data=[]))
    service = _service(bridge_settings, database)

    report = await service.search(passport_subject, telegram_user_id=OPERATOR_ID)

    bridge = report.result_for(ProviderName.INN_BRIDGE)
    assert bridge is not None
    assert bridge.status is ProviderStatus.NO_RESULTS
    _assert_three_sources_unchecked(report)


@respx.mock
async def test_a_rejected_key_is_an_error_not_an_absent_inn(
    bridge_settings: Settings, database: Database, passport_subject: SearchSubject
) -> None:
    """T-4. HTTP 200 + ``state: failed`` — отклонённый ключ, а не «ИНН нет»."""
    route(
        envelope(
            state="failed",
            errors_info=[{"error": "Неверный токен X-API-KEY", "error_code": 401}],
        )
    )
    service = _service(bridge_settings, database)

    report = await service.search(passport_subject, telegram_user_id=OPERATOR_ID)

    bridge = report.result_for(ProviderName.INN_BRIDGE)
    assert bridge is not None
    assert bridge.status is ProviderStatus.ERROR
    assert bridge.error_code == "unauthorized"
    assert not isinstance(bridge, InnBridgeResult) or bridge.inn is None
    _assert_three_sources_unchecked(report)


@respx.mock
async def test_several_different_inns_are_refused(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-5. Чужой ИНН подошьёт должнику чужое банкротство — брать первый нельзя."""
    route(envelope(data=[{"innfiz": OTHER_INN}, {"innfiz": "222222222222"}]))

    result = await PassportInnProvider(bridge_settings).fetch(passport_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "ambiguous_identity"
    assert not isinstance(result, InnBridgeResult)


@respx.mock
async def test_the_same_inn_twice_is_one_answer(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-6. Дубль строки — не неоднозначность."""
    route(envelope(data=[{"innfiz": OTHER_INN}, {"innfiz": OTHER_INN}]))

    result = await PassportInnProvider(bridge_settings).fetch(passport_subject)

    assert result.status is ProviderStatus.SUCCESS
    assert isinstance(result, InnBridgeResult)
    assert result.inn == OTHER_INN


@respx.mock
async def test_a_ten_digit_inn_is_refused(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-7. Пример из fiz_02 отдаёт десять цифр — это ИНН юрлица.

    Отдать его дальше значило бы купить у трёх источников гарантированно
    отклонённый и всё равно оплаченный вызов.
    """
    route(envelope(data=[{"innfiz": "7703245603"}]))

    result = await PassportInnProvider(bridge_settings).fetch(passport_subject)

    assert result.status is ProviderStatus.ERROR
    assert result.error_code == "unexpected_inn_length"
    assert not isinstance(result, InnBridgeResult)


@respx.mock
async def test_rows_without_innfiz_are_a_schema_problem(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-8. Строки есть, поля нет — это про схему, а не про отсутствие ИНН."""
    route(envelope(data=[{"foo": "bar"}]))

    result = await PassportInnProvider(bridge_settings).fetch(passport_subject)

    assert result.status is ProviderStatus.UNAVAILABLE
    assert result.error_code == "unexpected_schema"


@respx.mock
async def test_the_passport_is_split_into_seria_and_number(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-9. Четыре цифры серии и шесть номера, плюс обычный блок персоны."""
    sent = route(envelope(data=[{"innfiz": INN}]))

    await PassportInnProvider(bridge_settings).fetch(passport_subject)

    params = json.loads(sent.calls[0].request.content)["params"]
    assert params["method"] == "passport_fns"
    assert params["seria"] == SERIA
    assert params["number"] == NUMBER
    assert params["lastname"] == "Малина"
    assert params["firstname"] == "Александр"
    assert params["secondname"] == "Сергеевич"
    assert params["dob"] == "1990-12-17"
    assert params["country"] == "ru"


# ---------------------------------------------------------------- политика вызова


@respx.mock
async def test_a_known_individual_inn_means_no_call_and_no_line(
    bridge_settings: Settings, database: Database, passport_subject: SearchSubject
) -> None:
    """T-10. ИНН уже есть — моста в отчёте нет вовсе: объяснять нечего."""
    calls = route(envelope(data=[{"innfiz": INN}]))
    service = _service(bridge_settings, database)
    subject = passport_subject.model_copy(update={"inn": INN})

    report = await service.search(subject, telegram_user_id=OPERATOR_ID)

    assert report.result_for(ProviderName.INN_BRIDGE) is None
    assert not any(
        json.loads(call.request.content)["params"]["method"] == "passport_fns"
        for call in calls.calls
    )


@respx.mock
async def test_a_ten_digit_subject_inn_still_needs_the_bridge(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-11. Десятизначный ИНН — юрлица; три источника его отвергнут."""
    sent = route(envelope(data=[{"innfiz": INN}]))
    subject = passport_subject.model_copy(update={"inn": "7703245603"})
    provider = PassportInnProvider(bridge_settings)

    assert provider.is_needed(subject)
    result = await provider.fetch(subject)

    assert result.status is ProviderStatus.SUCCESS
    assert sent.call_count == 1


@respx.mock
async def test_the_flag_off_means_not_configured_and_no_calls(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-12. Платный вызов не включается сам собой."""
    sent = route(envelope(data=[{"innfiz": INN}]))
    settings = bridge_settings.model_copy(update={"inn_bridge_enabled": False})

    result = await PassportInnProvider(settings).fetch(passport_subject)

    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert sent.call_count == 0


@respx.mock
async def test_no_newdb_key_means_not_configured_and_no_calls(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-13. Ключа у проекта нет — это состояние обязано быть безопасным."""
    sent = route(envelope(data=[{"innfiz": INN}]))
    settings = bridge_settings.model_copy(update={"newdb_api_key": ""})

    result = await PassportInnProvider(settings).fetch(passport_subject)

    assert result.status is ProviderStatus.NOT_CONFIGURED
    assert sent.call_count == 0


@respx.mock
async def test_a_passport_only_subject_never_reaches_the_network(
    bridge_settings: Settings,
) -> None:
    """T-14. Запирает правдивость ASK_PASSPORT в ``search_misc``.

    Флоу поиска по паспорту строит субъект без ФИО и без даты рождения. Мост
    обязан отказать на них, не сделав ни одного обращения, — иначе обещание «не
    передаётся во внешние источники» на том экране станет ложью.
    """
    sent = route(envelope(data=[{"innfiz": INN}]))
    subject = SearchSubject(search_type=SearchType.PASSPORT.value, passport=PASSPORT)

    result = await PassportInnProvider(bridge_settings).fetch(subject)

    assert result.error_code == "insufficient_query"
    assert sent.call_count == 0


@respx.mock
async def test_a_missing_birth_date_is_refused_before_the_call(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-15. ФНС требует дату рождения; сказать это честнее, чем купить отказ."""
    sent = route(envelope(data=[{"innfiz": INN}]))
    subject = passport_subject.model_copy(update={"birth_date": None})

    result = await PassportInnProvider(bridge_settings).fetch(subject)

    assert result.error_code == "insufficient_query"
    assert sent.call_count == 0


@respx.mock
async def test_a_missing_patronymic_still_asks_and_says_so(
    bridge_settings: Settings, passport_subject: SearchSubject
) -> None:
    """T-16. Без отчества вызов делается, но «не найдено» не выдаётся за итог."""
    sent = route(envelope(data=[]))
    subject = passport_subject.model_copy(
        update={"name": PersonName(last_name="Малина", first_name="Александр")}
    )

    result = await PassportInnProvider(bridge_settings).fetch(subject)

    assert sent.call_count == 1
    assert result.status is ProviderStatus.NO_RESULTS
    assert result.error_message is not None
    assert "отчество" in result.error_message
    assert "отчество" in _source_line_for(result)


@respx.mock
async def test_demo_mode_has_a_bridge_that_never_talks(
    settings: Settings, database: Database, passport_subject: SearchSubject
) -> None:
    """T-17. Демо обязано работать без ключа и без сети.

    ТЗ предполагало отсутствие моста в демо; вместо этого он детерминированный —
    иначе паспортный шаг некому показать, а ``make demo`` перестал бы объяснять,
    откуда берётся ИНН.
    """
    assert settings.app_mode is AppMode.DEMO
    sent = respx.post(NEWDB_URL).mock(return_value=httpx.Response(200, json={}))
    bridge = build_inn_bridge(settings)

    demo_subject = passport_subject.model_copy(
        update={"name": parse_fio("Тестов Андрей Сергеевич"), "birth_date": date(1985, 3, 12)}
    )
    result = await bridge.fetch(demo_subject)

    assert result.status is ProviderStatus.SUCCESS
    assert isinstance(result, InnBridgeResult)
    assert result.inn == "770912345601"
    assert sent.call_count == 0
    # Без паспорта демо-моста для субъекта не существует: демо-источники ищут по
    # ФИО, и строка «паспорт не указан» объясняла бы то, чего не происходит.
    assert not bridge.is_needed(demo_subject.model_copy(update={"passport": None}))


# ---------------------------------------------------------------- кэш


def test_the_passport_changes_the_cache_key(passport_subject: SearchSubject) -> None:
    """T-18. Поиск с паспортом — другой вопрос, чем поиск без него."""
    without = passport_subject.model_copy(update={"passport": None})
    assert build_query_hash(passport_subject) != build_query_hash(without)


def test_two_different_passports_do_not_share_a_cache_entry() -> None:
    """T-19. Регресс на живой баг, существовавший до всякого моста.

    У ``SearchType.PASSPORT`` все прочие поля ``None``, поэтому без паспорта в
    ключе **любые** два поиска по паспорту делили одну запись кэша, и второй
    оператор получал чужой отчёт с пометкой «из кэша».
    """
    first = SearchSubject(search_type=SearchType.PASSPORT.value, passport=PASSPORT)
    second = SearchSubject(search_type=SearchType.PASSPORT.value, passport="4509123456")
    assert build_query_hash(first) != build_query_hash(second)


async def test_a_found_bankruptcy_survives_the_second_showing(
    live_settings: Settings, database: Database
) -> None:
    """T-20. Ключевой: найденное не исчезает при повторном открытии отчёта.

    Первый показ обогащает субъект ИНН, и банкротство подтверждается по нему.
    Второй строится по входящему субъекту, у которого ИНН нет (оператор снова
    ввёл ФИО, дату рождения и паспорт), а записи приходят из БД — уже с ИНН.
    Без восстановления ИНН из ``subject_json`` совпадение падало бы с 0.90 до
    0.50 и запись выпадала бы из отчёта.
    """
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio("Иванов Иван Иванович"),
        birth_date=date(1980, 6, 1),
        passport=PASSPORT,
    )
    service = _stubbed_service(live_settings, database)

    first = await service.search(subject, telegram_user_id=OPERATOR_ID)
    assert first.active_bankruptcies
    assert first.active_bankruptcies[0].is_confirmed

    second = await service.search(subject, telegram_user_id=OPERATOR_ID)

    assert second.from_cache
    assert second.active_bankruptcies, "банкротство исчезло при повторном показе"
    assert second.active_bankruptcies[0].is_usable
    assert second.recovery_score is not None and first.recovery_score is not None
    assert second.recovery_score.score == first.recovery_score.score


async def test_a_cache_hit_does_not_call_the_bridge(
    live_settings: Settings, database: Database
) -> None:
    """T-21. Кэш-хит — ноль обращений и ноль списаний."""
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio("Иванов Иван Иванович"),
        birth_date=date(1980, 6, 1),
        passport=PASSPORT,
    )
    bridge = _StubBridge()
    service = _stubbed_service(live_settings, database, bridge=bridge)

    await service.search(subject, telegram_user_id=OPERATOR_ID)
    assert bridge.calls == 1
    await service.search(subject, telegram_user_id=OPERATOR_ID)
    assert bridge.calls == 1


async def test_the_bridge_line_survives_the_cache(
    live_settings: Settings, database: Database
) -> None:
    """T-22. Объяснение переживает кэш вместе со строкой «не проверено»."""
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio("Иванов Иван Иванович"),
        birth_date=date(1980, 6, 1),
        passport=PASSPORT,
    )
    service = _stubbed_service(live_settings, database)

    first = await service.search(subject, telegram_user_id=OPERATOR_ID)
    second = await service.search(subject, telegram_user_id=OPERATOR_ID)

    restored = second.result_for(ProviderName.INN_BRIDGE)
    original = first.result_for(ProviderName.INN_BRIDGE)
    assert restored is not None and original is not None
    assert restored.status is original.status
    assert _source_line_for(restored) == _source_line_for(original)


# ---------------------------------------------------------------- устойчивость


async def test_a_hanging_bridge_does_not_take_the_search_with_it(
    live_settings: Settings, database: Database
) -> None:
    """Бюджет истёк на мосту — поиск продолжается, три источника «не проверено».

    Мост стоит в последовательной фазе перед внешней волной, поэтому зависший
    мост — единственное место, где одна медленная сеть могла бы задержать весь
    отчёт. Потолок у него тот же, что у остальных источников.
    """

    class _Hanging(InnBridgeProvider):
        @property
        def is_configured(self) -> bool:
            return True

        async def _fetch(self, subject: SearchSubject) -> ProviderResult:
            import asyncio

            await asyncio.sleep(30)
            raise AssertionError("должен был быть отменён")  # pragma: no cover

    settings = live_settings.model_copy(update={"request_timeout_seconds": 1.0})
    registry = ProviderRegistry(
        internal=build_internal_provider(settings, database),
        external=[_StubBankruptcy()],
        inn_bridge=_Hanging(),
    )
    service = SearchService(settings=settings, database=database, registry=registry)

    report = await service.search(
        SearchSubject(
            search_type=SearchType.PERSON.value,
            name=parse_fio("Иванов Иван Иванович"),
            birth_date=date(1980, 6, 1),
            passport=PASSPORT,
        ),
        telegram_user_id=OPERATOR_ID,
    )

    bridge = report.result_for(ProviderName.INN_BRIDGE)
    assert bridge is not None
    assert bridge.status is ProviderStatus.UNAVAILABLE
    assert bridge.error_code == "timeout"
    assert report.recovery_score is not None
    fedresurs = report.result_for(ProviderName.FEDRESURS)
    assert fedresurs is not None and fedresurs.error_code == "insufficient_query"


async def test_a_crashing_bridge_does_not_break_the_report(
    live_settings: Settings, database: Database
) -> None:
    """Баг в мосте ухудшает один раздел, а не превращает должника в «не проверен»."""

    class _Exploding(InnBridgeProvider):
        @property
        def is_configured(self) -> bool:
            return True

        async def _fetch(self, subject: SearchSubject) -> ProviderResult:
            raise RuntimeError("bridge bug")

    registry = ProviderRegistry(
        internal=build_internal_provider(live_settings, database),
        external=[_StubBankruptcy()],
        inn_bridge=_Exploding(),
    )
    service = SearchService(settings=live_settings, database=database, registry=registry)

    report = await service.search(
        SearchSubject(
            search_type=SearchType.PERSON.value,
            name=parse_fio("Иванов Иван Иванович"),
            birth_date=date(1980, 6, 1),
            passport=PASSPORT,
        ),
        telegram_user_id=OPERATOR_ID,
    )

    bridge = report.result_for(ProviderName.INN_BRIDGE)
    assert bridge is not None
    assert bridge.status is ProviderStatus.ERROR
    assert bridge.error_code == "unhandled_exception"
    assert report.recovery_score is not None


# ---------------------------------------------------------------- приватность


@respx.mock
async def test_the_passport_never_reaches_storage_or_the_report(
    bridge_settings: Settings, database: Database, passport_subject: SearchSubject
) -> None:
    """T-23. Даже при STORE_RAW_RESPONSES=true и эхе параметров в ответе."""
    route(envelope(data=[{"innfiz": INN}], echo_params=True))
    settings = bridge_settings.model_copy(update={"store_raw_responses": True})
    service = _service(settings, database)

    report = await service.search(passport_subject, telegram_user_id=OPERATOR_ID)

    bridge = report.result_for(ProviderName.INN_BRIDGE)
    assert bridge is not None
    assert bridge.raw_response is None

    async with database.session() as session:
        repo = SearchRepository(session)
        request = (await repo.recent_for_user(OPERATOR_ID))[0]
        rows = await repo.results_for_request(request.id)
        stored = json.dumps(
            [
                {
                    "provider": row.provider,
                    "raw": row.raw_response,
                    "normalized": row.normalized_json,
                    "error": row.error_message,
                }
                for row in rows
            ],
            ensure_ascii=False,
        )
        subject_json = request.subject_json
        masked_query = request.masked_query

    bridge_row = next(row for row in rows if row.provider == ProviderName.INN_BRIDGE.value)
    assert bridge_row.raw_response is None
    for haystack in (stored, subject_json, masked_query, render_report(report)):
        for needle in (PASSPORT, SERIA, NUMBER):
            assert needle not in haystack


@respx.mock
async def test_a_vendor_error_quoting_the_passport_is_scrubbed(
    bridge_settings: Settings, database: Database, passport_subject: SearchSubject
) -> None:
    """T-24. ``error_message`` пишется в БД мимо обоих флагов приватности."""
    route(
        envelope(
            state="failed",
            errors_info=[
                {"error": f"seria {PASSPORT} is not valid", "error_code": 400},
                {"error": "Неверный токен X-API-KEY"},
            ],
        )
    )
    service = _service(bridge_settings, database)

    report = await service.search(passport_subject, telegram_user_id=OPERATOR_ID)

    bridge = report.result_for(ProviderName.INN_BRIDGE)
    assert bridge is not None
    assert bridge.error_code == "unauthorized"
    assert bridge.error_message is not None
    for needle in (PASSPORT, SERIA, NUMBER):
        assert needle not in bridge.error_message

    async with database.session() as session:
        repo = SearchRepository(session)
        request = (await repo.recent_for_user(OPERATOR_ID))[0]
        rows = await repo.results_for_request(request.id)
    stored = next(row for row in rows if row.provider == ProviderName.INN_BRIDGE.value)
    assert stored.error_message is not None
    assert PASSPORT not in stored.error_message


def test_scrub_passport_hides_the_passport_and_spares_the_inn() -> None:
    """T-25. Юнит на скруббер."""
    text = f"seria {SERIA} number {NUMBER} glued {PASSPORT} inn {INN} entity 7703245603"
    scrubbed = scrub_passport(text, seria=SERIA, number=NUMBER)

    assert SERIA not in scrubbed
    assert NUMBER not in scrubbed
    assert PASSPORT not in scrubbed
    # ИНН — десять и двенадцать цифр, вне диапазона 4–6, и не портится.
    assert INN in scrubbed
    assert "7703245603" in scrubbed
    assert scrub_passport("", seria=SERIA, number=NUMBER) == ""
    # Любой другой четырёх-шестизначный прогон тоже уходит.
    assert "1234" not in scrub_passport("code 1234", seria=SERIA, number=NUMBER)


def test_log_redaction_reaches_nested_values() -> None:
    """T-26. Один ``logger.info(..., params=payload)`` не должен всё вывалить."""
    from app.logging_setup import _redact_sensitive

    event = {
        "event": "newdb.request",
        "payload": {"params": {"seria": SERIA, "number": NUMBER}, "method": "passport_fns"},
    }
    redacted = _redact_sensitive(None, "info", event)

    assert SERIA not in json.dumps(redacted, ensure_ascii=False)
    assert NUMBER not in json.dumps(redacted, ensure_ascii=False)


@respx.mock
async def test_the_derived_inn_is_stored_while_the_passport_is_not(
    bridge_settings: Settings, database: Database, passport_subject: SearchSubject
) -> None:
    """T-27. Условие работоспособности T-20 при STORE_SENSITIVE_IDENTIFIERS=false."""
    route(envelope(data=[{"innfiz": INN}]))
    assert bridge_settings.store_sensitive_identifiers is False
    service = _service(bridge_settings, database)

    await service.search(passport_subject, telegram_user_id=OPERATOR_ID)

    async with database.session() as session:
        request = (await SearchRepository(session).recent_for_user(OPERATOR_ID))[0]
    stored = json.loads(request.subject_json)
    assert stored.get("passport") is None
    assert stored["inn"] == INN


@respx.mock
async def test_the_report_never_prints_the_obtained_inn(
    bridge_settings: Settings, database: Database, passport_subject: SearchSubject
) -> None:
    """T-28. Ни полностью, ни маскированно — иначе два показа разойдутся."""
    route(envelope(data=[{"innfiz": INN}]))
    service = _service(bridge_settings, database)

    report = await service.search(passport_subject, telegram_user_id=OPERATOR_ID)
    text = render_report(report)

    assert INN not in text
    assert "27********38" not in text


# ---------------------------------------------------------------- отчёт и смета


def test_three_sections_explain_why_the_inn_is_missing() -> None:
    """T-29. Тексты для «ФНС не нашла» и «мост не отработал» различаются."""
    no_results = _rendered_with_bridge(
        ProviderResult(provider=ProviderName.INN_BRIDGE, status=ProviderStatus.NO_RESULTS)
    )
    unavailable = _rendered_with_bridge(
        ProviderResult(
            provider=ProviderName.INN_BRIDGE,
            status=ProviderStatus.UNAVAILABLE,
            error_code="poll_timeout",
        )
    )

    assert no_results.count("ФНС не нашла ИНН по паспорту.") == 3
    assert unavailable.count("Получить ИНН по паспорту не удалось (poll_timeout)") == 3
    assert "это не значит, что записей нет" in unavailable
    assert "ФНС не нашла" not in unavailable


def test_a_successful_bridge_line_has_no_record_count() -> None:
    """T-30. Общая ветка напечатала бы «0 зап.» для полученного ИНН."""
    line = _source_line_for(
        InnBridgeResult(provider=ProviderName.INN_BRIDGE, status=ProviderStatus.SUCCESS, inn=INN)
    )
    assert "зап." not in line
    assert line.startswith("✓ ИНН по паспорту (ФНС) — ИНН получен")


def test_the_bridge_does_not_change_report_confidence() -> None:
    """T-31. Мост не реестр фактов: покрытие от него не зависит."""
    engine = RecoveryScoreEngine()
    success = engine.evaluate(
        _report_with_bridge(
            ProviderResult(provider=ProviderName.INN_BRIDGE, status=ProviderStatus.SUCCESS)
        )
    )
    unavailable = engine.evaluate(
        _report_with_bridge(
            ProviderResult(
                provider=ProviderName.INN_BRIDGE,
                status=ProviderStatus.UNAVAILABLE,
                error_code="timeout",
            )
        )
    )
    assert success.confidence == unavailable.confidence


# ---------------------------------------------------------------- целостность


def test_the_persisted_bridge_name_round_trips() -> None:
    """T-34. ``_load_cached`` восстанавливает имя провайдера из строки."""
    assert ProviderName("inn_bridge") is ProviderName.INN_BRIDGE


async def test_the_bridge_registered_twice_is_rejected(
    settings: Settings, database: Database
) -> None:
    """T-35. Отчёт адресуется по имени: дубль означал бы нечитаемый источник."""
    bridge = build_inn_bridge(settings)
    with pytest.raises(DuplicateProviderError):
        ProviderRegistry(
            internal=build_internal_provider(settings, database),
            external=[bridge],
            inn_bridge=bridge,
        )


async def test_the_bridge_is_not_a_provider_multiplier(
    settings: Settings, database: Database
) -> None:
    """T-33. ``configured_names`` питает смету и блок ИСТОЧНИКИ."""
    registry = ProviderRegistry(
        internal=build_internal_provider(settings, database),
        external=[],
        inn_bridge=build_inn_bridge(settings),
    )
    assert ProviderName.INN_BRIDGE not in registry.configured_names
    assert list(registry) == []


# ---------------------------------------------------------------- helpers


def _service(settings: Settings, database: Database) -> SearchService:
    """Сервис с живым мостом и тремя источниками, которые ищут только по ИНН."""
    maps = NewDBFieldMaps.load(settings.newdb_field_map)
    registry = ProviderRegistry(
        internal=build_internal_provider(settings, database),
        external=[
            NewDBBankruptcyProvider(settings, maps),
            NewDBBusinessProvider(settings, maps),
            NewDBArbitrationProvider(settings, maps),
        ],
        inn_bridge=PassportInnProvider(settings),
    )
    return SearchService(settings=settings, database=database, registry=registry)


class _StubBridge(InnBridgeProvider):
    """Мост, который всегда отдаёт ИНН, и считает, сколько раз его звали."""

    def __init__(self, inn: str = INN) -> None:
        self._inn = inn
        self.calls = 0

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        self.calls += 1
        return InnBridgeResult(provider=self.name, status=ProviderStatus.SUCCESS, inn=self._inn)


class _StubBankruptcy(BaseProvider):
    """ЕФРСБ через ИНН: запись без даты рождения, с ИНН поиска.

    Ровно то, что кладёт в запись ``_searched_by_inn``: имя из строки ответа и
    ИНН, по которому искали. Дата рождения у метода отсутствует — на ней и
    ломался повторный показ.
    """

    name = ProviderName.FEDRESURS

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if subject.inn is None:
            return self.insufficient_query("Нужен ИНН физлица (12 цифр)")
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS,
            records=[
                BankruptcyRecord(
                    debtor_name="Иванов Иван Иванович",
                    inn=subject.inn,
                    case_number="А40-118472/2026",
                    procedure="Реализация имущества гражданина",
                    status=BankruptcyStatus.ACTIVE,
                )
            ],
        )


def _stubbed_service(
    settings: Settings, database: Database, *, bridge: InnBridgeProvider | None = None
) -> SearchService:
    registry = ProviderRegistry(
        internal=build_internal_provider(settings, database),
        external=[_StubBankruptcy()],
        inn_bridge=bridge or _StubBridge(),
    )
    return SearchService(settings=settings, database=database, registry=registry)


def _assert_three_sources_unchecked(report: DebtorReport) -> None:
    """Ни один из трёх источников не смеет сказать «записей нет»."""
    for provider in (ProviderName.FEDRESURS, ProviderName.FNS, ProviderName.COURT):
        result = report.result_for(provider)
        assert result is not None
        assert not result.status.is_answered, "«не проверено» прочиталось бы как «ничего нет»"
        assert result.status is ProviderStatus.ERROR
        assert result.error_code == "insufficient_query"


def _source_line_for(result: ProviderResult) -> str:
    from app.services.reporting import _source_line

    # Счётчик записей внутренней базы живёт в отчёте, а не в результате, поэтому
    # строка источника собирается по паре (отчёт, результат).
    return _source_line(_report_with_bridge(result), result)


def _report_with_bridge(bridge: ProviderResult) -> DebtorReport:
    subject = SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio("Иванов Иван Иванович"),
        birth_date=date(1980, 6, 1),
    )
    unchecked = [
        ProviderResult(
            provider=provider,
            status=ProviderStatus.ERROR,
            error_code="insufficient_query",
            error_message="Нужен ИНН физлица (12 цифр)",
        )
        for provider in (ProviderName.FEDRESURS, ProviderName.FNS, ProviderName.COURT)
    ]
    return DebtorReport(subject=subject, provider_results=[bridge, *unchecked])


def _rendered_with_bridge(bridge: ProviderResult) -> str:
    return render_report(_report_with_bridge(bridge))
