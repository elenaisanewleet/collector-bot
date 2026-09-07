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
from typing import NamedTuple

from app.db.models import Debtor
from app.domain.fees import claim_fee, court_order_fee
from app.utils.dates import format_datetime, utcnow
from app.utils.formatting import pluralize_ru
from app.utils.money import format_amount
from app.web.render import cell, document, e, navigation, print_footer, raw_cell, section, table

__all__ = ["FeeRules", "render_base_page", "render_person_page"]

_HEADERS = ("Должник", "Долг", "Пошлина", "Как подавать", "Машины", "Адрес")

SEARCH_NOTE = (
    "Поиск идёт по всей строке: фамилия, госномер, улица, номер записи — что помните, то и вводите."
)
ESTIMATED_NOTE = (
    "Долг со звёздочкой посчитан по тарифу из дат постановки и выдачи, а не взят "
    "из документа. В цену иска такая сумма идёт только после сверки."
)
FEE_NOTE = (
    "Пошлина посчитана по ст. 333.19 НК РФ от суммы долга. Проценты по ст. 395 ГК "
    "сюда не входят: их считает юрист на дату подачи, и от них зависит ступень."
)


class FeeRules(NamedTuple):
    """Пороги, по которым выбирается способ подачи. Приходят из настроек.

    Страница не решает, где проходят границы, — их знает
    :mod:`app.services.verdict`, и переписывать их здесь значило бы завести
    вторую копию, которая разъедется с первой на первой же правке тарифа.
    """

    court_order_max: Decimal
    min_debt_to_fee_ratio: Decimal


class _Kind(NamedTuple):
    """Как подавать на этого должника — и каким цветом это показать."""

    key: str
    title: str
    tab: str


#: Четыре исхода, и каждый — про деньги заказчика, а не про свойства строки.
#: Цвета живут в CSS по ключу: зелёный — приказ (дёшево и быстро), синий — иск
#: (дороже, но столько же денег на кону), янтарь — процесс не окупается, серый
#: — считать не из чего.
_ORDER = _Kind("order", "Судебный приказ", "Судебный приказ")
_CLAIM = _Kind("claim", "Иск", "Иск")
_THIN = _Kind("thin", "Не окупается", "Не окупается")
_NONE = _Kind("none", "Нет суммы", "Без суммы")
_KINDS = (_ORDER, _CLAIM, _THIN, _NONE)


def _classify(amount: Decimal | None, rules: FeeRules) -> tuple[_Kind, Decimal | None]:
    """Способ подачи и пошлина по нему.

    Тот же порядок веток, что и у вердикта: сначала «а есть ли из чего
    считать», потом «окупится ли», и только потом «приказ или иск». Проверять
    окупаемость последней значило бы советовать судебный приказ там, где он
    стоит дороже долга.
    """
    if amount is None or amount <= 0:
        return _NONE, None
    fee = court_order_fee(amount) if amount <= rules.court_order_max else claim_fee(amount)
    if fee and amount < fee * rules.min_debt_to_fee_ratio:
        return _THIN, fee
    return (_ORDER if amount <= rules.court_order_max else _CLAIM), fee


def render_base_page(
    debtors: Sequence[Debtor],
    *,
    app_name: str,
    rules: FeeRules,
    person_urls: dict[int, str] | None = None,
    demo_mode: bool = False,
    print_mode: bool = False,
) -> str:
    from app.web.render import demo_banner

    rows = [(debtor, *_classify(_amount(debtor), rules)) for debtor in debtors]

    parts: list[str] = []
    if demo_mode:
        parts.append(demo_banner())
    parts.append(_hero(rows))
    parts.append(_table(rows, person_urls or {}))
    parts.append(f"<footer>{e(_footer(debtors))}</footer>")

    nav = navigation(app_name, [("base", "Должники")])
    body = _STYLE + "".join(parts)
    if print_mode:
        body += print_footer(app_name, utcnow())
    else:
        body += _SCRIPT
    return document(title=f"{app_name} — база должников", nav=nav, body=body)


def _amount(debtor: Debtor) -> Decimal | None:
    return Decimal(str(debtor.debt_amount)) if debtor.debt_amount is not None else None


_Row = tuple[Debtor, _Kind, Decimal | None]


def _hero(rows: Sequence[_Row]) -> str:
    """Шапка: сводка деньгами, вкладки и поиск.

    Сводка отвечает на вопрос, ради которого список и открывают: сколько всего
    на кону и во что обойдётся это забрать. Раньше здесь стояли «с машиной» и
    «с датой рождения» — свойства выгрузки, а не деньги; ни одно решение по ним
    не принимают.
    """
    total = len(rows)
    # Долг берётся у должника, а не третьим полем строки: третье — это пошлина.
    debts = [amount for debtor, _, _ in rows if (amount := _amount(debtor)) is not None]
    debt = sum(debts, Decimal(0))
    # Пошлины считаются только там, где подавать стоит: сложить их со строками
    # «не окупается» значило бы показать заказчику счёт за суды, которых он не
    # начнёт.
    fees = sum((fee for _, kind, fee in rows if fee is not None and kind is not _THIN), Decimal(0))
    worth = sum(1 for _, kind, _ in rows if kind in (_ORDER, _CLAIM))
    noun = pluralize_ru(total, "должник", "должника", "должников")
    figures = "".join(
        (
            f'<div><span class="lbl">Всего требований</span>'
            f"<b>{e(format_amount(debt) if debt else '—')}</b></div>",
            f'<div><span class="lbl">Пошлины по ним</span>'
            f"<b>{e(format_amount(fees) if fees else '—')}</b>"
            f"<small>по {worth} из {total}, где подавать стоит</small></div>",
        )
    )
    counts = {kind.key: sum(1 for _, k, _ in rows if k is kind) for kind in _KINDS}
    tabs = "".join(
        f'<button type="button" class="tab k-{kind.key}" data-kind="{kind.key}">'
        f"{e(kind.tab)} <span>{counts[kind.key]}</span></button>"
        for kind in _KINDS
        # Пустая вкладка не рисуется: «Иск (0)» — это приглашение нажать и
        # увидеть пустой список.
        if counts[kind.key]
    )
    return (
        '<header class="hero" id="base">'
        f"<h1>{total} {e(noun)}</h1>"
        f'<div class="nums">{figures}</div>'
        f'<div class="tabs"><button type="button" class="tab on" data-kind="">'
        f"Все <span>{total}</span></button>{tabs}</div>"
        '<div class="tools">'
        '<input id="b-find" type="search" placeholder="Поиск: фамилия, госномер, адрес…" '
        'autocomplete="off" spellcheck="false">'
        '<select id="b-sort" aria-label="Сортировка">'
        '<option value="name">По алфавиту</option>'
        '<option value="debt">Сначала крупные долги</option>'
        '<option value="debt-asc">Сначала мелкие долги</option>'
        "</select>"
        "</div>"
        f'<p class="hint">{e(SEARCH_NOTE)}</p>'
        '<p class="hint" id="b-status"></p>'
        "</header>"
    )


def _table(rows: Sequence[_Row], person_urls: dict[int, str]) -> str:
    cells: list[tuple[str, ...]] = []
    attrs: list[str] = []
    for debtor, kind, fee in rows:
        amount = _amount(debtor)
        shown = format_amount(amount) if amount is not None else "—"
        if amount is not None and debtor.debt_is_estimated:
            shown += "*"
        plates = debtor.vehicle_plates or debtor.vehicle_plate or ""
        cells.append(
            (
                raw_cell(
                    f'<a href="{e(person_urls[debtor.id])}"><b>{e(debtor.fio or "—")}</b></a>'
                    if debtor.id in person_urls
                    else f"<b>{e(debtor.fio or '—')}</b>",
                    label="Должник",
                ),
                raw_cell(f'<span class="n">{e(shown)}</span>', label="Долг"),
                raw_cell(
                    f'<span class="n">{e(format_amount(fee) if fee is not None else "—")}</span>',
                    label="Пошлина",
                ),
                raw_cell(
                    f'<span class="pill k-{kind.key}">{e(kind.title)}</span>',
                    label="Как подавать",
                ),
                cell(plates or "—", label="Машины"),
                cell(debtor.address or "—", label="Адрес"),
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
            f'data-find="{e(haystack)}" data-debt="{kopecks}" data-kind="{kind.key}" '
            f'data-name="{e((debtor.fio_normalized or "").lower())}"'
        )
    body = f'<div id="base-table">{table(_HEADERS, cells, row_attrs=attrs)}</div>'
    notes = f'<p class="hint">{e(ESTIMATED_NOTE)}</p><p class="hint">{e(FEE_NOTE)}</p>'
    return section("base", "Должники", body + notes)


def _footer(debtors: Sequence[Debtor]) -> str:
    return f"Список на {format_datetime(utcnow())}. Записей: {len(debtors)}."


_STYLE = """<style>
.tools{display:flex;gap:.5rem;margin:.75rem 0}
.tools input{flex:1;min-width:0}
#base-table td .n{font-variant-numeric:tabular-nums;white-space:nowrap}
#base-table td:nth-child(6){max-width:20rem}
#b-more{margin:.5rem auto;display:block}

/* Вкладки. Живут в герое, поэтому и цвета берут геройские: на тёмной подложке
   обычные --ink-* не читаются вовсе. */
.tabs{display:flex;flex-wrap:wrap;gap:.4rem;margin:.9rem 0 .2rem}
.tabs .tab{font:inherit;font-size:var(--t-xs);cursor:pointer;
  padding:.3rem .7rem;border-radius:999px;
  border:1px solid var(--hero-dim);background:transparent;color:var(--hero-soft)}
.tabs .tab span{opacity:.65;margin-left:.25rem;font-variant-numeric:tabular-nums}
.tabs .tab:hover{border-color:var(--hero-ink);color:var(--hero-ink)}
.tabs .tab.on{background:var(--hero-ink);border-color:var(--hero-ink);color:var(--hero)}
.tabs .tab.on span{opacity:.55}

/* Один цвет на один исход, и тот же самый — на вкладке и в строке таблицы.
   Разойдись они, и вкладка «Иск» вела бы к строкам другого цвета. */
.pill{display:inline-block;white-space:nowrap;font-size:var(--t-2xs);
  padding:.15rem .5rem;border-radius:999px;border:1px solid}
.pill.k-order{color:var(--good);background:var(--good-bg);border-color:var(--good)}
.pill.k-claim{color:var(--accent-ink);background:var(--accent-soft);border-color:var(--accent)}
.pill.k-thin{color:var(--warn);background:var(--warn-bg);border-color:var(--warn)}
.pill.k-none{color:var(--ink-3);background:var(--surface-2);border-color:var(--line)}
/* Строка без суммы приглушена целиком: по ней всё равно нечего решать. */
#base-table tr[data-kind="none"] td{color:var(--ink-3)}
@media print{.tabs,.tools,#b-more{display:none}}
</style>"""

_SCRIPT = """<script>
(function () {
  var table = document.getElementById('base-table');
  if (!table) return;
  var rows = [].slice.call(table.querySelectorAll('tbody tr'));
  var find = document.getElementById('b-find');
  var sorter = document.getElementById('b-sort');
  var status = document.getElementById('b-status');
  var tabs = [].slice.call(document.querySelectorAll('.tabs .tab'));
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
  var kind = '';

  function num(row) { return parseInt(row.dataset.debt, 10) || 0; }
  var order = {
    name: function (a, b) { return a.dataset.name.localeCompare(b.dataset.name, 'ru'); },
    debt: function (a, b) { return num(b) - num(a); },
    'debt-asc': function (a, b) { return num(a) - num(b); }
  };

  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  function apply() {
    var matched = rows.filter(function (row) {
      if (kind && row.dataset.kind !== kind) return false;
      return !query || row.dataset.find.indexOf(query) >= 0;
    });
    matched.sort(order[mode] || order.name);
    rows.forEach(function (row) { row.hidden = true; });
    var shown = matched.slice(0, limit);
    var parent = rows.length ? rows[0].parentNode : null;
    shown.forEach(function (row) { row.hidden = false; if (parent) parent.appendChild(row); });

    var n = matched.length;
    // Счётчик показывается и при выбранной вкладке: «Ничего не найдено» без
    // напоминания о фильтре читается как «в базе никого нет».
    var word = plural(n, 'запись', 'записи', 'записей');
    status.textContent = (query || kind)
      ? (n ? 'Показано: ' + n + ' ' + word : 'Ничего не найдено')
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
  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      tabs.forEach(function (other) { other.classList.remove('on'); });
      tab.classList.add('on');
      kind = tab.dataset.kind;
      limit = STEP;
      apply();
    });
  });
  more.addEventListener('click', function () { limit += STEP; apply(); });
  apply();
})();
</script>"""


# ---------------------------------------------------------------- один человек


def render_person_page(
    debtor: Debtor,
    *,
    app_name: str,
    back_url: str = "",
    demo_mode: bool = False,
    print_mode: bool = False,
) -> str:
    """Сводка по одному должнику: всё, что о нём знает наша база.

    Открывается кликом из списка. Это ещё не отчёт по реестрам — отчёт стоит
    денег и живёт своей ссылкой. Здесь то, что известно бесплатно и сразу:
    кто он, сколько машин, из чего сложился долг и во что обойдётся суд.

    Разделять эти два экрана обязательно. Смешать их значило бы показать
    рядом проверенное и непроверенное одинаковым шрифтом — а весь продукт
    держится на том, что «не спрашивали» и «не нашли» выглядят по-разному.
    """
    from app.web.render import demo_banner

    parts: list[str] = []
    if demo_mode:
        parts.append(demo_banner())
    parts.append(_person_hero(debtor, back_url))
    parts.append(_person_facts(debtor))
    parts.append(_person_money(debtor))

    # Ссылку «ко всему списку» даёт шапка, а не навигация: navigation печатает
    # подпись текстом, и полный адрес встал бы на страницу вместе с токеном
    # доступа — тем самым, который открывает всю базу.
    nav = navigation(app_name, [("facts", "Что известно"), ("money", "Деньги")])
    body = _STYLE + "".join(parts)
    if print_mode:
        body += print_footer(app_name, utcnow())
    return document(title=f"{app_name} — должник", nav=nav, body=body)


def _person_hero(debtor: Debtor, back_url: str) -> str:
    back = f'<p class="hint"><a href="{e(back_url)}">← ко всему списку</a></p>' if back_url else ""
    born = debtor.birth_date.strftime("%d.%m.%Y") if debtor.birth_date else "—"
    return (
        '<header class="hero">'
        f"<h1>{e(debtor.fio or 'Без имени')}</h1>"
        f'<p class="hint">Дата рождения: {e(born)}</p>'
        f"{back}"
        "</header>"
    )


def _person_facts(debtor: Debtor) -> str:
    plates = debtor.vehicle_plates or debtor.vehicle_plate or "—"
    episodes = debtor.source_record_ids or debtor.external_debtor_id or "—"
    count = len([item for item in episodes.split(",") if item.strip()]) if episodes != "—" else 0
    rows = [
        ("Машины", plates),
        ("Задержаний", str(count) if count else "—"),
        ("ИД записей в учёте", episodes),
        ("Адрес", debtor.address or "—"),
        # Паспорт только маской: сам номер нужен мосту к ИНН, а не читателю.
        ("Паспорт", debtor.passport_masked or "—"),
        ("ИНН", debtor.inn or "—"),
        ("Договор", debtor.contract_number or "—"),
    ]
    body = table(
        ("Поле", "Значение"),
        [(cell(name, label="Поле"), cell(value, label="Значение")) for name, value in rows],
    )
    return section("facts", "Что известно", body)


def _person_money(debtor: Debtor) -> str:
    if debtor.debt_amount is None:
        return section(
            "money",
            "Деньги",
            '<p class="hint">Суммы долга нет: считать цену иска и пошлину не из чего.</p>',
        )
    amount = Decimal(str(debtor.debt_amount))
    order = court_order_fee(amount)
    claim = claim_fee(amount)
    note = (
        '<p class="hint">Сумма посчитана по тарифу из дат постановки и выдачи, '
        "а не взята из документа.</p>"
        if debtor.debt_is_estimated
        else ""
    )
    body = table(
        ("Что", "Сколько"),
        [
            (cell("Долг", label="Что"), cell(format_amount(amount), label="Сколько")),
            (
                cell("Пошлина: судебный приказ", label="Что"),
                cell(format_amount(order), label="Сколько"),
            ),
            (cell("Пошлина: иск", label="Что"), cell(format_amount(claim), label="Сколько")),
        ],
    )
    return section("money", "Деньги", body + note)
