"""Страница очереди взыскания.

Главный экран продукта в вебе: восемьсот должников, отсортированных по тому,
что с ними делать. В сообщении Telegram такой таблицы не сделать — ради этого
страница и существует.

Фильтры по вердикту работают без сервера: строки уже на странице, кнопка лишь
прячет лишние. Оператор кликает часто, и ждать запроса на каждый клик незачем.
"""

from __future__ import annotations

from app.domain.verdict import VERDICT_TITLES, Verdict
from app.services.batch import QueueSnapshot
from app.utils.dates import format_datetime
from app.utils.money import format_amount
from app.web.render import cell, document, e, navigation, section, table

ORDER = (Verdict.FILE, Verdict.ORDER, Verdict.REVIEW, Verdict.DROP)
TONE = {
    Verdict.FILE: "good",
    Verdict.ORDER: "accent",
    Verdict.REVIEW: "warn",
    Verdict.DROP: "crit",
}


def render_queue_page(snapshot: QueueSnapshot, *, app_name: str) -> str:
    body = "".join(
        [
            _header(snapshot),
            _summary(snapshot),
            _queue(snapshot),
            f"<footer>{e(_footer_text(snapshot))}</footer>",
        ]
    )
    nav = navigation(
        app_name,
        (("summary", "Итог"), ("queue", "Очередь")),
        (
            f"Прогон №{snapshot.run_id}",
            f"от {format_datetime(snapshot.started_at)}",
        ),
    )
    return document(title=f"Очередь взыскания №{snapshot.run_id}", nav=nav, body=body)


def _header(snapshot: QueueSnapshot) -> str:
    return (
        f'<header class="card"><h1>Очередь взыскания</h1>'
        f'<p class="sub">Прогон №{snapshot.run_id} · проверено '
        f"{snapshot.processed} из {snapshot.total}"
        f"{' · ошибок ' + str(snapshot.failed) if snapshot.failed else ''}</p></header>"
    )


def _summary(snapshot: QueueSnapshot) -> str:
    cells = [
        f'<div class="cell"><span class="lbl">Проверено</span>'
        f"<b>{snapshot.processed}</b><small>из {snapshot.total}</small></div>"
    ]
    for verdict in ORDER:
        count = snapshot.count(verdict)
        debt = snapshot.debt(verdict)
        cells.append(
            f'<div class="cell {TONE[verdict]}"><span class="lbl">'
            f"{e(VERDICT_TITLES[verdict])}</span><b>{count}</b>"
            f"<small>{e(format_amount(debt)) if debt else '—'}</small></div>"
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


def _queue(snapshot: QueueSnapshot) -> str:
    filters = ['<button type="button" data-filter="all" aria-pressed="true">Все</button>']
    filters.extend(
        f'<button type="button" data-filter="{verdict.value}" aria-pressed="false">'
        f"{e(VERDICT_TITLES[verdict])} · {snapshot.count(verdict)}</button>"
        for verdict in ORDER
        if snapshot.count(verdict)
    )

    rows = []
    for item in snapshot.items:
        debtor = item.debtor
        name = (
            (debtor.fio if debtor else None) or (debtor.contract_number if debtor else None) or "—"
        )
        verdict = _verdict_of(item.verdict)
        rows.append(
            (
                f'<td data-v="{e(item.verdict)}">'
                f'<span class="stripe {e(item.verdict)}"></span>'
                f'<span class="tag {TONE.get(verdict, "mute")}">'
                f"{e(VERDICT_TITLES.get(verdict, item.verdict))}</span></td>",
                cell(name),
                cell(debtor.contract_number if debtor else None, numeric=True, copy=True),
                cell(format_amount(item.debt_amount), numeric=True, right=True),
                cell(
                    format_amount(item.state_fee) if item.state_fee else "—",
                    numeric=True,
                    right=True,
                ),
                cell(item.error or item.headline),
            )
        )

    grid = table(("Вердикт", "Должник", "Договор", "Долг", "Пошлина", "Обоснование"), rows)
    return section(
        "queue",
        f"Очередь — {len(snapshot.items)}",
        f'<div class="filters" id="filters">{"".join(filters)}</div>{grid}{_FILTER_SCRIPT}',
    )


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
        else "Прогон ещё идёт. "
    )
    return (
        finished
        + "Оценка аналитическая и не заменяет юридическую проверку. "
        + "Ссылка временная и открывается без пароля — не пересылайте её посторонним."
    )
