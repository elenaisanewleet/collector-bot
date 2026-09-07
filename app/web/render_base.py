"""Вся база должников одной страницей: чтобы не лезть в 1С.

Зачем она есть. Очередь отвечает на вопрос «кого нести в суд» и показывает
только тех, кого уже проверили, то есть уже оплатили. А до этого возникает
другой вопрос, обычный и ежедневный: «кто у меня вообще есть и что про него
известно». Сегодня ответ на него ищут в 1С — а туда лезть неудобно, и доступ
туда есть не у всех.

Что здесь есть и чего нет.

**Поиск идёт по всей строке, а не по фамилии.** Оператор помнит то, что помнит:
кусок фамилии, номер машины, улицу, номер записи. Поле поиска одно, и ищет оно
по всему сразу — это дешевле, чем объяснять человеку, в какую графу вводить.

**Сортировка — по имени и по долгу.** Больше не нужно: остальные колонки
справочные, и сортировка по ним не отвечает ни на один вопрос.

**Список выдаётся окном.** Две тысячи строк разом — это мёртвая прокрутка на
телефоне и секунда на каждый ввод буквы. Поиск и сортировка при этом всегда
идут по ВСЕМ строкам, а не по показанным: иначе человек, набравший фамилию,
получал бы ответ «не найдено» о том, что в базе есть.

**Расчётный долг помечен.** Сумма, посчитанная по тарифу, не имеет права
выглядеть подтверждённой документом — это то же правило, что и везде в
продукте, только про деньги.

ФИО здесь не маскируются, в отличие от очереди. Разница осознанная: очередь
живёт по ссылке, которую пересылают, а эта страница и есть справочник
взыскателя — маскировать в нём имена значило бы сделать его бесполезным.
Управляется это сроком жизни ссылки и её отзывом, а не вычёркиванием букв.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.db.models import Debtor
from app.utils.dates import format_datetime, utcnow
from app.utils.formatting import pluralize_ru
from app.utils.money import format_amount
from app.web.render import cell, document, e, navigation, print_footer, raw_cell, section, table

__all__ = ["render_base_page"]

_HEADERS = ("Должник", "Дата рождения", "Машины", "Долг", "Адрес", "ИД записей")

SEARCH_NOTE = (
    "Поиск идёт по всей строке: фамилия, госномер, улица, номер записи — что помните, то и вводите."
)
ESTIMATED_NOTE = (
    "Долг со звёздочкой посчитан по тарифу из дат постановки и выдачи, а не взят "
    "из документа. В цену иска такая сумма идёт только после сверки."
)


def render_base_page(
    debtors: Sequence[Debtor],
    *,
    app_name: str,
    demo_mode: bool = False,
    print_mode: bool = False,
) -> str:
    from app.web.render import demo_banner

    parts: list[str] = []
    if demo_mode:
        parts.append(demo_banner())
    parts.append(_hero(debtors))
    parts.append(_table(debtors))
    parts.append(f"<footer>{e(_footer(debtors))}</footer>")

    nav = navigation(app_name, [("Должники", "#base")])
    body = _STYLE + "".join(parts)
    if print_mode:
        body += print_footer(app_name, utcnow())
    else:
        body += _SCRIPT
    return document(title=f"{app_name} — база должников", nav=nav, body=body)


def _hero(debtors: Sequence[Debtor]) -> str:
    total = len(debtors)
    known = [d for d in debtors if d.debt_amount is not None]
    money = sum((Decimal(str(d.debt_amount)) for d in known), Decimal("0"))
    noun = pluralize_ru(total, "должник", "должника", "должников")
    figures = "".join(
        (
            f'<div><span class="lbl">Всего требований</span>'
            f"<b>{e(format_amount(money) if known else '—')}</b></div>",
            f'<div><span class="lbl">С машиной</span>'
            f"<b>{sum(1 for d in debtors if d.vehicle_plate)}</b></div>",
            f'<div><span class="lbl">С датой рождения</span>'
            f"<b>{sum(1 for d in debtors if d.birth_date)}</b></div>",
        )
    )
    return (
        '<header class="card hero" id="base">'
        f"<h1>{total} {e(noun)}</h1>"
        f'<div class="nums">{figures}</div>'
        '<div class="tools">'
        '<input id="b-find" type="search" placeholder="Поиск: фамилия, госномер, адрес…" '
        'autocomplete="off" spellcheck="false">'
        '<select id="b-sort" aria-label="Сортировка">'
        '<option value="name">По алфавиту</option>'
        '<option value="debt">По сумме долга</option>'
        "</select>"
        "</div>"
        f'<p class="hint">{e(SEARCH_NOTE)}</p>'
        f'<p class="hint" id="b-status"></p>'
        "</header>"
    )


def _table(debtors: Sequence[Debtor]) -> str:
    rows: list[tuple[str, ...]] = []
    attrs: list[str] = []
    for debtor in debtors:
        amount = Decimal(str(debtor.debt_amount)) if debtor.debt_amount is not None else None
        shown = format_amount(amount) if amount is not None else "—"
        if amount is not None and debtor.debt_is_estimated:
            shown += "*"
        plates = debtor.vehicle_plates or debtor.vehicle_plate or ""
        rows.append(
            (
                raw_cell(f"<b>{e(debtor.fio or '—')}</b>", label="Должник"),
                cell(
                    debtor.birth_date.strftime("%d.%m.%Y") if debtor.birth_date else "—",
                    label="Дата рождения",
                ),
                cell(plates or "—", label="Машины"),
                raw_cell(f'<span class="n">{e(shown)}</span>', label="Долг"),
                cell(debtor.address or "—", label="Адрес"),
                cell(debtor.source_record_ids or debtor.external_debtor_id or "—", label="ИД"),
            )
        )
        haystack = " ".join(
            filter(
                None,
                (
                    debtor.fio,
                    debtor.birth_date.strftime("%d.%m.%Y") if debtor.birth_date else "",
                    plates,
                    debtor.address,
                    debtor.source_record_ids,
                    debtor.external_debtor_id,
                ),
            )
        ).lower()
        kopecks = int(amount * 100) if amount is not None else 0
        attrs.append(
            f'data-find="{e(haystack)}" data-debt="{kopecks}" '
            f'data-name="{e((debtor.fio_normalized or "").lower())}"'
        )
    body = f'<div id="base-table">{table(_HEADERS, rows, row_attrs=attrs)}</div>'
    return section("base", "Должники", body + f'<p class="hint">{e(ESTIMATED_NOTE)}</p>')


def _footer(debtors: Sequence[Debtor]) -> str:
    return f"Список на {format_datetime(utcnow())}. Записей: {len(debtors)}."


_STYLE = """<style>
.tools{display:flex;gap:.5rem;margin:.75rem 0}
.tools input{flex:1;min-width:0}
#base-table td .n{font-variant-numeric:tabular-nums;white-space:nowrap}
#base-table td:nth-child(5){max-width:22rem}
#b-more{margin:.5rem auto;display:block}
</style>"""

_SCRIPT = """<script>
(function () {
  var table = document.getElementById('base-table');
  if (!table) return;
  var rows = [].slice.call(table.querySelectorAll('tbody tr'));
  var find = document.getElementById('b-find');
  var sorter = document.getElementById('b-sort');
  var status = document.getElementById('b-status');
  var more = document.createElement('button');
  more.id = 'b-more';
  more.type = 'button';
  table.parentNode.appendChild(more);

  // Окно рендера: две тысячи строк разом — мёртвая прокрутка на телефоне.
  // Поиск и сортировка идут по ВСЕМ строкам, а не по показанным.
  var STEP = window.matchMedia('(max-width:600px)').matches ? 25 : 100;
  var limit = STEP;
  var query = '';
  var mode = 'name';

  function num(row) { return parseInt(row.dataset.debt, 10) || 0; }
  var order = {
    name: function (a, b) { return a.dataset.name.localeCompare(b.dataset.name, 'ru'); },
    debt: function (a, b) { return num(b) - num(a); }
  };

  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  function apply() {
    var matched = rows.filter(function (row) {
      return !query || row.dataset.find.indexOf(query) >= 0;
    });
    matched.sort(order[mode] || order.name);
    rows.forEach(function (row) { row.hidden = true; });
    var shown = matched.slice(0, limit);
    var parent = rows.length ? rows[0].parentNode : null;
    shown.forEach(function (row) { row.hidden = false; if (parent) parent.appendChild(row); });

    var n = matched.length;
    status.textContent = query
      ? (n ? 'Найдено: ' + n + ' ' + plural(n, 'запись', 'записи', 'записей') : 'Ничего не найдено')
      : '';
    more.hidden = n <= limit;
    more.textContent = 'Показать ещё ' + Math.min(STEP, n - limit);
  }

  var timer = null;
  find.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      query = find.value.trim().toLowerCase();
      limit = STEP;
      apply();
    }, 120);
  });
  sorter.addEventListener('change', function () { mode = sorter.value; limit = STEP; apply(); });
  more.addEventListener('click', function () { limit += STEP; apply(); });
  apply();
})();
</script>"""
