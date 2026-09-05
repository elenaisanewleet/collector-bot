"""Страница очереди взыскания.

Главный экран продукта в вебе: восемьсот должников, отсортированных по тому,
что с ними делать. В сообщении Telegram такой таблицы не сделать — ради этого
страница и существует.

Фильтры по вердикту работают без сервера: строки уже на странице, кнопка лишь
прячет лишние. Оператор кликает часто, и ждать запроса на каждый клик незачем.

Провалившаяся проверка здесь — собственное состояние, а не жёлтый вердикт
«проверить руками». Строка, которую не удалось проверить, не должна попадать в
счётчик проверенных: это тот же инвариант, только на уровне прогона.
"""

from __future__ import annotations

from app.db.models import BatchItem
from app.domain.verdict import VERDICT_TITLES, Verdict
from app.services.batch import QueueSnapshot
from app.utils.dates import format_datetime
from app.utils.masking import mask_name
from app.utils.money import format_amount
from app.web.render import (
    ExportLinks,
    cell,
    document,
    e,
    navigation,
    print_footer,
    raw_cell,
    section,
    table,
)

ORDER = (Verdict.FILE, Verdict.ORDER, Verdict.REVIEW, Verdict.DROP)
TONE = {
    Verdict.FILE: "good",
    Verdict.ORDER: "accent",
    Verdict.REVIEW: "warn",
    Verdict.DROP: "crit",
}
# Пятое состояние строки: проверка не выполнена. Отдельный тон, отдельный чип и
# отдельный фильтр — иначе сбой читается как вердикт «проверить руками».
FAILED = "failed"
FAILED_TITLE = "Не проверено"
PRIVACY_NOTE = (
    "ФИО в таблице сокращены: чтобы решить, кого нести в суд, полное имя здесь "
    "не нужно — оно есть в отчёте по конкретному должнику."
)


def render_queue_page(
    snapshot: QueueSnapshot,
    *,
    app_name: str,
    demo_mode: bool = False,
    exports: ExportLinks | None = None,
    print_mode: bool = False,
) -> str:
    from app.web.render import demo_banner

    parts: list[str] = []
    if demo_mode:
        parts.append(demo_banner())
    parts.append(_header(snapshot))
    if snapshot.finished_at is None:
        parts.append(_running(snapshot))
    if exports is not None and not print_mode:
        parts.append(_actions(exports))
    parts.append(_summary(snapshot))
    parts.append(_queue(snapshot, print_mode=print_mode))
    parts.append(f"<footer>{e(_footer_text(snapshot))}</footer>")

    nav = navigation(
        app_name,
        (("summary", "Итог"), ("queue", "Очередь")),
        (
            f"Прогон №{snapshot.run_id}",
            f"от {format_datetime(snapshot.started_at)}",
        ),
    )
    return document(
        title=f"Очередь взыскания №{snapshot.run_id} — {app_name}",
        nav=nav,
        body="".join(parts),
        foot=print_footer(app_name, snapshot.finished_at or snapshot.started_at),
        auto_print=print_mode,
    )


def _header(snapshot: QueueSnapshot) -> str:
    checked = snapshot.processed - snapshot.failed
    failed = f", из них с ошибкой {snapshot.failed}" if snapshot.failed else ""
    return (
        f'<header class="card"><h1>Очередь взыскания</h1>'
        f'<p class="note">Прогон №{snapshot.run_id} · обработано '
        f"{snapshot.processed} из {snapshot.total} · проверено {checked}{failed}</p></header>"
    )


def _running(snapshot: QueueSnapshot) -> str:
    """Плашка незавершённого прогона.

    Незаконченный срез, показанный теми же итоговыми плитками, читается как
    результат: ноль в «безнадёжно» выглядит как «безнадёжных нет».
    """
    done = round(snapshot.processed / snapshot.total * 100) if snapshot.total else 0
    return (
        f'<div class="running"><b>Прогон идёт: {snapshot.processed} из {snapshot.total}</b>'
        f'<div class="meter"><i style="width:{done}%"></i></div>'
        f"Счётчики ниже — промежуточные, они посчитаны только по проверенным."
        f"</div>{_RELOAD_SCRIPT}"
    )


# Незавершённый прогон обновляет сам себя: иначе оператор смотрит на застывший
# срез и не знает, что он застыл.
_RELOAD_SCRIPT = "<script>setTimeout(function(){location.reload()},30000);</script>"


def _actions(exports: ExportLinks) -> str:
    return (
        '<div class="actions">'
        f'<a href="{e(exports.print_url)}">Распечатать или сохранить в PDF</a>'
        f'<a href="{e(exports.text_url)}" download>Скачать таблицей (CSV)</a>'
        "</div>"
    )


def _summary(snapshot: QueueSnapshot) -> str:
    checked = snapshot.processed - snapshot.failed
    cells = [
        f'<div class="cell"><span class="lbl">Проверено</span>'
        f"<b>{checked}</b><small>из {snapshot.total}</small></div>"
    ]
    for verdict in ORDER:
        count = snapshot.count(verdict) - (snapshot.failed if verdict is Verdict.REVIEW else 0)
        debt = snapshot.debt(verdict)
        cells.append(
            f'<div class="cell {TONE[verdict]}"><span class="lbl">'
            f"{e(VERDICT_TITLES[verdict])}</span><b>{max(count, 0)}</b>"
            f"<small>{e(format_amount(debt)) if debt else '—'}</small></div>"
        )
    # Пятая плитка: сбои видны как сбои, а не растворены в «проверить руками».
    cells.append(
        f'<div class="cell failed"><span class="lbl">{FAILED_TITLE}</span>'
        f"<b>{snapshot.failed}</b><small>проверка не выполнена</small></div>"
    )
    strip = f'<div class="strip">{"".join(cells)}</div>'

    saved = (
        f'<p class="note">Не будет потрачено на пошлины по безнадёжным: '
        f"<strong>{e(format_amount(snapshot.saved_fees))}</strong>.</p>"
        if snapshot.saved_fees
        else ""
    )
    ready = (
        f'<p class="note">В суд можно нести сегодня: {snapshot.actionable} '
        f"на {e(format_amount(snapshot.actionable_debt))}.</p>"
        if snapshot.actionable
        else ""
    )
    return section("summary", "Итог прогона", strip + ready + saved)


def _queue(snapshot: QueueSnapshot, *, print_mode: bool = False) -> str:
    counts = {verdict.value: snapshot.count(verdict) for verdict in ORDER}
    counts[Verdict.REVIEW.value] = max(counts[Verdict.REVIEW.value] - snapshot.failed, 0)

    filters = ['<button type="button" data-filter="all" aria-pressed="true">Все</button>']
    filters.extend(
        f'<button type="button" data-filter="{verdict.value}" aria-pressed="false">'
        f"{e(VERDICT_TITLES[verdict])} · {counts[verdict.value]}</button>"
        for verdict in ORDER
        if counts[verdict.value]
    )
    if snapshot.failed:
        filters.append(
            f'<button type="button" data-filter="{FAILED}" aria-pressed="false">'
            f"{FAILED_TITLE} · {snapshot.failed}</button>"
        )

    rows = [_row(item) for item in snapshot.items]
    tones = [f'data-tone="{e(_row_tone(item))}"' for item in snapshot.items]
    grid = table(
        ("Вердикт", "Должник", "Договор", "Долг", "Пошлина", "Обоснование"),
        rows,
        row_attrs=tones,
    )
    controls = "" if print_mode else f'<div class="filters" id="filters">{"".join(filters)}</div>'
    script = "" if print_mode else _FILTER_SCRIPT
    return section(
        "queue",
        f"Очередь — {len(snapshot.items)}",
        controls + grid + f'<p class="note">{e(PRIVACY_NOTE)}</p>' + script,
    )


def _row(item: BatchItem) -> tuple[str, ...]:
    debtor = item.debtor
    # Полное ФИО по одной ссылке на восемьсот строк — это выгрузка базы; для
    # решения «нести или не нести» достаточно сокращённого.
    name = mask_name(debtor.fio) if debtor and debtor.fio else None
    name = name or (debtor.contract_number if debtor else None) or "—"

    if item.error:
        tone, key, title, mark = "unchecked", FAILED, FAILED_TITLE, "!"
    else:
        verdict = _verdict_of(item.verdict)
        tone, key = TONE.get(verdict, "mute"), verdict.value
        title, mark = VERDICT_TITLES.get(verdict, item.verdict), "•"
    return (
        raw_cell(
            f'<span class="tag {tone}"><span class="mark">{mark}</span>{e(title)}</span>',
            label="Вердикт",
            value=key,
        ),
        cell(name, label="Должник"),
        cell(debtor.contract_number if debtor else None, label="Договор", numeric=True, copy=True),
        cell(format_amount(item.debt_amount), label="Долг", numeric=True, right=True),
        cell(
            format_amount(item.state_fee) if item.state_fee else "—",
            label="Пошлина",
            numeric=True,
            right=True,
        ),
        cell(
            f"Проверка не выполнена: {item.error}" if item.error else item.headline,
            label="Обоснование",
        ),
    )


def _row_tone(item: BatchItem) -> str:
    """Кромка строки. У сбоя своя, отличная от жёлтой «проверить руками»."""
    return FAILED if item.error else _verdict_of(item.verdict).value


_FILTER_SCRIPT = """<script>
(function () {
  var box = document.getElementById('filters');
  if (!box) return;
  box.addEventListener('click', function (event) {
    var button = event.target.closest('button[data-filter]');
    if (!button) return;
    var want = button.dataset.filter;
    box.querySelectorAll('button').forEach(function (other) {
      other.setAttribute('aria-pressed', String(other === button));
    });
    document.querySelectorAll('#queue tbody tr').forEach(function (row) {
      var mark = row.querySelector('[data-v]');
      row.hidden = !(want === 'all' || (mark && mark.dataset.v === want));
    });
  });
})();
</script>"""


def _verdict_of(value: str) -> Verdict:
    try:
        return Verdict(value)
    except ValueError:
        return Verdict.REVIEW


def _footer_text(snapshot: QueueSnapshot) -> str:
    finished = (
        f"Прогон завершён {format_datetime(snapshot.finished_at)}. "
        if snapshot.finished_at
        else "Прогон ещё идёт, цифры промежуточные. "
    )
    return (
        finished
        + "Оценка аналитическая и не заменяет юридическую проверку. "
        + "Ссылка временная и открывается без пароля — не пересылайте её посторонним."
    )
