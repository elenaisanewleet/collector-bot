"""Сценарий массового прогона в боте — целиком, от сметы до итога.

Проверяется не «работает ли команда», а четыре утверждения, каждое из которых
стоит денег или доверия.

*   **До запуска названа цена.** Оператор решает, тратить ли деньги, и решение
    принимается по числам в сообщении. Тест на смету, которая не назвала рубли
    или назвала их выдуманными, обязан падать.
*   **Подтверждается конкретная смета.** Сообщения в чате живут вечно, база
    меняется: кнопка, запускающая вчерашний прогон на сегодняшние деньги, —
    это списание втёмную.
*   **Прогон кончается тремя разными способами**, и итог обязан их различать.
    Очередь после отказа источника неполная, а решение о госпошлине принимают
    по ней; «Проверка завершена» над половиной выгрузки — самая дорогая ложь,
    которую этот бот может сказать.
*   **Что успели — показываем всегда.** Оборванный прогон не отменяет
    посчитанного: за двести проверенных строк уже заплачено.

Стенд — настоящий диспетчер (``tests/bot_harness.py``): роутеры, FSM, middleware,
перехвачен только исходящий Telegram. Отдельно от него проверяются чистые
функции вёрстки: состояния, которые в демо-режиме не воспроизвести (прогон на
восемьсот строк, оборванный на трёхстах), проверяются на них.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from aiogram import Bot, Dispatcher

from app.bot.handlers.batch import (
    BASE_CHANGED,
    confirm_label,
    render_estimate,
    render_progress,
    render_summary,
)
from app.container import Container
from app.db.models import Debtor
from app.db.repository import BatchRepository, DebtorRepository
from app.domain.enums import ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import ProviderResult
from app.providers.base import BaseProvider, ProviderError
from app.services.batch import (
    REFUSALS_BEFORE_HALT,
    BatchEstimate,
    BatchProgress,
    BatchService,
    BatchSummary,
    RunStatus,
)
from app.services.share import ShareLinkService

from .bot_harness import (
    OPERATOR_ID,
    SentMessages,
    buttons,
    callbacks,
    dispatcher_for,
    feed,
    make_callback,
    make_message,
    urls,
)

DEMO_DEBTORS = 6


@pytest.fixture
async def loaded(container: Container) -> Container:
    await container.import_service.import_file(container.settings.internal_csv_path)
    return container


@pytest.fixture
def loaded_dispatcher(loaded: Container) -> Dispatcher:
    return dispatcher_for(loaded)


@pytest.fixture
def linked_loaded(loaded: Container) -> Container:
    """Тот же контейнер, но с публичным адресом: ссылки на очередь включены."""
    settings = loaded.settings.model_copy(update={"web_public_url": "https://reports.example.test"})
    loaded.settings = settings
    loaded.share_service = ShareLinkService(settings, loaded.database)
    return loaded


def confirm(sent: SentMessages) -> str:
    """Callback кнопки запуска — последней, которую бот показал.

    Именно последней: пересчитанная смета приходит со своей кнопкой, и тест,
    берущий первую, нажимал бы ту самую устаревшую, от которой мы и защищаемся.
    """
    return [data for data in callbacks(sent) if data.startswith("batch:run")][-1]


async def run_batch(dispatcher: Dispatcher, bot: Bot, sent: SentMessages) -> None:
    await feed(dispatcher, bot, message=make_message("/batch"))
    await feed(dispatcher, bot, callback_query=make_callback(confirm(sent)))


def estimate(**overrides: Any) -> BatchEstimate:
    base: dict[str, Any] = {
        "debtors": 800,
        "cached": 120,
        "to_query": 668,
        "providers_per_debtor": 5,
        "capped": False,
    }
    return BatchEstimate(**{**base, **overrides})


def summary(**overrides: Any) -> BatchSummary:
    base: dict[str, Any] = {
        "run_id": 7,
        "total": 800,
        "processed": 800,
        "failed": 0,
        "counts": {"file": 40, "order": 210, "review": 120, "drop": 300},
        "totals": {
            "file:debt": Decimal("12000000"),
            "file:fee": Decimal("1000000"),
            "order:debt": Decimal("18000000"),
            "order:fee": Decimal("240000"),
            "drop:fee": Decimal("5610868"),
        },
    }
    return BatchSummary(**{**base, **overrides})


# ---------------------------------------------------------------- 1. смета


async def test_the_estimate_names_the_money_before_anything_is_spent(
    loaded_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Первое сообщение отвечает на вопрос «во что мне это встанет»."""
    await feed(loaded_dispatcher, bot, message=make_message("/batch"))

    assert sent.contains(f"В базе: {DEMO_DEBTORS}")
    assert sent.contains("Обращений к источникам: до 30")
    assert sent.contains("списываются с вашего баланса")
    # Ни одна строка ещё не проверена.
    assert not sent.contains("Проверка завершена")
    assert not sent.contains("Проверяю базу")


def test_the_estimate_prints_roubles_when_the_price_is_configured() -> None:
    text = render_estimate(estimate(cost_per_request=Decimal("3")))

    assert "Обращений к источникам: до 3 340" in text
    assert "Спишется с баланса: до 10 020 ₽" in text
    assert "по 3 ₽ за обращение" in text


def test_the_estimate_refuses_to_invent_a_price_it_was_not_given() -> None:
    """Ноль рублей — утверждение «бесплатно». Правда — «цена неизвестна».

    Тариф у каждого договора свой, и смета, назвавшая чужой, обманывает ровно
    там, где оператор решает, тратить ли деньги.
    """
    text = render_estimate(estimate())

    assert "PROVIDER_REQUEST_COST" in text
    assert "Считайте в обращениях" in text
    assert "0 ₽" not in text


def test_the_estimate_says_which_rows_cannot_be_checked_at_all() -> None:
    """Строку без ФИО и без договора надо чинить в выгрузке, то есть до запуска."""
    text = render_estimate(estimate(unusable=12))

    assert "Ни ФИО, ни номера договора: 12 строк" in text
    assert "«не проверено»" in text
    assert "«ничего не найдено»" in text


async def test_unusable_rows_are_counted_and_cost_nothing(loaded: Container) -> None:
    async with loaded.database.session() as session:
        await DebtorRepository(session).upsert(
            Debtor(dedup_key="broken-row", fio=None, contract_number=None)
        )

    found = await loaded.batch_service.estimate()

    assert found.debtors == DEMO_DEBTORS + 1
    assert found.unusable == 1
    # Непроверяемая строка не доходит до источников и в оплачиваемое число не
    # входит: смета не должна закладывать деньги на запрос, которого не будет.
    assert found.to_query == DEMO_DEBTORS
    assert found.requests == DEMO_DEBTORS * found.providers_per_debtor


async def test_an_empty_base_is_offered_an_import_not_an_estimate(
    dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(dispatcher, bot, message=make_message("/batch"))

    assert sent.contains("Внутренняя база пуста")
    assert not sent.contains("Обращений к источникам")


# ---------------------------------------------------------------- 2. подтверждение


def test_the_confirm_button_names_the_sum_not_the_action() -> None:
    """Под пальцем у оператора списание, и подпись обязана говорить о нём."""
    assert (
        confirm_label(estimate(cost_per_request=Decimal("3"))) == "Запустить и списать до 10 020 ₽"
    )
    assert confirm_label(estimate()) == "Запустить — до 3 340 платных обращений"
    assert confirm_label(estimate(providers_per_debtor=0)) == "Запустить проверку"


async def test_nothing_runs_until_the_button_is_pressed(
    loaded_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, loaded: Container
) -> None:
    await feed(loaded_dispatcher, bot, message=make_message("/batch"))

    async with loaded.database.session() as session:
        assert await BatchRepository(session).latest_run(OPERATOR_ID) is None


async def test_the_run_button_carries_the_estimate_it_was_shown_with(
    loaded_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    await feed(loaded_dispatcher, bot, message=make_message("/batch"))

    assert confirm(sent) == f"batch:run:{DEMO_DEBTORS}"
    assert any("Запустить" in text for text in buttons(sent))


async def test_a_stale_estimate_is_re_shown_instead_of_charged(
    loaded_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, loaded: Container
) -> None:
    """База изменилась между сметой и нажатием — деньги не списываются.

    Сообщение со сметой живёт в чате вечно. Нажатое через день, оно запустило бы
    прогон по числам, которых оператор не видел, — и заплатил бы он за них
    сегодняшними деньгами.
    """
    await feed(loaded_dispatcher, bot, message=make_message("/batch"))
    stale = confirm(sent)

    async with loaded.database.session() as session:
        await DebtorRepository(session).upsert(
            Debtor(dedup_key="fresh-row", fio="Новиков Иван Петрович")
        )
    sent.texts.clear()

    await feed(loaded_dispatcher, bot, callback_query=make_callback(stale))

    assert sent.contains(BASE_CHANGED)
    assert sent.contains(f"В базе: {DEMO_DEBTORS + 1}")
    assert not sent.contains("Проверка завершена")
    async with loaded.database.session() as session:
        assert await BatchRepository(session).latest_run(OPERATOR_ID) is None
    # И пересчитанная смета подтверждается уже своим числом.
    assert confirm(sent) == f"batch:run:{DEMO_DEBTORS + 1}"


# ---------------------------------------------------------------- 3. прогресс


async def test_progress_lives_in_one_message_that_is_edited(
    loaded_dispatcher: Dispatcher, bot: Bot, sent: SentMessages
) -> None:
    """Одно сообщение вместо восьмисот — иначе чат нечитаем уже на сотом."""
    await run_batch(loaded_dispatcher, bot, sent)

    started = [text for text in sent.sends if text.startswith("Проверяю базу")]
    assert len(started) == 1, "прогресс отправлен больше одного раза"
    assert sent.edits, "прогресс не правился на месте"


def test_progress_names_what_has_already_been_spent() -> None:
    text = render_progress(
        BatchProgress(processed=140, total=800, failed=0, run_id=7),
        estimate(cost_per_request=Decimal("3")),
    )

    assert "140 из 800" in text
    assert "Потрачено: до 700 обращений" in text
    assert "до 2 100 ₽" in text
    assert "взятые из кэша не оплачиваются" in text


def test_progress_stays_silent_about_money_it_does_not_know() -> None:
    text = render_progress(BatchProgress(processed=140, total=800, failed=0), estimate())

    assert "Потрачено: до 700 обращений" in text
    assert "₽" not in text


async def test_the_queue_link_appears_while_the_run_is_still_going(
    linked_loaded: Container, bot: Bot, sent: SentMessages
) -> None:
    """Очередь заполняется на ходу, и открыть её можно с первой секунды.

    Ссылка, выданная только в конце, означает полчаса пустого ожидания над
    страницей, которая всё это время уже показывала бы движение.
    """
    dispatcher = dispatcher_for(linked_loaded)
    await feed(dispatcher, bot, message=make_message("/batch"))
    await feed(dispatcher, bot, callback_query=make_callback(confirm(sent)))

    progress_markups = [
        markup
        for text, markup in zip(sent.texts, sent.markups, strict=True)
        if text.startswith("Проверяю базу") and markup is not None
    ]
    assert progress_markups, "под прогрессом не было ни одной кнопки"
    assert any(
        button.url and button.url.startswith("https://reports.example.test/q/")
        for markup in progress_markups
        for row in markup.inline_keyboard
        for button in row
    )


async def test_the_run_id_is_known_from_the_very_first_progress_event(
    loaded: Container,
) -> None:
    seen: list[BatchProgress] = []

    async def track(progress: BatchProgress) -> None:
        seen.append(progress)

    result = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID, progress=track)

    assert seen[0].processed == 0
    assert seen[0].run_id == result.run_id


# ---------------------------------------------------------------- 4. итог


async def test_the_summary_is_short_and_offers_the_queue(
    linked_loaded: Container, bot: Bot, sent: SentMessages
) -> None:
    dispatcher = dispatcher_for(linked_loaded)
    await run_batch(dispatcher, bot, sent)

    assert sent.contains("Проверка завершена")
    assert sent.contains("Сэкономлено на пошлинах")
    assert any(url.startswith("https://reports.example.test/q/") for url in urls(sent))
    # Сводка — выжимка, а не отчёт: таблица живёт на странице.
    final = sent.texts[-1]
    assert len(final.splitlines()) < 20


def test_the_summary_names_both_sums_that_matter() -> None:
    text = render_summary(summary())

    assert "Проверка завершена: 800 из 800" in text
    assert "Пошлина по 250 должникам, которых несём в суд: 1 240 000 ₽" in text
    assert "Сэкономлено на пошлинах: 5 610 868 ₽" in text
    assert "300 безнадёжных отсеяно" in text


def test_a_zero_saving_is_printed_rather_than_omitted() -> None:
    """Пропущенная строка читается как отсутствие экономии.

    «Безнадёжных не нашлось» — другое утверждение, и его надо сказать. Тем же
    правилом живёт страница очереди.
    """
    text = render_summary(summary(counts={"order": 3}, totals={"order:debt": Decimal("300000")}))

    assert "Сэкономлено на пошлинах: 0 ₽" in text
    assert "безнадёжных в этом прогоне не нашлось" in text


def test_failed_rows_are_not_reported_as_clean() -> None:
    text = render_summary(summary(failed=30))

    assert "Не удалось проверить: 30 строк" in text
    assert "Это не «ничего не найдено»" in text


# ---------------------------------------------------------------- 5. обрыв и баланс


class RefusingProvider(BaseProvider):
    """Источник, у которого кончился баланс.

    Отвечает так же, как настоящий: не исключением наружу, а результатом с
    кодом ``payment_required`` — провайдеры в этом проекте не бросают.
    """

    name = ProviderName.FSSP
    title = "ФССП"

    @property
    def is_configured(self) -> bool:
        return True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        raise ProviderError("payment_required", "недостаточно средств на счёте")


def refusing_container(container: Container) -> Container:
    """Контейнер, в котором единственный источник отказывает по деньгам."""
    from app.providers.registry import ProviderRegistry, build_internal_provider
    from app.services.search import SearchService

    # Один должник за раз: порог остановки должен срабатывать на предсказуемой
    # строке, иначе тест меряет расписание asyncio, а не поведение прогона.
    settings = container.settings.model_copy(update={"batch_concurrency": 1})
    registry = ProviderRegistry(
        internal=build_internal_provider(settings, container.database),
        external=[RefusingProvider()],
    )
    search = SearchService(settings=settings, database=container.database, registry=registry)
    return Container(
        settings=settings,
        database=container.database,
        registry=registry,
        search_service=search,
        import_service=container.import_service,
        batch_service=BatchService(
            settings=settings, database=container.database, search_service=search
        ),
        verdict_engine=container.verdict_engine,
        share_service=container.share_service,
        subject_store=container.subject_store,
        # Подменяется только источник и его окружение; всё остальное берётся
        # готовым из исходного контейнера, чтобы список полей не превращался в
        # вторую сборку приложения.
        access_service=container.access_service,
    )


async def test_a_depleted_balance_stops_the_run_instead_of_paying_on(
    loaded: Container,
) -> None:
    """Отказ по деньгам — это дыра во всех оставшихся строках сразу.

    Один упавший источник виден в строке. Источник, который отказывает всем,
    дописал бы пятьсот бодрых «ничего не найдено» — за деньги и с вердиктами,
    неотличимыми от посчитанных по-настоящему.
    """
    broke = refusing_container(loaded)

    result = await broke.batch_service.run(telegram_user_id=OPERATOR_ID)

    assert result.status == RunStatus.STOPPED
    assert result.processed == REFUSALS_BEFORE_HALT
    assert result.processed < result.total
    assert result.refused_sources == (ProviderName.FSSP.value,)
    assert not result.is_complete
    assert result.unchecked == DEMO_DEBTORS - REFUSALS_BEFORE_HALT

    async with broke.database.session() as session:
        repo = BatchRepository(session)
        run = await repo.latest_run(OPERATOR_ID)
        rows = await repo.queue(result.run_id, limit=100)
    assert run is not None
    assert run.status == RunStatus.STOPPED
    # Непроверенные строки не записаны вовсе: пустая строка «не проверено»
    # неотличима от провалившейся проверки, а это разные вещи.
    assert len(rows) == REFUSALS_BEFORE_HALT


async def test_a_stopped_run_says_so_in_the_chat_and_still_shows_what_it_got(
    loaded: Container, bot: Bot, sent: SentMessages
) -> None:
    broke = refusing_container(loaded)
    broke.share_service = ShareLinkService(
        broke.settings.model_copy(update={"web_public_url": "https://reports.example.test"}),
        broke.database,
    )
    dispatcher = dispatcher_for(broke)

    await run_batch(dispatcher, bot, sent)

    assert sent.contains("Прогон остановлен: источник перестал отвечать")
    assert sent.contains("Отказ пришёл от: ФССП")
    assert sent.contains("Очередь ниже неполная")
    assert sent.contains("не проверялись вовсе")
    # Что успели — всё равно показываем, вместе со ссылкой на очередь.
    assert sent.contains("Сэкономлено на пошлинах")
    assert any(url.startswith("https://reports.example.test/q/") for url in urls(sent))


async def test_a_cached_refusal_does_not_stop_todays_run(loaded: Container) -> None:
    """Вчерашний отказ ничего не стоил и ничего не говорит о сегодняшнем счёте."""
    from app.domain.models import DebtorReport
    from app.services.batch import _refusals

    refused = ProviderResult(
        provider=ProviderName.FSSP,
        status=ProviderStatus.ERROR,
        error_code="payment_required",
    )
    subject = SearchSubject(search_type="contract", contract_number="EV-1")
    fresh = DebtorReport(subject=subject, provider_results=[refused])
    cached = DebtorReport(subject=subject, provider_results=[refused], from_cache=True)

    assert _refusals(fresh) == (ProviderName.FSSP.value,)
    assert _refusals(cached) == ()


async def test_a_torn_run_is_closed_honestly_rather_than_left_running(
    loaded: Container, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Сбой на записи не оставляет прогон вечно «идущим».

    Иначе страница очереди ждёт строк, которых уже никто не напишет, и целых
    десять минут показывает бодрый прогресс над мёртвым прогоном.
    """

    async def explode(self: BatchRepository, item: object) -> None:
        raise RuntimeError("диск кончился")

    monkeypatch.setattr(BatchRepository, "add_item", explode)

    result = await loaded.batch_service.run(telegram_user_id=OPERATOR_ID)

    assert result.status == RunStatus.INTERRUPTED
    assert result.error == "RuntimeError"
    assert not result.is_complete

    async with loaded.database.session() as session:
        run = await BatchRepository(session).latest_run(OPERATOR_ID)
    assert run is not None
    assert run.status == RunStatus.INTERRUPTED
    assert run.finished_at is not None


async def test_a_torn_run_still_delivers_a_summary_to_the_chat(
    loaded_dispatcher: Dispatcher, bot: Bot, sent: SentMessages, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(self: BatchRepository, item: object) -> None:
        raise RuntimeError("диск кончился")

    monkeypatch.setattr(BatchRepository, "add_item", explode)

    await run_batch(loaded_dispatcher, bot, sent)

    assert sent.contains("Прогон оборвался")
    assert sent.contains("RuntimeError")
    assert not sent.contains("Проверка завершена")


def test_a_torn_summary_reads_as_incomplete_before_it_reads_as_numbers() -> None:
    """Порядок строк — часть утверждения.

    Двести «можно подавать», прочитанные до сообщения о том, что прогон встал на
    трёхстах из восьмисот, успевают стать планом на неделю.
    """
    text = render_summary(
        summary(processed=300, status=RunStatus.INTERRUPTED, error="TimeoutError")
    )
    lines = text.splitlines()

    assert lines[0] == "Прогон оборвался"
    incomplete = next(i for i, line in enumerate(lines) if "Очередь ниже неполная" in line)
    verdicts = next(i for i, line in enumerate(lines) if line.startswith("Подавать иск"))
    assert incomplete < verdicts
    assert "Оставшиеся 500 должников не проверялись вовсе" in text
    assert "возьмутся из кэша" in text


def test_a_run_that_finished_its_last_page_badly_admits_it() -> None:
    """Счётчик дошёл до конца, а закрытие сорвалось: строк может не хватать."""
    text = render_summary(summary(status=RunStatus.INTERRUPTED, error="OperationalError"))

    assert "закрылся нештатно" in text
    assert "могла не попасть в очередь" in text
