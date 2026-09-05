"""Страница очереди — главный экран продукта.

Проверяется не вёрстка, а утверждения, которые страница делает о деньгах и о
полноте данных. Одно и то же число, поданное как «проверено» вместо «не
проверено», стоит оператору госпошлины; поэтому счётчики здесь проверяются
поимённо, а «не проверено» — во всех четырёх состояниях прогона: пустом,
идущем, оборванном и полностью провалившемся.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.db.models import BatchItem, Debtor
from app.domain.verdict import VERDICT_ORDER, Verdict
from app.services.batch import QueueSnapshot
from app.web.render import ExportLinks
from app.web.render_queue import (
    FAILED,
    FAILED_TITLE,
    STALE_AFTER,
    Coverage,
    _row,
    _row_coverage,
    _row_tone,
    coverage_of,
    render_queue_page,
)

APP = "Проверка долга"
STARTED = datetime(2026, 9, 4, 9, 0, tzinfo=UTC)
EXPORTS = ExportLinks(text_url="/q/tok/queue.csv", print_url="/q/tok/print")


def item(
    *,
    index: int = 1,
    verdict: Verdict = Verdict.ORDER,
    debt: Decimal | None = Decimal("120000"),
    fee: Decimal | None = Decimal("2400"),
    score: int | None = 62,
    confidence: int = 100,
    error: str | None = None,
    fio: str = "Демов Максим Игоревич",
    minutes: int = 0,
) -> BatchItem:
    row = BatchItem(
        id=index,
        batch_run_id=1,
        debtor_id=index,
        verdict=verdict.value,
        verdict_order=VERDICT_ORDER[verdict],
        headline="Проверка не выполнена" if error else "Долг бесспорный.",
        debt_amount=None if error else debt,
        debt_kopecks=0 if error or debt is None else int(debt * 100),
        state_fee=None if error else fee,
        score=None if error else score,
        confidence=0 if error else confidence,
        error=error,
        created_at=STARTED + timedelta(minutes=minutes),
    )
    row.debtor = Debtor(
        id=index,
        dedup_key=f"row-{index}",
        fio=fio,
        contract_number=f"ЭВ-{index:04d}",
    )
    return row


def snapshot(
    items: list[BatchItem],
    *,
    total: int | None = None,
    running: bool = False,
    status: str | None = None,
    counts: dict[str, int] | None = None,
    totals: dict[str, Decimal] | None = None,
) -> QueueSnapshot:
    """Срез прогона со счётчиками, посчитанными так же, как их считает база."""
    if counts is None:
        counts = {}
        for row in items:
            counts[row.verdict] = counts.get(row.verdict, 0) + 1
    if totals is None:
        totals = {}
        for row in items:
            totals[f"{row.verdict}:debt"] = totals.get(f"{row.verdict}:debt", Decimal("0")) + (
                row.debt_amount or Decimal("0")
            )
            totals[f"{row.verdict}:fee"] = totals.get(f"{row.verdict}:fee", Decimal("0")) + (
                row.state_fee or Decimal("0")
            )
    return QueueSnapshot(
        run_id=7,
        started_at=STARTED,
        finished_at=None if running else STARTED + timedelta(minutes=20),
        total=total if total is not None else len(items),
        processed=len(items),
        failed=sum(1 for row in items if row.error),
        counts=counts,
        totals=totals,
        items=items,
        status=status or ("running" if running else "finished"),
        providers_per_debtor=5,
    )


def page(snap: QueueSnapshot, **kwargs: object) -> str:
    return render_queue_page(snap, app_name=APP, exports=EXPORTS, **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------- деньги


def test_the_money_answer_stands_above_the_table() -> None:
    """Первый экран отвечает про деньги: сколько окупается и во что встанет."""
    rows = [
        item(index=1, verdict=Verdict.FILE, debt=Decimal("900000"), fee=Decimal("23000")),
        item(index=2, verdict=Verdict.ORDER, debt=Decimal("120000"), fee=Decimal("2400")),
        item(index=3, verdict=Verdict.DROP, debt=Decimal("80000"), fee=Decimal("2000")),
    ]
    html = page(snapshot(rows))

    hero = html.split("</header>")[0]
    assert "Суд окупается" in hero
    # Двое из троих: иск и приказ. Отсев в этот счётчик не попадает.
    assert "<b>2</b>" in hero
    assert "1 020 000 ₽" in hero  # долг по ним
    assert "25 400 ₽" in hero  # пошлина за них
    assert "1 100 000 ₽" in hero  # долг всего


def test_saved_fees_are_visible_and_named() -> None:
    """Самая продающая цифра продукта раньше не показывалась вовсе."""
    rows = [
        item(index=1, verdict=Verdict.ORDER, debt=Decimal("120000"), fee=Decimal("2400")),
        item(index=2, verdict=Verdict.DROP, debt=Decimal("300000"), fee=Decimal("9500")),
    ]
    hero = page(snapshot(rows)).split("</header>")[0]

    assert "Сэкономлено пошлины" in hero
    assert "9 500 ₽" in hero
    assert "останутся в кассе" in hero


def test_saved_fees_say_zero_out_loud() -> None:
    """Ноль печатается словами: отсутствие строки читается как «нет экономии»."""
    hero = page(snapshot([item(index=1)])).split("</header>")[0]
    assert "Сэкономлено пошлины" in hero
    assert "безнадёжных в этом прогоне пока не нашлось" in hero


def test_the_fee_bar_leaves_unpriced_rows_out_of_its_width() -> None:
    """Ширина сегмента — рубли. У непроверенных строк рублей нет.

    Подмешать их шириной значило бы выдумать сумму; вместо этого полоса
    получает рваный край и говорит, скольких строк в ней не хватает.
    """
    rows = [
        item(index=1, verdict=Verdict.ORDER, debt=Decimal("120000"), fee=Decimal("2400")),
        item(index=2, verdict=Verdict.REVIEW, error="TimeoutError"),
    ]
    hero = page(snapshot(rows)).split("</header>")[0]

    assert 'class="bar open"' in hero
    assert "Полоса неполная" in hero
    assert "1 не проверено" in hero
    assert "ещё в очереди" not in hero


# ---------------------------------------------------------------- полнота


def test_unchecked_rows_never_enter_the_checked_counter() -> None:
    """Главный инвариант на масштабе: сбой — не проверка.

    Три строки: одна проверена целиком, одна по половине источников, одна
    провалилась. Проверенных — две, а не три.
    """
    rows = [
        item(index=1, confidence=100),
        item(index=2, confidence=55),
        item(index=3, error="HTTPStatusError"),
    ]
    cover = coverage_of(snapshot(rows))

    assert cover == Coverage(full=1, partial=1, failed=1, pending=0, total=3)
    assert cover.checked == 2
    assert cover.failed == 1
    assert cover.full + cover.partial + cover.failed + cover.pending == cover.total

    html = page(snapshot(rows))
    assert "В счётчик проверенных попали 2 строки из 3" in html
    assert "Непроверенные 1 в него не входят" in html


def test_partial_rows_are_marked_in_the_markup_not_only_in_prose() -> None:
    """Кромка строки несёт полноту: сортировка по баллу не станет надёжнее данных."""
    full = item(index=1, confidence=100)
    partial = item(index=2, confidence=40)
    broken = item(index=3, error="TimeoutError")

    assert _row_coverage(full) == "full"
    assert _row_coverage(partial) == "partial"
    assert _row_coverage(broken) == FAILED

    html = page(snapshot([full, partial, broken]))
    assert 'data-cov="full"' in html
    assert 'data-cov="partial"' in html
    assert f'data-cov="{FAILED}"' in html
    # Балл частичной строки подписан покрытием, полной — нет: словами
    # отмечается исключение.
    assert "данные 40%" in html
    assert "данные 100%" not in html


def test_sorting_by_score_carries_its_own_warning() -> None:
    html = page(snapshot([item(index=1, confidence=45)]))
    assert 'id="q-score-note"' in html
    assert "Сортировка по баллу не делает данные полнее" in html


def test_a_failed_row_is_not_a_yellow_verdict() -> None:
    """Сбой проверки — собственное состояние, а не «проверить руками»."""
    broken = item(index=1, verdict=Verdict.REVIEW, error="TimeoutError")
    cells = "".join(_row(broken))

    assert FAILED_TITLE in cells
    assert "Проверить руками" not in cells
    assert f'data-v="{FAILED}"' in cells
    assert _row_tone(broken) == FAILED


def test_the_ledger_prices_failed_rows_as_unknown_not_as_zero() -> None:
    """Ноль означал бы «эти строки ничего не стоят». Правда — «мы не знаем»."""
    rows = [
        item(index=1, verdict=Verdict.ORDER, debt=Decimal("120000"), fee=Decimal("2400")),
        item(index=2, verdict=Verdict.REVIEW, error="TimeoutError"),
    ]
    money = page(snapshot(rows)).split('id="money"')[1].split("</section>")[0]

    assert FAILED_TITLE in money
    # Строка сбоя несёт прочерки в обеих денежных колонках, а не нули.
    assert '<td class="n r" data-l="Долг">—</td>' in money
    assert '<td class="n r" data-l="Пошлина">—</td>' in money
    assert "цену считать не из чего" in money
    # «Проверить руками» осталось без своей единственной строки: она была сбоем.
    assert 'data-l="Строк">0</td>' in money


# ---------------------------------------------------------------- состояния прогона


def test_an_empty_run_does_not_pretend_to_be_a_result() -> None:
    """Нули под подписью «суд окупается» читаются как «проверили — не окупается»."""
    html = page(snapshot([], total=0))

    assert "Прогон был пустым" in html
    assert "Суд окупается" not in html
    assert "Считать пока нечего" in html
    assert "/import" in html


def test_a_running_run_shows_progress_and_what_it_has_already_cost() -> None:
    rows = [item(index=i, minutes=i) for i in range(1, 4)]
    html = page(snapshot(rows, total=10, running=True), now=STARTED + timedelta(minutes=4))

    assert "Прогон идёт: 3 из 10" in html
    assert "Осталось 7 должников" in html
    assert "Потрачено запросов к платным источникам: до 15" in html
    assert "ещё в очереди <b>7</b>" in html
    # Незавершённые счётчики обязаны называть себя промежуточными.
    assert "Счётчики выше — промежуточные" in html
    assert "location.reload()" in html


def test_a_run_that_stopped_moving_says_so() -> None:
    """Оборванный прогон снаружи неотличим от идущего — кроме времени строк."""
    rows = [item(index=i, minutes=i) for i in range(1, 4)]
    stale = STARTED + timedelta(minutes=3) + STALE_AFTER + timedelta(minutes=5)
    html = page(snapshot(rows, total=800, running=True), now=stale)

    assert "Похоже, прогон оборвался" in html
    assert "Новых строк нет уже 15 минут" in html
    assert 'class="running stalled"' in html
    # Перезагружать зависшую страницу каждые полминуты незачем.
    assert "location.reload()" not in html
    # Посчитанное остаётся верным и никуда не девается.
    assert "Очередь — 3" in html


def test_a_run_stopped_by_a_refusing_source_is_neither_running_nor_done() -> None:
    """Третье состояние страницы, и оно не сводится к двум прежним.

    «Идёт» — неправда: никто больше ничего не напишет. «Завершён» — неправда
    дороже: под этим словом очередь читается как ответ по всей выгрузке, а
    госпошлину платят по ней.
    """
    rows = [item(index=i, minutes=i) for i in range(1, 4)]
    html = page(snapshot(rows, total=800, status="stopped"))

    assert "Прогон неполный: проверено 3 из 800" in html
    assert "Источник перестал отвечать" in html
    assert "Оставшиеся 797 должников не проверялись вовсе" in html
    # Ни бодрого прогресса, ни перезагрузки: ждать нечего.
    assert "Прогон идёт" not in html
    assert "location.reload()" not in html


def test_a_torn_run_never_calls_its_missing_rows_a_queue() -> None:
    """«Ещё в очереди» — только пока очередь есть.

    Числа те же, слова другие: строки, до которых прогон не дошёл и уже не
    дойдёт, стоят в счётчике полноты не как ожидающие, а как непроверенные.
    """
    rows = [item(index=1)]
    torn = page(snapshot(rows, total=10, status="interrupted"))
    alive = page(snapshot(rows, total=10, running=True), now=STARTED + timedelta(minutes=1))

    assert "Не проверялись" in torn
    assert "прогон до них не дошёл и уже не дойдёт" in torn
    assert "Ещё в очереди" not in torn
    # А у идущего прогона они действительно ещё в очереди.
    assert "Ещё в очереди" in alive


def test_a_run_where_nothing_could_be_checked_says_nothing_was_checked() -> None:
    """Восемь строк, все со сбоем: бодрых нулей по вердиктам быть не должно."""
    rows = [item(index=i, verdict=Verdict.REVIEW, error="TimeoutError") for i in range(1, 9)]
    html = page(snapshot(rows))

    assert "Проверено полностью <b>0</b>" in html
    assert "не проверено <b>8</b>" in html
    assert "В счётчик проверенных попали 0 строк из 8" in html
    # Пошлину считать не из чего, и так и сказано — вместо полосы с нулями.
    assert "Пошлину пока не из чего считать" in html
    assert 'class="bar' not in html


def test_a_run_that_finished_without_writing_anything_offers_a_next_step() -> None:
    html = page(snapshot([], total=42))
    assert "Ни одна из 42 строк не дошла до очереди" in html
    assert "/batch" in html


# ---------------------------------------------------------------- интерактив


def test_the_page_carries_its_own_sorting_search_and_filters() -> None:
    rows = [
        item(index=1, verdict=Verdict.FILE, debt=Decimal("900000")),
        item(index=2, verdict=Verdict.ORDER, debt=Decimal("120000")),
        item(index=3, verdict=Verdict.DROP, debt=Decimal("9000"), fee=Decimal("400")),
    ]
    html = page(snapshot(rows))

    assert 'id="q-find"' in html
    assert 'id="q-sort"' in html
    assert 'value="debt"' in html and 'value="score"' in html
    assert 'data-filter="file"' in html
    # Ключи сортировки лежат на строке, а не выковыриваются из отформатированных
    # рублей: «87 600 ₽» строкой сортируется выше «154 200 ₽».
    assert 'data-debt="90000000"' in html
    assert 'data-name="демов м. и. эв-0001"' in html
    # Группы сворачиваются, окно рендера расширяется кнопкой.
    assert 'data-group="file"' in html
    assert 'id="q-more"' in html


def test_amounts_copy_as_numbers_not_as_roubles() -> None:
    """В исковое заявление вставляют 120000, а не «120 000 ₽»."""
    html = page(snapshot([item(index=1, debt=Decimal("120000"))]))
    assert 'data-copy="120000">120 000 ₽</button>' in html
    assert 'data-copy="ЭВ-0001"' in html


def test_the_printed_page_carries_no_interface() -> None:
    """На бумагу уходит документ, а не пульт управления им."""
    rows = [item(index=1), item(index=2, verdict=Verdict.DROP)]
    html = page(snapshot(rows), print_mode=True)

    assert 'id="q-find"' not in html
    assert 'class="filters"' not in html
    assert "window.print()" in html
    # Но сами строки — все до одной, без окна рендера и без обрезки.
    assert html.count('data-tone="') >= 2
    assert "Сэкономлено пошлины" in html


def test_full_names_never_reach_the_queue_table() -> None:
    """Восемьсот полных ФИО по одной ссылке без пароля — это выгрузка базы."""
    html = page(snapshot([item(index=1, fio="Демов Максим Игоревич")]))
    assert "Демов М. И." in html
    assert "Демов Максим Игоревич" not in html


def test_the_page_reaches_nothing_outside_itself() -> None:
    """Страница обязана открываться без интернета: ни шрифтов, ни CDN."""
    import re

    html = page(snapshot([item(index=1)]))
    assert re.search(r'(?:src|href)="(?:https?:)?//', html) is None
    assert "fonts.googleapis.com" not in html
