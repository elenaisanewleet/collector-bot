"""Terminal demo of the whole pipeline.

Runs three fictional debtors — a high, a medium and a low recovery case — through
the exact code path the bot uses: internal lookup, external providers, identity
matching, aggregation, scoring, rendering. No token, no network, no credentials.

This is also the end-to-end smoke test: if this prints three reports with three
different score categories, the pipeline is wired correctly.
"""

from __future__ import annotations

from datetime import date

from app.config import AppMode, Settings, get_settings
from app.container import Container, build_container
from app.domain.enums import Region, SearchType
from app.domain.identity import SearchSubject, parse_fio
from app.logging_setup import configure_logging, get_logger
from app.services.import_service import ImportService
from app.services.reporting import render_internal_card, render_report

logger = get_logger(__name__)

DEMO_USER_ID = 0
SEPARATOR = "=" * 68

DEMO_SUBJECTS: tuple[tuple[str, str, date, tuple[str, ...]], ...] = (
    (
        "Высокая перспектива",
        "Тестов Андрей Сергеевич",
        date(1985, 3, 12),
        (Region.MOSCOW.value,),
    ),
    (
        "Средняя перспектива",
        "Примеров Алексей Олегович",
        date(1979, 7, 24),
        (Region.MOSCOW.value, Region.MOSCOW_OBLAST.value),
    ),
    (
        "Низкая перспектива / банкротство",
        "Демов Максим Игоревич",
        date(1990, 11, 3),
        (Region.MOSCOW.value,),
    ),
)


async def run_demo_flow(settings: Settings | None = None) -> int:
    resolved = settings or get_settings()
    if resolved.app_mode is not AppMode.DEMO:
        # Refuse to fabricate a demo against live providers.
        resolved = resolved.model_copy(update={"app_mode": AppMode.DEMO})

    configure_logging(resolved.log_level, json_output=resolved.log_json)
    container = build_container(resolved)
    await container.database.create_all()

    try:
        await _seed(container.import_service, resolved)
        print(f"\n{SEPARATOR}\n{resolved.app_name} — демонстрационный прогон\n{SEPARATOR}")

        for label, fio, birth_date, regions in DEMO_SUBJECTS:
            subject = SearchSubject(
                search_type=SearchType.PERSON.value,
                name=parse_fio(fio),
                birth_date=birth_date,
                regions=regions,
            )
            report = await container.search_service.search(
                subject, telegram_user_id=DEMO_USER_ID, force_refresh=True
            )
            print(f"\n{SEPARATOR}\n{label}\n{SEPARATOR}\n")
            print(render_report(report, demo_mode=True))

        await _demo_contract_lookup(container)
        await _demo_batch(container)
        print(f"\n{SEPARATOR}\nДемо завершено.\n{SEPARATOR}\n")
    finally:
        await container.dispose()
    return 0


async def _seed(import_service: ImportService, settings: Settings) -> None:
    path = settings.internal_csv_path
    if not path.is_file():
        print(f"CSV с демо-данными не найден: {path}")
        return
    report = await import_service.import_file(path)
    print(
        f"Загружено из {path}: строк {report.total_rows}, "
        f"импортировано {report.imported}, ошибок {report.failed}"
    )


async def _demo_batch(container: Container) -> None:
    """Главный сценарий: прогон всей выгрузки и очередь по вердиктам."""
    from app.bot.handlers.batch import render_estimate, render_summary
    from app.db.repository import BatchRepository
    from app.domain.verdict import VERDICT_TITLES, Verdict
    from app.utils.money import format_amount

    print(f"\n{SEPARATOR}\nМассовая проверка всей выгрузки\n{SEPARATOR}\n")
    estimate = await container.batch_service.estimate()
    print(render_estimate(estimate, container.settings.app_name))

    summary = await container.batch_service.run(telegram_user_id=DEMO_USER_ID)
    print(f"\n{render_summary(summary)}\n")

    async with container.database.session() as session:
        items = await BatchRepository(session).queue(summary.run_id, limit=50)

    print("ОЧЕРЕДЬ ВЗЫСКАНИЯ")
    for item in items:
        debtor = item.debtor
        name = (debtor.fio if debtor else None) or "—"
        title = VERDICT_TITLES.get(Verdict(item.verdict), item.verdict)
        fee = f", пошлина {format_amount(item.state_fee)}" if item.state_fee else ""
        print(f"  {title:18s} {name:30s} {format_amount(item.debt_amount)}{fee}")
        print(f"  {'':18s} {item.headline}")


async def _demo_contract_lookup(container: Container) -> None:
    """Shows the contract flow: an internal hit promoted into a full check."""
    subject = SearchSubject(
        search_type=SearchType.CONTRACT.value,
        contract_number="EV-20481",
        claim_number="EV-20481",
        debtor_id="EV-20481",
    )
    records = await container.search_service.lookup_internal(subject)
    print(f"\n{SEPARATOR}\nПоиск по договору EV-20481\n{SEPARATOR}\n")
    if not records:
        print("Во внутренней базе ничего не найдено.")
        return
    for record in records:
        print(render_internal_card(record))
        print(f"Уровень совпадения: {record.match_confidence:.2f}")
