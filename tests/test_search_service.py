"""Search orchestration, caching and history.

These exercise the whole pipeline through the same entry point the bot uses.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.container import Container
from app.db.repository import SearchRepository
from app.domain.enums import ProviderName, ProviderStatus, Region, ScoreCategory, SearchType
from app.domain.identity import SearchSubject, VehicleDescriptor, parse_fio
from app.domain.models import ProviderResult
from app.providers.base import BaseProvider
from app.services.reporting import render_report
from app.services.search import _enrich_from_internal, build_query_hash

OPERATOR_ID = 111


def subject_for(fio: str, birth_date: date | None = None) -> SearchSubject:
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio(fio),
        birth_date=birth_date,
        regions=(Region.MOSCOW.value,),
    )


# ---------------------------------------------------------------- end to end


async def test_full_pipeline_produces_a_scored_report(container: Container) -> None:
    report = await container.search_service.search(
        subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12)),
        telegram_user_id=OPERATOR_ID,
    )

    assert report.recovery_score is not None
    assert 0 <= report.recovery_score.score <= 100
    assert report.provider_results
    assert report.internal_records  # matched against the demo CSV


async def test_demo_debtors_land_in_distinct_categories(container: Container) -> None:
    """The three fixtures are meant to span the score range; if they collapse
    into one band, the scoring rules have drifted."""
    high = await container.search_service.search(
        subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12)),
        telegram_user_id=OPERATOR_ID,
    )
    medium = await container.search_service.search(
        subject_for("Примеров Алексей Олегович", date(1979, 7, 24)),
        telegram_user_id=OPERATOR_ID,
    )
    low = await container.search_service.search(
        subject_for("Демов Максим Игоревич", date(1990, 11, 3)),
        telegram_user_id=OPERATOR_ID,
    )

    assert high.recovery_score.category == ScoreCategory.HIGH.value  # type: ignore[union-attr]
    assert medium.recovery_score.category == ScoreCategory.MEDIUM.value  # type: ignore[union-attr]
    assert low.recovery_score.category == ScoreCategory.LOW.value  # type: ignore[union-attr]
    assert high.recovery_score.score > medium.recovery_score.score  # type: ignore[union-attr]
    assert medium.recovery_score.score > low.recovery_score.score  # type: ignore[union-attr]


async def test_active_bankruptcy_appears_in_the_report(container: Container) -> None:
    report = await container.search_service.search(
        subject_for("Демов Максим Игоревич", date(1990, 11, 3)),
        telegram_user_id=OPERATOR_ID,
    )
    assert report.active_bankruptcies


async def test_search_is_persisted_to_history(container: Container) -> None:
    await container.search_service.search(
        subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12)),
        telegram_user_id=OPERATOR_ID,
    )
    async with container.database.session() as session:
        requests = await SearchRepository(session).recent_for_user(OPERATOR_ID)

    assert len(requests) == 1
    assert requests[0].search_type == SearchType.PERSON.value
    # The stored label is masked, never the raw query.
    assert requests[0].masked_query.startswith("Тестов А.")


async def test_history_is_scoped_to_the_operator(container: Container) -> None:
    await container.search_service.search(
        subject_for("Тестов Андрей Сергеевич"), telegram_user_id=111
    )
    async with container.database.session() as session:
        other = await SearchRepository(session).recent_for_user(222)
    assert other == []


async def test_provider_results_and_score_are_stored(container: Container) -> None:
    await container.search_service.search(
        subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12)),
        telegram_user_id=OPERATOR_ID,
    )
    async with container.database.session() as session:
        repo = SearchRepository(session)
        request = (await repo.recent_for_user(OPERATOR_ID))[0]
        results = await repo.results_for_request(request.id)
        stored_score = await repo.report_for_request(request.id)

    assert results
    assert stored_score is not None
    assert 0 <= stored_score.score <= 100


async def test_raw_responses_are_not_stored_by_default(container: Container) -> None:
    await container.search_service.search(
        subject_for("Тестов Андрей Сергеевич"), telegram_user_id=OPERATOR_ID
    )
    async with container.database.session() as session:
        repo = SearchRepository(session)
        request = (await repo.recent_for_user(OPERATOR_ID))[0]
        results = await repo.results_for_request(request.id)

    assert all(row.raw_response is None for row in results)


# ---------------------------------------------------------------- caching


async def test_second_identical_search_uses_the_cache(container: Container) -> None:
    subject = subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12))
    await container.search_service.search(subject, telegram_user_id=OPERATOR_ID)
    second = await container.search_service.search(subject, telegram_user_id=OPERATOR_ID)

    assert second.from_cache
    assert second.cached_at is not None
    # Внутренняя база опрашивается заново даже на кэше — она наша и бесплатная,
    # поэтому из проверки на кэш-попадание исключена.
    assert all(
        result.cache_hit
        for result in second.provider_results
        if result.provider is not ProviderName.INTERNAL
    )


async def test_force_refresh_bypasses_the_cache(container: Container) -> None:
    subject = subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12))
    await container.search_service.search(subject, telegram_user_id=OPERATOR_ID)
    fresh = await container.search_service.search(
        subject, telegram_user_id=OPERATOR_ID, force_refresh=True
    )
    assert not fresh.from_cache


async def test_cache_preserves_the_score(container: Container) -> None:
    subject = subject_for("Демов Максим Игоревич", date(1990, 11, 3))
    first = await container.search_service.search(subject, telegram_user_id=OPERATOR_ID)
    cached = await container.search_service.search(subject, telegram_user_id=OPERATOR_ID)

    assert cached.recovery_score is not None
    assert cached.recovery_score.score == first.recovery_score.score  # type: ignore[union-attr]


async def test_the_cache_is_never_kinder_than_the_answer_it_stores(
    container: Container,
) -> None:
    """Неполный ответ остаётся неполным и на следующий день.

    ФНП находит тринадцать уведомлений и не сопоставляет ни одного; отчёт
    говорит об этом, а скоринг не даёт плюса «залогов не найдено». Отчёт живёт в
    кэше сутки и пересобирается из базы — и если бы признак неполноты туда не
    попадал, пересобранный отчёт напечатал бы «залогов не найдено» и вернул
    снятый плюс. Тихая инверсия с отсрочкой в один запрос.
    """
    subject = subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12))
    await container.search_service.search(subject, telegram_user_id=OPERATOR_ID)

    async with container.database.session() as session:
        repo = SearchRepository(session)
        request = await repo.find_cached_request(build_query_hash(subject), ttl_hours=24)
        assert request is not None
        stored = await repo.results_for_request(request.id)
        row = next(row for row in stored if row.provider == ProviderName.PLEDGE.value)
        row.is_partial = True
        row.notes_json = '["В реестре ФНП найдено 13 уведомлений на это ФИО"]'
        await session.commit()

    cached = await container.search_service.search(subject, telegram_user_id=OPERATOR_ID)

    assert cached.from_cache
    result = cached.result_for(ProviderName.PLEDGE)
    assert result is not None
    assert result.is_partial
    assert result.notes == ("В реестре ФНП найдено 13 уведомлений на это ФИО",)

    text = render_report(cached)
    assert "Записей в реестре залогов не найдено" not in text
    assert "13 уведомлений" in text

    score = cached.recovery_score
    assert score is not None
    assert "no_pledges" not in {factor.name for factor in score.factors}


async def test_expired_cache_is_not_used(container: Container) -> None:
    from app.services.search import SearchService

    subject = subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12))
    await container.search_service.search(subject, telegram_user_id=OPERATOR_ID)

    expiring = SearchService(
        settings=container.settings.model_copy(update={"cache_ttl_hours": 0}),
        database=container.database,
        registry=container.registry,
    )
    result = await expiring.search(subject, telegram_user_id=OPERATOR_ID)
    assert not result.from_cache


async def test_different_subjects_do_not_share_a_cache_entry(
    container: Container,
) -> None:
    first = subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12))
    second = subject_for("Тестов Андрей Сергеевич", date(1990, 1, 1))
    assert build_query_hash(first) != build_query_hash(second)

    await container.search_service.search(first, telegram_user_id=OPERATOR_ID)
    result = await container.search_service.search(second, telegram_user_id=OPERATOR_ID)
    assert not result.from_cache


def test_query_hash_ignores_irrelevant_ordering() -> None:
    first = subject_for("Тестов Андрей Сергеевич").model_copy(
        update={"regions": (Region.MOSCOW.value, Region.MOSCOW_OBLAST.value)}
    )
    second = subject_for("Тестов Андрей Сергеевич").model_copy(
        update={"regions": (Region.MOSCOW_OBLAST.value, Region.MOSCOW.value)}
    )
    assert build_query_hash(first) == build_query_hash(second)


# ---------------------------------------------------------------- resilience


def _without(container: Container, name: ProviderName) -> list[BaseProvider]:
    """Registry contents with one provider removed, so a test double can take
    its place instead of shadowing it."""
    return [p for p in container.registry.external if p.name is not name]


class SlowProvider(BaseProvider):
    name = ProviderName.COURT

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        import asyncio

        await asyncio.sleep(30)
        raise AssertionError("should have been cancelled")  # pragma: no cover


class ExplodingProvider(BaseProvider):
    name = ProviderName.PROPERTY

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        raise RuntimeError("provider bug")


async def test_a_hanging_provider_does_not_block_the_report(
    container: Container,
) -> None:
    from app.providers.registry import ProviderRegistry
    from app.services.search import SearchService

    registry = ProviderRegistry(
        internal=container.registry.internal,
        external=[*_without(container, ProviderName.COURT), SlowProvider()],
    )
    service = SearchService(
        settings=container.settings.model_copy(update={"provider_budget_seconds": 1.0}),
        database=container.database,
        registry=registry,
    )
    report = await service.search(
        subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12)),
        telegram_user_id=OPERATOR_ID,
    )

    court = report.result_for(ProviderName.COURT)
    assert court is not None
    assert court.status is ProviderStatus.UNAVAILABLE
    # The rest of the report is intact.
    assert report.result_for(ProviderName.FSSP).is_answered  # type: ignore[union-attr]
    assert report.recovery_score is not None


async def test_duplicate_provider_names_are_rejected(container: Container) -> None:
    """Two providers under one name would make one of them unreadable."""
    from app.providers.registry import DuplicateProviderError, ProviderRegistry

    with pytest.raises(DuplicateProviderError):
        ProviderRegistry(
            internal=container.registry.internal,
            external=[*container.registry.external, SlowProvider()],
        )


async def test_a_crashing_provider_does_not_break_the_report(
    container: Container,
) -> None:
    from app.providers.registry import ProviderRegistry
    from app.services.search import SearchService

    registry = ProviderRegistry(
        internal=container.registry.internal,
        external=[*_without(container, ProviderName.PROPERTY), ExplodingProvider()],
    )
    service = SearchService(
        settings=container.settings, database=container.database, registry=registry
    )
    report = await service.search(
        subject_for("Тестов Андрей Сергеевич", date(1985, 3, 12)),
        telegram_user_id=OPERATOR_ID,
    )

    assert report.result_for(ProviderName.PROPERTY).status is ProviderStatus.ERROR  # type: ignore[union-attr]
    assert report.recovery_score is not None


# ---------------------------------------------------------------- internal lookup


async def test_contract_lookup_finds_the_internal_record(container: Container) -> None:
    subject = SearchSubject(
        search_type=SearchType.CONTRACT.value,
        contract_number="EV-20481",
        claim_number="EV-20481",
        debtor_id="EV-20481",
    )
    records = await container.search_service.lookup_internal(subject)

    assert records
    assert records[0].full_name == "Тестов Андрей Сергеевич"
    # Found by an exact identifier, so confidence is not reduced by a missing DOB.
    assert records[0].match_confidence >= 0.95


async def test_lookup_by_plate(container: Container) -> None:
    subject = SearchSubject(
        search_type=SearchType.VEHICLE_PLATE.value,
        vehicle=VehicleDescriptor(plate="А123ВС77"),
    )
    records = await container.search_service.lookup_internal(subject)
    assert records
    assert records[0].vehicle_plate == "А123ВС77"


async def test_lookup_by_vin(container: Container) -> None:
    subject = SearchSubject(
        search_type=SearchType.VIN.value,
        vehicle=VehicleDescriptor(vin="XW8ZZZ61ZKG011111"),
    )
    records = await container.search_service.lookup_internal(subject)
    assert records
    assert records[0].vin == "XW8ZZZ61ZKG011111"


async def test_unknown_contract_returns_nothing(container: Container) -> None:
    subject = SearchSubject(search_type=SearchType.CONTRACT.value, contract_number="НЕТ-ТАКОГО")
    assert await container.search_service.lookup_internal(subject) == []


async def test_internal_records_are_deduplicated_across_sources(
    container: Container,
) -> None:
    """The CSV file and its imported copy in the database are one debtor."""
    await container.import_service.import_file(container.settings.internal_csv_path)
    subject = SearchSubject(search_type=SearchType.CONTRACT.value, contract_number="EV-20481")
    records = await container.search_service.lookup_internal(subject)
    assert len(records) == 1


@pytest.mark.parametrize(
    "search_type", [SearchType.VEHICLE_PLATE, SearchType.VIN, SearchType.ADDRESS]
)
async def test_vehicle_and_address_searches_report_unconnected_sources(
    container: Container, search_type: SearchType
) -> None:
    """No lawful provider is wired for these, and the report must say so rather
    than returning an empty "clean" result."""
    subject = SearchSubject(
        search_type=search_type.value,
        vehicle=VehicleDescriptor(plate="А123ВС77", vin="XW8ZZZ61ZKG011111"),
        address="Москва",
    )
    report = await container.search_service.search(subject, telegram_user_id=OPERATOR_ID)
    vehicle_result = report.result_for(ProviderName.VEHICLE)
    assert vehicle_result is not None
    assert vehicle_result.status is ProviderStatus.NOT_CONFIGURED


async def test_a_phone_pulls_the_name_from_our_export(container: Container) -> None:
    """Оператор помнит только номер — и этого должно хватить.

    Заказчик ведёт должников в 1С, в выгрузке рядом с телефоном лежат ФИО и
    дата рождения. Без переноса их в запрос ФССП и залоги отвечали бы
    «недостаточно данных» о человеке, которого мы только что нашли у себя.
    """
    from app.domain.enums import SearchType

    only_phone = SearchSubject(search_type=SearchType.PERSON.value, phone="+7 (999) 123-45-01")
    records, result, exact = await container.search_service.lookup_internal_result(only_phone)

    assert result.status is ProviderStatus.SUCCESS
    assert exact, "телефон — точный идентификатор, а не совпадение по имени"

    enriched = _enrich_from_internal(only_phone, records, exact=exact)
    assert enriched.name is not None, "ФИО должно подтянуться из выгрузки"
    assert enriched.birth_date is not None, "дата рождения тоже: без неё ФССП не ищет"


async def test_a_namesake_hit_does_not_borrow_a_birth_date(container: Container) -> None:
    """Совпадение по одному имени датой рождения не дополняется.

    Подставив дату однофамильца, мы получили бы чужие производства, показанные
    как производства должника, и не отличили бы их потом ничем.
    """
    from app.domain.identity import parse_fio

    by_name = SearchSubject(search_type="person", name=parse_fio("Тестов Андрей Сергеевич"))
    records, _result, exact = await container.search_service.lookup_internal_result(by_name)

    assert not exact, "поиск по имени — не точный идентификатор"
    assert _enrich_from_internal(by_name, records, exact=exact).birth_date is None


async def test_an_inn_from_the_export_opens_three_more_sources(container: Container) -> None:
    """ИНН из выгрузки переносится в запрос — ради этого колонка и заведена.

    Банкротство, статус ИП и арбитраж ищут ТОЛЬКО по ИНН. Если он лежит в 1С
    рядом с телефоном, оператору достаточно ввести номер: три источника
    откроются сами, и платный запрос ИНН по паспорту не понадобится.
    """
    from app.domain.models import InternalDebtorRecord

    record = InternalDebtorRecord(debtor_id="DEM-001", inn="770912345601")
    subject = SearchSubject(search_type="person", phone="+79991234501")

    enriched = _enrich_from_internal(subject, [record], exact=True)

    assert enriched.inn == "770912345601"


def test_a_ten_digit_inn_never_reaches_the_card() -> None:
    """Десятизначный ИНН — юрлица, и в проверке человека ему делать нечего.

    Источники отвергли бы такой запрос целиком, а оператор увидел бы «не
    проверено» без объяснимой причины. Строка выгрузки при этом не теряется:
    она приходит с замечанием, которое видно при импорте.
    """
    from app.providers.internal.csv_schema import DebtorRow, iter_rows

    parsed = [row for _number, row in iter_rows("fio,inn\nТестов Андрей Сергеевич,7709123456\n")]
    row = parsed[0]
    assert isinstance(row, DebtorRow)
    assert row.inn is None
    assert any("inn" in warning for warning in row.warnings)
