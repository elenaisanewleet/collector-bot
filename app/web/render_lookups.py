"""Кого пробили по номеру телефона — и кого из них в базе нет.

Зачем отдельная страница, если есть справочник должников. Справочник отвечает
на вопрос «что мы знаем про этого человека» и живёт выгрузкой заказчика. Здесь
вопрос обратный и он про новых: владелец вводит номер, бот поднимает личность —
ФИО, дату рождения, паспорт, СНИЛС, ИНН, — и дальше эта личность либо
находится в выгрузке, либо нет. Второе и есть новый клиент: человек, которого
привезли, а в базе его ещё не завели.

Отметка «новый клиент» — замер СВОЕГО дня, а не текущего состояния базы. Она
посчитана в момент проверки и с тех пор не пересчитывается: следующий импорт
выгрузки изменит ответ, и молча переписать прошлое значило бы соврать о том,
что владелец видел, когда принимал решение. Считается по фамилии — почему
именно так, написано у :meth:`DebtorRepository.count_by_surname`.

Документы показываются целиком, а не масками, и это не небрежность. Страница
живёт за подписанной ссылкой с коротким сроком, выдаётся только владельцу и
гаснет по ``/revoke`` — ровно как справочник должников, где ФИО тоже не
вычёркиваются. Смысл страницы в том, чтобы из неё можно было заполнить
заявление; замаскированный паспорт превращает её в напоминание о том, что
паспорт где-то есть. Что попадёт в базу, решает не эта страница, а
``STORE_SENSITIVE_IDENTIFIERS``: при выключенном флаге здесь честно окажется
маска, потому что самого номера в базе не будет.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.db.models import PhoneLookup
from app.utils.dates import format_datetime, utcnow
from app.utils.formatting import pluralize_ru
from app.web.render import cell, document, e, navigation, print_footer, raw_cell, section, table

__all__ = ["render_lookups_page"]

_HEADERS = (
    "Когда",
    "ФИО",
    "Дата рождения",
    "Телефон",
    "Паспорт",
    "Выдан",
    "СНИЛС",
    "ИНН",
    "В базе",
)

EMPTY_NOTE = (
    "Здесь появятся все, кого вы пробьёте по номеру телефона. "
    "Отправьте боту номер — строка добавится сама."
)
NEW_NOTE = (
    "«Новый клиент» — значит, что на момент проверки никого с такой фамилией "
    "в базе не было. Отметка не пересчитывается: она про тот день, а не про сегодня."
)
DOC_NOTE = (
    "Паспорт, дата выдачи и СНИЛС пришли из проверки по номеру и не подтверждены "
    "документом. Перед подачей сверьте их с тем, что на руках."
)


def render_lookups_page(
    lookups: Sequence[PhoneLookup],
    *,
    app_name: str,
    demo_mode: bool = False,
    print_mode: bool = False,
) -> str:
    from app.web.render import demo_banner

    parts: list[str] = []
    if demo_mode:
        parts.append(demo_banner())
    parts.append(_hero(lookups))
    parts.append(_table(lookups))
    parts.append(f"<footer>{e(_footer(lookups))}</footer>")

    nav = navigation(app_name, [("lookups", "Проверки по номеру")])
    body = _STYLE + "".join(parts)
    if print_mode:
        body += print_footer(app_name, utcnow())
    else:
        body += _SCRIPT
    return document(title=f"{app_name} — новые клиенты", nav=nav, body=body)


def _hero(lookups: Sequence[PhoneLookup]) -> str:
    """Шапка: сколько пробили и сколько из них новых.

    Два числа, а не пять: страницу открывают ради одного вопроса — «кого надо
    завести». Остальное считается глазами по таблице.
    """
    total = len(lookups)
    fresh = sum(1 for lookup in lookups if lookup.is_new_client)
    known = total - fresh
    noun = pluralize_ru(total, "проверка", "проверки", "проверок")
    figures = "".join(
        (
            f'<div><span class="lbl">Новых клиентов</span><b>{fresh}</b>'
            f"<small>{e('в базе таких фамилий не было')}</small></div>",
            f'<div><span class="lbl">Уже в базе</span><b>{known}</b>'
            f"<small>{e('фамилия нашлась в выгрузке')}</small></div>",
        )
    )
    # Полоса вкладок — только когда есть что делить: фильтр, не отсекающий
    # ничего, читается как сломанный. То же правило, что в справочнике.
    tabs = (
        '<div class="tabs">'
        f'<button type="button" class="tab on" data-kind="">Все <span>{total}</span></button>'
        f'<button type="button" class="tab k-new" data-kind="new">'
        f"Новые клиенты <span>{fresh}</span></button>"
        f'<button type="button" class="tab k-known" data-kind="known">'
        f"Есть в базе <span>{known}</span></button>"
        "</div>"
        if fresh and known
        else ""
    )
    return (
        '<header class="hero" id="lookups">'
        f"<h1>{total} {e(noun)} по номеру</h1>"
        f'<div class="nums">{figures}</div>'
        f"{tabs}"
        '<div class="tools">'
        '<input id="l-find" type="search" placeholder="Поиск: фамилия, телефон, паспорт…" '
        'autocomplete="off" spellcheck="false">'
        "</div>"
        '<p class="hint" id="l-status"></p>'
        "</header>"
    )


def _document(full: str | None, masked: str | None) -> str:
    """Документ целиком, если он в базе есть, иначе его маска.

    Разница видна глазами и означает настройку развёртывания, а не пропуск в
    данных: ``STORE_SENSITIVE_IDENTIFIERS`` выключен — в базе только маска.
    """
    return full or masked or "—"


def _day(value: date | None) -> str:
    return value.strftime("%d.%m.%Y") if value else "—"


def _table(lookups: Sequence[PhoneLookup]) -> str:
    if not lookups:
        return section("lookups", "Проверки по номеру", f'<p class="hint">{e(EMPTY_NOTE)}</p>')

    cells: list[tuple[str, ...]] = []
    attrs: list[str] = []
    for lookup in lookups:
        passport = _document(lookup.passport, lookup.passport_masked)
        snils = _document(lookup.snils, lookup.snils_masked)
        birth = _day(lookup.birth_date)
        issued = _day(lookup.passport_issued)
        mark = (
            '<span class="pill k-new">Новый клиент</span>'
            if lookup.is_new_client
            else f'<span class="pill k-known">{lookup.base_matches}</span>'
        )
        cells.append(
            (
                cell(format_datetime(lookup.created_at), label="Когда"),
                raw_cell(f"<b>{e(lookup.fio or '—')}</b>", label="ФИО"),
                cell(birth, label="Дата рождения"),
                cell(lookup.phone_masked or "—", label="Телефон"),
                # Копируется одним нажатием: страницу открывают затем, чтобы
                # перенести эти цифры в заявление, и перебивать их руками —
                # это опечатка в иске.
                cell(passport, label="Паспорт", copy=passport != "—"),
                cell(issued, label="Выдан"),
                cell(snils, label="СНИЛС", copy=snils != "—"),
                cell(lookup.inn or "—", label="ИНН", copy=bool(lookup.inn)),
                raw_cell(mark, label="В базе"),
            )
        )
        haystack = " ".join(
            filter(
                None,
                (lookup.fio, birth, lookup.phone_masked, passport, issued, snils, lookup.inn),
            )
        ).lower()
        kind = "new" if lookup.is_new_client else "known"
        attrs.append(f'data-find="{e(haystack)}" data-kind="{kind}"')

    body = f'<div id="lookups-table">{table(_HEADERS, cells, row_attrs=attrs)}</div>'
    notes = f'<p class="hint">{e(NEW_NOTE)}</p><p class="hint">{e(DOC_NOTE)}</p>'
    return section("lookups", "Проверки по номеру", body + notes)


def _footer(lookups: Sequence[PhoneLookup]) -> str:
    return f"Список на {format_datetime(utcnow())}. Записей: {len(lookups)}."


_STYLE = """<style>
.tools{display:flex;gap:.5rem;margin:.75rem 0}
.tools input{flex:1;min-width:0}
#lookups-table td{font-variant-numeric:tabular-nums}
#lookups-table td:nth-child(2){font-variant-numeric:normal}

/* Вкладки живут в герое и берут геройские цвета: обычные --ink-* на тёмной
   подложке не читаются вовсе. Разметка та же, что в справочнике должников. */
.tabs{display:flex;flex-wrap:wrap;gap:.4rem;margin:.9rem 0 .2rem}
.tabs .tab{font:inherit;font-size:var(--t-xs);cursor:pointer;
  padding:.3rem .7rem;border-radius:999px;
  border:1px solid var(--hero-dim);background:transparent;color:var(--hero-soft)}
.tabs .tab span{opacity:.65;margin-left:.25rem;font-variant-numeric:tabular-nums}
.tabs .tab:hover{border-color:var(--hero-ink);color:var(--hero-ink)}
.tabs .tab.on{background:var(--hero-ink);border-color:var(--hero-ink);color:var(--hero)}
.tabs .tab.on span{opacity:.55}

/* Новый клиент — это работа, которую надо сделать, поэтому он акцентный, а не
   тревожный: ничего не сломалось, человека просто ещё не завели. */
.pill{display:inline-block;white-space:nowrap;font-size:var(--t-2xs);
  padding:.15rem .5rem;border-radius:999px;border:1px solid}
.pill.k-new{color:var(--accent-ink);background:var(--accent-soft);border-color:var(--accent)}
.pill.k-known{color:var(--ink-3);background:var(--surface-2);border-color:var(--line)}
@media print{.tabs,.tools{display:none}}
</style>"""

_SCRIPT = """<script>
(function () {
  var table = document.getElementById('lookups-table');
  if (!table) return;
  var rows = [].slice.call(table.querySelectorAll('tbody tr'));
  var find = document.getElementById('l-find');
  var status = document.getElementById('l-status');
  var tabs = [].slice.call(document.querySelectorAll('.tabs .tab'));
  var query = '';
  var kind = '';

  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few;
    return many;
  }

  function apply() {
    var shown = 0;
    rows.forEach(function (row) {
      var ok = (!kind || row.dataset.kind === kind)
        && (!query || row.dataset.find.indexOf(query) >= 0);
      row.hidden = !ok;
      if (ok) shown++;
    });
    // Счётчик показывается и при выбранной вкладке: «Ничего не найдено» без
    // напоминания о фильтре читается как «здесь пусто».
    var word = plural(shown, 'запись', 'записи', 'записей');
    status.textContent = (query || kind)
      ? (shown ? 'Показано: ' + shown + ' ' + word : 'Ничего не найдено')
      : '';
  }

  var timer = null;
  find.addEventListener('input', function () {
    clearTimeout(timer);
    timer = setTimeout(function () {
      query = find.value.trim().toLowerCase();
      apply();
    }, 120);
  });
  tabs.forEach(function (tab) {
    tab.addEventListener('click', function () {
      tabs.forEach(function (other) { other.classList.remove('on'); });
      tab.classList.add('on');
      kind = tab.dataset.kind;
      apply();
    });
  });
  apply();
})();
</script>"""
