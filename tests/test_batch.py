"""Массовая проверка выгрузки."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.container import Container
from app.db.repository import BatchRepository, DebtorRepository
from app.domain.verdict import Verdict
from app.services.batch import BatchProgress
from app.services.export import queue_to_csv

OPERATOR_ID = 111


@pytest.fixture
async def loaded(container: Container) -> Container:
    """Контейнер с загруженной демо-выгрузкой."""
    await container.import_service.import_file(container.settings.internal_csv_path)
    return container


# ---------------------------------------------------------------- estimate


async def test_estimate_counts_the_base(loaded: Container) -> None:
    estimate = await loaded.batch_service.estimate()

    assert estimate.debtors == 6
    assert estimate.to_query == 6
    assert estimate.cached == 0
    # Демо-режим подключает ФССП, ЕФРСБ, ФНС, залоги и арбитраж.
    assert estimate.providers_per_debtor == 5
    assert estimate.requests == 30


async def test_estimate_reuses_the_cache(loaded: Container) -> None:
    """Повторный прогон не оплачивает заново вчерашних должников."""
    await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    estimate = await loaded.batch_service.estimate()
    assert estimate.cached > 0
    assert estimate.to_query < estimate.debtors


async def test_estimate_respects_the_cap(loaded: Container) -> None:
    from app.services.batch import BatchService

    service = BatchService(
        settings=loaded.settings.model_copy(update={"batch_max_debtors": 2}),
        database=loaded.database,
        search_service=loaded.search_service,
    )
    estimate = await service.estimate()

    assert estimate.debtors == 2
    assert estimate.capped


async def test_estimate_on_an_empty_base(container: Container) -> None:
    estimate = await container.batch_service.estimate()
    assert estimate.debtors == 0
    assert estimate.requests == 0


async def test_the_bridge_adds_no_calls_to_a_batch_today(loaded: Container) -> None:
    """T-32/T-33. Паспортов в выгрузке нет — значит нет и вызовов моста.

    И это должно быть видно, а не молчаливо: у всех шести должников ИНН
    неизвестен, то есть банкротство, статус ИП и арбитраж по ним не будут
    проверены вовсе. Смета обязана сказать это до запуска.
    """
    from app.bot.handlers.batch import render_estimate

    estimate = await loaded.batch_service.estimate()

    assert estimate.bridge_calls == 0
    assert estimate.without_inn == 6
    # Мост не множитель: providers_per_debtor от него не изменился.
    assert estimate.providers_per_debtor == 5
    assert estimate.requests == 30

    text = render_estimate(estimate)
    assert "ИНН по паспорту: 0 вызовов" in text
    assert "проверены НЕ будут" in text


async def test_the_bridge_shows_up_as_its_own_line_when_it_will_fire(
    loaded: Container,
) -> None:
    """Смета печатает мост отдельной строкой, а не растворяет его в общем числе."""
    from app.bot.handlers.batch import render_estimate
    from app.services.batch import BatchEstimate

    estimate = BatchEstimate(
        debtors=800,
        cached=0,
        to_query=800,
        providers_per_debtor=5,
        capped=False,
        bridge_enabled=True,
        bridge_calls=800,
        without_inn=800,
    )
    text = render_estimate(estimate)

    assert estimate.requests == 800 * 5 + 800
    assert "ИНН по паспорту (ФНС): 800 вызовов — по одному на должника" in text


# ---------------------------------------------------------------- run


async def test_run_produces_a_verdict_for_everyone(loaded: Container) -> None:
    summary = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    assert summary.total == 6
    assert summary.processed == 6
    assert summary.failed == 0
    assert sum(summary.counts.values()) == 6


async def test_run_separates_the_bankrupt_from_the_collectable(loaded: Container) -> None:
    summary = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    # Демов в банкротстве — отсев; остальные с суммой долга идут в приказ.
    assert summary.count(Verdict.DROP) >= 1
    assert summary.count(Verdict.ORDER) >= 1


async def test_run_reports_the_avoided_fees(loaded: Container) -> None:
    """Главное число прогона: сколько не будет потрачено впустую."""
    summary = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)
    assert summary.saved_fees > 0


async def test_queue_is_ordered_by_verdict_then_amount(loaded: Container) -> None:
    """Деньги хранятся текстом, поэтому порядок должен идти по числовому ключу."""
    summary = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    async with loaded.database.session() as session:
        items = await BatchRepository(session).queue(summary.run_id, limit=50)

    orders = [item.verdict_order for item in items]
    assert orders == sorted(orders)

    within = [
        item.debt_amount or Decimal("0") for item in items if item.verdict == Verdict.ORDER.value
    ]
    assert within == sorted(within, reverse=True)


async def test_queue_can_be_filtered_by_verdict(loaded: Container) -> None:
    summary = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    async with loaded.database.session() as session:
        dropped = await BatchRepository(session).queue(
            summary.run_id, verdict=Verdict.DROP.value, limit=50
        )
    assert dropped
    assert all(item.verdict == Verdict.DROP.value for item in dropped)


async def test_progress_is_reported(loaded: Container) -> None:
    seen: list[BatchProgress] = []

    async def track(progress: BatchProgress) -> None:
        seen.append(progress)

    service = loaded.batch_service
    await service.run(telegram_user_id=OPERATOR_ID, progress=track)

    assert seen
    assert seen[-1].processed == seen[-1].total
    assert seen[-1].percent == 100


async def test_a_broken_row_does_not_abort_the_run(loaded: Container) -> None:
    """Строка без ФИО и без договора не должна ронять остальные."""
    from app.db.models import Debtor

    async with loaded.database.session() as session:
        await DebtorRepository(session).upsert(
            Debtor(dedup_key="broken-row", fio=None, contract_number=None)
        )

    summary = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    assert summary.total == 7
    assert summary.processed == 7
    assert summary.failed == 1
    # Остальные шесть всё равно получили вердикт.
    assert sum(summary.counts.values()) == 7


async def test_run_is_recorded_for_the_operator(loaded: Container) -> None:
    summary = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    async with loaded.database.session() as session:
        run = await BatchRepository(session).latest_run(OPERATOR_ID)
    assert run is not None
    assert run.id == summary.run_id
    assert run.status == "finished"


async def test_runs_are_scoped_to_the_operator(loaded: Container) -> None:
    await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    async with loaded.database.session() as session:
        other = await BatchRepository(session).latest_run(222)
    assert other is None


# ---------------------------------------------------------------- export


async def test_export_contains_every_row(loaded: Container) -> None:
    summary = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    async with loaded.database.session() as session:
        items = await BatchRepository(session).queue(summary.run_id, limit=100)
    payload = queue_to_csv(items)
    text = payload.decode("utf-8-sig")

    assert text.splitlines()[0].startswith("verdict;")
    assert len(text.strip().splitlines()) == len(items) + 1
    assert "Тестов Андрей Сергеевич" in text


async def test_export_masks_the_phone_by_default(loaded: Container) -> None:
    summary = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    async with loaded.database.session() as session:
        items = await BatchRepository(session).queue(summary.run_id, limit=100)
    text = queue_to_csv(items).decode("utf-8-sig")

    assert "+79991234501" not in text
    assert "***" in text


async def test_export_opens_in_excel() -> None:
    """BOM и точка с запятой — иначе Excel в русской локали ломает файл."""
    payload = queue_to_csv([])
    assert payload.startswith(b"\xef\xbb\xbf")
    assert b";" in payload
