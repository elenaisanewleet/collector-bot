"""Отрисовка веб-отчёта.

Отчёт по ссылке, а не простынёй в чат: в сообщении Telegram нет ни таблиц, ни
навигации, а смотреть надо на десятки строк производств сразу, сравнивать
суммы и копировать номера дел.

Правила разметки те же, что и у текстового отчёта, и это не совпадение —
инвариант проекта живёт в данных, а не в оформлении:

*   источник, который не ответил, показывается как «не проверено», и никогда
    как «ничего не найдено»;
*   формулировки этих состояний берутся из :mod:`app.services.reporting` и
    нигде не переписываются: второй набор слов неизбежно разъедется с первым;
*   у каждой записи видно, насколько уверенно она сопоставлена с должником;
*   вердикт всегда идёт вместе с причиной и с тем, по скольким источникам он
    посчитан.

Шаблонизатора нет намеренно: страница одна, зависимость лишняя, а каждая
секция — обычная функция, которую можно проверить тестом по отдельности.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from html import escape

from app.domain.enums import (
    BANKRUPTCY_STATUS_TITLES,
    BUSINESS_ROLE_TITLES,
    BUSINESS_STATUS_TITLES,
    COURT_CASE_ROLE_TITLES,
    MATCH_LEVEL_TITLES,
    PLEDGE_STATUS_TITLES,
    PROVIDER_TITLES,
    SCORE_CATEGORY_TITLES,
    BankruptcyStatus,
    BusinessStatus,
    MatchLevel,
    PledgeStatus,
    ProviderName,
    ScoreCategory,
)
from app.domain.models import (
    DebtorReport,
    InheritanceCase,
    InternalDebtorRecord,
    ProviderResult,
)
from app.domain.scoring import PROVIDER_CONFIDENCE_WEIGHTS
from app.domain.verdict import FEE_BASIS_TITLES, VERDICT_TITLES, VerdictDecision
from app.services.reporting import (
    COURT_SCOPE_NOTE,
    DEMO_BANNER,
    INHERITANCE_SCOPE_NOTE,
    NO_FACTORS_NOTE,
    PLEDGE_SCOPE_NOTE,
    SourceState,
    SourceStateCode,
    empty_reason,
    source_state,
    unanswered_line,
)
from app.utils.dates import format_date, format_datetime
from app.utils.formatting import pluralize_ru
from app.utils.masking import mask_phone, mask_vin
from app.utils.money import format_amount
from app.web.style import CSS

DISCLAIMER = (
    "Оценка аналитическая и не заменяет юридическую проверку. "
    "Ссылка временная и открывается без пароля — не пересылайте её посторонним."
)
PRINT_HINT = (
    "Печать: в диалоге печати выключите «Колонтитулы» — иначе браузер допишет "
    "на лист адрес этой страницы вместе с токеном доступа. Дата формирования и "
    "название системы уже стоят внизу каждого листа."
)
# Ниже этой уверенности шкала оценки приглушается: считать её измеренной
# величиной, когда половина источников молчала, нельзя.
LOW_CONFIDENCE = 0.5
# Источники, по которым считается покрытие. Тот же список, по которому
# считается уверенность оценки, — чтобы «ответили N из M» и confidence не
# расходились.
EXPECTED_PROVIDERS: tuple[ProviderName, ...] = tuple(
    ProviderName(key) for key in PROVIDER_CONFIDENCE_WEIGHTS
)


def e(value: object) -> str:
    """Экранирование. Всё, что приходит из данных, проходит через него."""
    return escape("" if value is None else str(value), quote=True)


@dataclass(frozen=True, slots=True)
class ExportLinks:
    """Адреса выгрузки за тем же токеном, что и страница."""

    text_url: str
    print_url: str


@dataclass(frozen=True, slots=True)
class Block:
    """Секция страницы: якорь, заголовок и готовая разметка.

    Секции собираются списком, а не печатаются подряд: тогда оглавление
    строится из фактически отрисованного, и мёртвая ссылка на несуществующий
    раздел становится невозможной по конструкции.
    """

    anchor: str
    title: str
    html: str


# ---------------------------------------------------------------- каркас


def document(
    *,
    title: str,
    nav: str,
    body: str,
    foot: str = "",
    auto_print: bool = False,
) -> str:
    """Каркас страницы.

    Заголовок документа нейтрален и не содержит ФИО: если оператор вставит
    адрес текстом в чат, превью-робот вытащит именно ``<title>``, и фамилия
    должника уедет в кэш стороннего сервиса.
    """
    print_script = (
        "<script>window.addEventListener('load',function(){window.print()});</script>"
        if auto_print
        else ""
    )
    return (
        "<!doctype html>\n"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        # Страница с персональными данными не должна попадать в поисковики.
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        '<meta name="referrer" content="no-referrer">'
        # Нейтральная карточка для любого линк-превьюера: ФИО остаётся в H1.
        f'<meta property="og:title" content="{e(title)}">'
        '<meta property="og:description" content="Отчёт открывается по временной ссылке.">'
        '<meta name="twitter:card" content="summary">'
        f"<title>{e(title)}</title>"
        f"<style>{CSS}</style></head>"
        f'<body><div class="shell">{nav}<main>{body}</main></div>{foot}'
        f"{_SCRIPT}{print_script}</body></html>"
    )


_SCRIPT = """<script>
document.addEventListener('click', function (event) {
  var el = event.target.closest('.copy');
  if (!el || el.dataset.done === '1') return;
  // Текст берётся только из data-copy: раньше повторный тап копировал слово
  // «скопировано» и затирал им номер производства. Возвращается на место
  // подпись, снятая до подмены: в очереди кнопка показывает «12 400 ₽», а
  // копирует «12400» — в исковое заявление вставляют число, а не рубли.
  var text = el.dataset.copy || '';
  var shown = el.textContent;
  var mark = function (label) {
    el.dataset.done = '1';
    el.textContent = label;
    setTimeout(function () { el.textContent = shown; el.dataset.done = ''; }, 1200);
  };
  if (!navigator.clipboard) { mark('выделите вручную'); return; }
  navigator.clipboard.writeText(text).then(
    function () { mark('скопировано'); },
    function () { mark('не удалось'); }
  );
});
// Свёрнутые блоки на бумаге раскрываются: скрытая строка — потерянный факт.
window.addEventListener('beforeprint', function () {
  document.querySelectorAll('details').forEach(function (item) { item.open = true; });
});
</script>"""


def navigation(brand: str, items: Sequence[tuple[str, str]], meta: Sequence[str] = ()) -> str:
    links = "".join(f'<li><a href="#{e(anchor)}">{e(title)}</a></li>' for anchor, title in items)
    note = "".join(f"<div>{e(line)}</div>" for line in meta)
    body = f"<ol>{links}</ol>" if links else ""
    return f'<nav><div class="brand">{e(brand)}</div>{body}<div class="meta">{note}</div></nav>'


def section(anchor: str, title: str, body: str, *, state: SourceState | None = None) -> str:
    """Секция с заголовком.

    В заголовке — не порядковый номер (это классификация, а не
    последовательность), а состояние источника: то самое, что определяет,
    можно ли верить содержимому раздела.
    """
    mark = state_tag(state) if state is not None else ""
    return f'<section class="card" id="{e(anchor)}"><h2>{e(title)}{mark}</h2>{body}</section>'


def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    row_attrs: Sequence[str] = (),
) -> str:
    """Таблица. ``row_attrs`` — атрибуты строк, по одному на строку."""
    head = "".join(f"<th>{e(header)}</th>" for header in headers)
    attrs = list(row_attrs) + [""] * (len(rows) - len(row_attrs))
    body = "".join(
        f"<tr{' ' + attr if attr else ''}>" + "".join(cells) + "</tr>"
        for attr, cells in zip(attrs, rows, strict=True)
    )
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def cell(
    value: object,
    *,
    label: str = "",
    numeric: bool = False,
    right: bool = False,
    copy: bool = False,
) -> str:
    """Ячейка таблицы.

    ``label`` дублирует заголовок колонки в ``data-l``: на телефоне таблица
    разворачивается в карточки, и подпись поля берётся оттуда, а не остаётся
    за горизонтальным скроллом.
    """
    classes = " ".join(filter(None, ["n" if numeric else "", "r" if right else ""]))
    attrs = f' class="{classes}"' if classes else ""
    attrs += f' data-l="{e(label)}"' if label else ""
    text = e(value if value not in (None, "") else "—")
    if copy and value:
        inner = f'<button type="button" class="copy" data-copy="{text}">{text}</button>'
    elif numeric and value:
        inner = f'<span class="plate">{text}</span>'
    else:
        inner = text
    return f"<td{attrs}>{inner}</td>"


def raw_cell(html: str, *, label: str = "", value: str = "", classes: str = "") -> str:
    """Ячейка с готовой разметкой. ``value`` уезжает в ``data-v`` для фильтров.

    ``classes`` — те же ``n``/``r``, что и у :func:`cell`: колонка с суммой не
    перестаёт быть колонкой с суммой оттого, что внутри неё кнопка.
    """
    attr = f' class="{e(classes)}"' if classes else ""
    attr += f' data-l="{e(label)}"' if label else ""
    attr += f' data-v="{e(value)}"' if value else ""
    return f"<td{attr}>{html}</td>"


def match_tag(level: MatchLevel) -> str:
    """Уровень сопоставления личности. Не прячется ни в одной таблице."""
    tone, mark = {
        MatchLevel.CONFIRMED: ("good", "✓"),
        MatchLevel.PROBABLE: ("warn", "~"),
        MatchLevel.WEAK: ("plain", "?"),
    }[level]
    return (
        f'<span class="tag {tone}"><span class="mark">{mark}</span>'
        f"{e(MATCH_LEVEL_TITLES[level])}</span>"
    )


def state_tag(state: SourceState) -> str:
    """Чип состояния источника.

    Форма и знак несут смысл раньше цвета: все четыре непроверенных состояния
    получают один контурный штрихованный класс, и на чёрно-белой распечатке
    «не проверено» остаётся отличимым от «проверено, записей нет».
    """
    tone = "unchecked" if state.is_unchecked else _answered_tone(state)
    return (
        f'<span class="tag {tone}"><span class="mark">{e(state.mark)}</span>{e(state.label)}</span>'
    )


def _answered_tone(state: SourceState) -> str:
    # Найденные записи не красятся зелёным: для взыскателя пять производств в
    # ФССП — плохая новость, а зелёный на странице значит «можно взыскать».
    if state.code is SourceStateCode.PARTIAL:
        # Ответ есть, но источник сам сказал, что прислал не всё: приглушённый
        # чип прочитался бы как «всё спокойно».
        return "warn"
    return "plain" if state.code is SourceStateCode.FOUND else "mute"


# ---------------------------------------------------------------- секции


def demo_banner() -> str:
    """Пометка демо-режима.

    Без неё пересланная ссылка на выдуманные данные неотличима от настоящей
    проверки — это хуже, чем непроверенное, поданное как чистое.
    """
    return f'<div class="demo">{e(DEMO_BANNER)}</div>'


def hero(
    report: DebtorReport,
    decision: VerdictDecision,
    *,
    exports: ExportLinks | None = None,
) -> str:
    """Ответ на главный вопрос — крупно и первым.

    Страницу открывают не «посмотреть данные», а решить, нести ли этого
    должника в суд. Поэтому вердикт, долг, пошлина, балл и покрытие источников
    стоят до всех таблиц, а кнопки выгрузки — рядом с ними.
    """
    score = report.recovery_score
    numbers = [
        f'<div><span class="lbl">Наш долг</span>'
        f"<b>{e(format_amount(decision.debt_amount))}</b></div>"
    ]
    if decision.state_fee is not None:
        numbers.append(
            f'<div><span class="lbl">Пошлина</span>'
            f"<b>{e(format_amount(decision.state_fee))}</b>"
            f"<small>{e(FEE_BASIS_TITLES[decision.fee_basis])}</small></div>"
        )
    if score is not None:
        numbers.append(
            f'<div><span class="lbl">Recovery Score</span>'
            f"<b>{score.score} / 100</b>"
            f"<small>уверенность {round(score.confidence * 100)}%</small></div>"
        )

    reasons = "".join(f"<li>{e(reason.text)}</li>" for reason in decision.reasons)
    reason_list = f'<ul class="reasons">{reasons}</ul>' if reasons else ""
    cached = (
        f'<p class="sub">Данные проверки от {e(format_datetime(report.cached_at))}</p>'
        if report.from_cache
        else ""
    )
    return (
        f'<header class="hero">'
        f'<div class="badge {e(decision.verdict.value)}"><i></i>'
        f"{e(VERDICT_TITLES[decision.verdict])}</div>"
        f"<h1>{e(report.subject.display_name)}</h1>"
        f'<p class="sub">{e(_subject_line(report))}</p>{cached}'
        f'<p class="why">{e(decision.headline)}</p>{reason_list}'
        f'<div class="nums">{"".join(numbers)}</div>'
        f"{coverage_line(report)}{_export_actions(exports)}</header>"
    )


def coverage_line(report: DebtorReport) -> str:
    """Сколько источников на самом деле ответило — рядом с вердиктом.

    Раньше это было видно только в разделе внизу страницы, и вердикт с
    пошлиной объявлялись так, будто данные полные.

    Молчавшие перечисляются всегда. Отдельно выделяется случай, когда молчал
    источник, на котором держатся вердикт и оценка: неподключённый справочник
    недвижимости и упавшая ФССП — разной цены новости, и подавать их одинаково
    значит обесценить предупреждение.
    """
    answered, total, missing = coverage(report)
    counter = f"Источники: ответили <b>{answered} из {total}</b>"
    if not missing:
        return f'<p class="coverage">{counter}. <a href="#sources">Показать список</a></p>'
    warning = " — вердикт посчитан по неполным данным" if scored_gap(report) else ""
    tone = " gap" if warning else ""
    return (
        f'<p class="coverage{tone}">{counter}{warning}. '
        f'<a href="#sources">Показать список</a>'
        f'<span class="miss">Не проверено: {e(", ".join(missing))}</span></p>'
    )


def scored_gap(report: DebtorReport) -> list[str]:
    """Молчавшие источники из числа тех, по которым считается оценка."""
    titles = {PROVIDER_TITLES[provider] for provider in EXPECTED_PROVIDERS}
    return [
        title for title, state in _source_rows(report) if state.is_unchecked and title in titles
    ]


def coverage(report: DebtorReport) -> tuple[int, int, list[str]]:
    """Ответили / всего ожидаемых / названия молчавших."""
    rows = _source_rows(report)
    answered = sum(1 for _, state in rows if not state.is_unchecked)
    missing = [title for title, state in rows if state.is_unchecked]
    return answered, len(rows), missing


def _source_rows(report: DebtorReport) -> list[tuple[str, SourceState]]:
    """Ожидаемый набор источников, а не только те, чей результат существует.

    Источник, для которого результат вообще не создался — нет в реестре, отчёт
    из старого кэша, — иначе просто исчезал бы из списка, и список выглядел бы
    полным.
    """
    rows: list[tuple[str, SourceState]] = [
        (
            PROVIDER_TITLES.get(result.provider, result.provider.value),
            _state_of(report, result),
        )
        for result in report.provider_results
    ]
    seen = {result.provider for result in report.provider_results}
    rows.extend(
        (PROVIDER_TITLES[provider], source_state(None))
        for provider in EXPECTED_PROVIDERS
        if provider not in seen
    )
    return rows


def _state_of(report: DebtorReport, result: ProviderResult) -> SourceState:
    """Состояние источника с правильным счётчиком записей.

    У внутренней базы записи лежат в самом отчёте, а не в результате: класть их
    ещё и туда значило бы удвоить их в отчёте и потерять доверие к точному
    совпадению по нашему же идентификатору.
    """
    if result.provider is ProviderName.INTERNAL:
        return source_state(result, records=len(report.internal_records))
    return source_state(result)


def internal_section(report: DebtorReport) -> str:
    """Наши данные.

    Внутренняя база проходит через тот же разбор состояний, что и внешние
    источники: упавшая БД или нечитаемая выгрузка не должны давать ту же
    строку, что честный пустой ответ.
    """
    result = report.result_for(ProviderName.INTERNAL)
    state = _state_of(report, result) if result is not None else source_state(None)
    unchecked = _unchecked(result)
    if unchecked:
        return section("internal", "Наши данные", unchecked, state=state)

    record = report.internal_record
    if record is None:
        return section(
            "internal",
            "Наши данные",
            '<p class="empty">Совпадений во внутренней базе нет.</p>' + _checked_note(result),
            state=state,
        )
    facts = _facts_grid(_internal_facts(record))
    extra = len(report.internal_records) - 1
    note = (
        f'<p class="note">Ещё {extra} похожих записей во внутренней базе.</p>' if extra > 0 else ""
    )
    return section("internal", "Наши данные", facts + _address_block(record) + note, state=state)


def _internal_facts(record: InternalDebtorRecord) -> list[tuple[str, str, str, str]]:
    phone = record.phone_masked or mask_phone(record.phone)
    rows: list[tuple[str, str, str, str]] = [
        ("ФИО", record.full_name or "—", "", ""),
        ("Дата рождения", format_date(record.birth_date), "", ""),
        ("Телефон", phone or "—", "", "маскирован"),
        ("Договор", record.contract_number or "—", "", ""),
        (
            "Задолженность",
            format_amount(record.debt_amount),
            "hit" if record.debt_amount else "none",
            "",
        ),
    ]
    if record.vehicle_plate:
        rows.append(("Госномер", record.vehicle_plate, "", ""))
    if record.vin:
        rows.append(("VIN", mask_vin(record.vin) or "—", "", "маскирован"))
    return rows


def _address_block(record: InternalDebtorRecord) -> str:
    """Адрес — за раскрывающимся блоком, и это решение, а не недосмотр.

    Ни в вердикте, ни в оценке адрес не участвует, а страница открывается без
    пароля и пересылается. Пусть он не попадает ни на первый экран, ни в
    случайный скриншот; при печати блок раскрывается вместе со всеми.
    """
    if not record.address:
        return ""
    return (
        '<details class="note"><summary>Показать адрес проживания</summary>'
        f'<div class="num">{e(record.address)}</div></details>'
    )


def enforcement_section(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.FSSP)
    state = source_state(result)
    unchecked = _unchecked(result)
    if unchecked:
        return section("fssp", "ФССП", unchecked, state=state)

    active = report.active_proceedings
    hidden = _hidden_note(len(report.enforcement_proceedings), len(active))
    if not active:
        return section(
            "fssp",
            "ФССП",
            '<p class="empty">Активных исполнительных производств не найдено.</p>'
            + hidden
            + _checked_note(result),
            state=state,
        )

    rows = [
        (
            cell(item.proceeding_number, label="Производство", numeric=True, copy=True),
            cell(format_amount(item.amount), label="Сумма", numeric=True, right=True),
            cell(item.subject, label="Предмет"),
            cell(item.department, label="Отдел"),
            raw_cell(match_tag(item.match_level), label="Совпадение"),
        )
        for item in active
    ]
    return section(
        "fssp",
        "ФССП",
        table(("Производство", "Сумма", "Предмет", "Отдел", "Совпадение"), rows)
        + _enforcement_summary(report, len(active))
        + hidden
        + _checked_note(result),
        state=state,
    )


def _enforcement_summary(report: DebtorReport, count: int) -> str:
    """Итог по производствам.

    Ноль вместо «неизвестно» здесь опаснее всего: это единственная цифра, по
    которой оператор прикидывает, сколько кредиторов уже стоит за деньгами
    должника. Если сумм источник не сообщил, так и сказано.
    """
    known = [item for item in report.active_proceedings if item.amount is not None]
    total = report.total_enforcement_amount
    if not known or total == Decimal("0"):
        return (
            f'<p class="note">Активных производств: {count}. '
            f"Подтверждённая сумма: неизвестна — источник не сообщил сумм.</p>"
        )
    detail = "" if len(known) == count else f" (сумма известна по {len(known)} из {count})"
    return (
        f'<p class="note">Активных производств: {count}. '
        f"Подтверждённая сумма: {e(format_amount(total))}{detail}.</p>"
    )


def _hidden_note(total: int, shown: int) -> str:
    """Сколько записей источник вернул сверх показанных.

    Отфильтрованное молча превращалось в отсутствующее: раздел писал «не
    найдено», а список источников тут же сообщал «2 зап.».
    """
    hidden = total - shown
    if hidden <= 0:
        return ""
    return (
        f'<p class="note">Источник вернул ещё {hidden} записей, которые сюда не попали: '
        f"слабое совпадение с должником либо непрочитанный статус записи.</p>"
    )


def bankruptcy_section(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.FEDRESURS)
    state = source_state(result)
    unchecked = _unchecked(result, report.result_for(ProviderName.INN_BRIDGE))
    if unchecked:
        return section("bankruptcy", "Банкротство", unchecked, state=state)

    usable = [item for item in report.bankruptcies if item.is_usable]
    if not usable:
        return section(
            "bankruptcy",
            "Банкротство",
            _empty_body(result, "Не обнаружено.", found=len(report.bankruptcies), noun="запись"),
            state=state,
        )

    rows = [
        (
            cell(item.case_number, label="Дело", numeric=True, copy=True),
            cell(item.procedure, label="Процедура"),
            raw_cell(_bankruptcy_status(item.status), label="Статус"),
            cell(format_date(item.started_at), label="Начало", numeric=True),
            raw_cell(match_tag(item.match_level), label="Совпадение"),
        )
        for item in usable
    ]
    return section(
        "bankruptcy",
        "Банкротство",
        table(("Дело", "Процедура", "Статус", "Начало", "Совпадение"), rows)
        + _checked_note(result),
        state=state,
    )


def _bankruptcy_status(status: BankruptcyStatus) -> str:
    """Три состояния, а не булев флаг.

    ``UNKNOWN`` — «дело найдено, состояние не прочитано». Напечатанное как
    «завершено», оно читается взыскателем как «путь свободен», и он платит
    пошлину в никуда.
    """
    tone = {
        BankruptcyStatus.ACTIVE: "crit",
        BankruptcyStatus.COMPLETED: "mute",
        BankruptcyStatus.UNKNOWN: "warn",
    }.get(status, "warn")
    title = BANKRUPTCY_STATUS_TITLES.get(status, "состояние процедуры не определено")
    return f'<span class="tag {tone}">{e(title)}</span>'


def business_section(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.FNS)
    state = source_state(result)
    unchecked = _unchecked(result, report.result_for(ProviderName.INN_BRIDGE))
    if unchecked:
        return section("business", "Бизнес", unchecked, state=state)

    usable = [item for item in report.business_relations if item.is_usable]
    if not usable:
        return section(
            "business",
            "Бизнес",
            _empty_body(
                result,
                "Связей с ИП и юрлицами не найдено.",
                found=len(report.business_relations),
                noun="связь",
            ),
            state=state,
        )

    rows = [
        (
            cell(BUSINESS_ROLE_TITLES.get(item.role, "связь"), label="Роль"),
            cell(item.name, label="Наименование"),
            cell(item.inn, label="ИНН", numeric=True, copy=True),
            raw_cell(_business_status(item.status), label="Статус"),
            raw_cell(match_tag(item.match_level), label="Совпадение"),
        )
        for item in usable
    ]
    return section(
        "business",
        "Бизнес",
        table(("Роль", "Наименование", "ИНН", "Статус", "Совпадение"), rows)
        + _checked_note(result),
        state=state,
    )


def _business_status(status: BusinessStatus) -> str:
    """Тоже три состояния: непрочитанное, поданное как «прекращено», прячет от
    оператора живое ИП — а для взыскания это плюс к перспективе."""
    tone = {
        BusinessStatus.ACTIVE: "good",
        BusinessStatus.TERMINATED: "mute",
        BusinessStatus.UNKNOWN: "warn",
    }.get(status, "warn")
    title = BUSINESS_STATUS_TITLES[status]
    return f'<span class="tag {tone}">{e(title)}</span>'


def pledge_section(report: DebtorReport) -> str:
    """Залоги.

    Ради этого раздела страницу и открывают: действующий залог на машину
    должника означает, что впереди нас стоит другой кредитор. Оговорка об
    охвате печатается в обоих случаях — и когда записи есть, и когда их нет,
    иначе «не найдено» читается шире проверенного.
    """
    result = report.result_for(ProviderName.PLEDGE)
    state = source_state(result)
    scope = f'<p class="note scope">{e(PLEDGE_SCOPE_NOTE)}</p>'
    unchecked = _unchecked(result)
    if unchecked:
        return section("pledge", "Залоги", unchecked + scope, state=state)

    usable = [item for item in report.pledges if item.is_usable]
    if not usable:
        return section(
            "pledge",
            "Залоги",
            _empty_body(
                result,
                "Записей в реестре залогов не найдено.",
                found=len(report.pledges),
                noun="запись",
                scope=scope,
            ),
            state=state,
        )

    rows = [
        (
            cell(item.subject, label="Предмет"),
            raw_cell(_pledge_status(item.status), label="Состояние"),
            cell(item.pledgee_name, label="Залогодержатель"),
            cell(mask_vin(item.vin) if item.vin else None, label="VIN", numeric=True),
            cell(item.registration_number, label="Уведомление", numeric=True, copy=True),
            raw_cell(match_tag(item.match_level), label="Совпадение"),
        )
        for item in usable
    ]
    return section(
        "pledge",
        "Залоги",
        table(
            ("Предмет", "Состояние", "Залогодержатель", "VIN", "Уведомление", "Совпадение"),
            rows,
        )
        + _checked_note(result, scope=scope),
        state=state,
    )


def _pledge_status(status: PledgeStatus) -> str:
    tone = {
        PledgeStatus.ACTIVE: "crit",
        PledgeStatus.TERMINATED: "mute",
        PledgeStatus.UNKNOWN: "warn",
    }.get(status, "warn")
    title = PLEDGE_STATUS_TITLES.get(status, "состояние записи не определено")
    return f'<span class="tag {tone}">{e(title)}</span>'


def inheritance_section(report: DebtorReport) -> str:
    """Наследственные дела.

    Подтверждённые и возможные разведены намеренно и по-разному. Подтверждённое
    дело — таблицей и с прямым выводом над ней: должник умер, иск к нему не
    подать. Возможные — за свёрнутым блоком с честным заголовком «однофамильцы,
    сопоставить не удалось»: реестр ищет по одному ФИО, и таблица чужих дел
    на первом экране читается как «вот что нашли про него», чего никакой чип в
    последней колонке не перебивает. На печати блок раскрывается вместе со
    всеми — скрытая строка не должна превращаться в потерянный факт.
    """
    result = report.result_for(ProviderName.INHERITANCE)
    state = source_state(result)
    scope = f'<p class="note scope">{e(INHERITANCE_SCOPE_NOTE)}</p>'
    unchecked = _unchecked(result)
    if unchecked:
        return section("inheritance", "Наследственные дела", unchecked + scope, state=state)

    usable = [item for item in report.inheritance_cases if item.is_usable]
    if not usable:
        return section(
            "inheritance",
            "Наследственные дела",
            _empty_body(
                result,
                "Наследственных дел по этому ФИО не найдено.",
                found=len(report.inheritance_cases),
                noun="дело",
                scope=scope,
            ),
            state=state,
        )

    confirmed = [item for item in usable if item.is_confirmed]
    probable = [item for item in usable if not item.is_confirmed]
    body = ""
    if confirmed:
        body += (
            '<p class="empty unchecked">Должник умер: дата рождения в записи реестра '
            "совпала. Иск к нему суд не примет — требование предъявляется наследникам "
            "или к наследственному имуществу.</p>"
        ) + _inheritance_table(confirmed)
    else:
        # Оговорки источника («найдено 1730, сопоставить не удалось ни одного»)
        # печатаются ДО таблицы, когда подтверждать нечего: иначе список дел с
        # фамилией должника открывает раздел без объяснения, чей он.
        notes = result.notes if result is not None else ()
        body += "".join(f'<p class="note">{e(note)}</p>' for note in notes)
    if probable:
        noun = pluralize_ru(len(probable), "однофамилец", "однофамильца", "однофамильцев")
        body += (
            f'<details class="note"><summary>{len(probable)} {noun}: '
            "сопоставить с должником не удалось</summary>"
            f"{_inheritance_table(probable)}</details>"
        )
    return section(
        "inheritance",
        "Наследственные дела",
        body + _checked_note(result, scope=scope, notes=bool(confirmed)),
        state=state,
    )


def _inheritance_table(items: Sequence[InheritanceCase]) -> str:
    """Колонка «Рождение» — не украшение, а само доказательство.

    Дата рождения наследодателя — единственный признак, по которому запись
    подтверждается или отбраковывается, и в таблице её не было: чип
    «подтверждено» стоял рядом с датой смерти и фамилией, а то, из-за чего он
    там стоит, оператору показано не было. Пустая ячейка при этом так же
    осмысленна, как заполненная: она и означает «сопоставить нечем».
    """
    rows = [
        (
            cell(item.case_number, label="Дело", numeric=True, copy=True),
            cell(item.deceased_name, label="Наследодатель"),
            cell(format_date(item.deceased_birth_date), label="Рождение", numeric=True),
            cell(format_date(item.death_date), label="Смерть", numeric=True),
            cell("открыто" if item.is_open else "закрыто", label="Состояние"),
            cell(item.notary_name, label="Нотариус"),
            raw_cell(match_tag(item.match_level), label="Совпадение"),
        )
        for item in items
    ]
    return table(
        ("Дело", "Наследодатель", "Рождение", "Смерть", "Состояние", "Нотариус", "Совпадение"),
        rows,
    )


def court_section(report: DebtorReport) -> str:
    """Арбитраж — и только он, о чём раздел говорит прямо."""
    result = report.result_for(ProviderName.COURT)
    state = source_state(result)
    scope = f'<p class="note scope">{e(COURT_SCOPE_NOTE)}</p>'
    unchecked = _unchecked(result, report.result_for(ProviderName.INN_BRIDGE))
    if unchecked:
        return section("court", "Суды", unchecked + scope, state=state)

    usable = [item for item in report.court_cases if item.is_usable]
    if not usable:
        return section(
            "court",
            "Суды",
            _empty_body(
                result,
                "Арбитражных дел не найдено.",
                found=len(report.court_cases),
                noun="дело",
                scope=scope,
            ),
            state=state,
        )

    rows = [
        (
            cell(item.case_number, label="Дело", numeric=True, copy=True),
            cell(COURT_CASE_ROLE_TITLES.get(item.role, "участник"), label="Роль"),
            cell(format_amount(item.amount), label="Сумма", numeric=True, right=True),
            cell(item.case_type, label="Категория"),
            cell(item.court_name, label="Суд"),
            raw_cell(match_tag(item.match_level), label="Совпадение"),
        )
        for item in usable
    ]
    return section(
        "court",
        "Суды",
        table(("Дело", "Роль", "Сумма", "Категория", "Суд", "Совпадение"), rows)
        + _checked_note(result, scope=scope),
        state=state,
    )


def score_section(report: DebtorReport) -> str:
    score = report.recovery_score
    if score is None:
        return ""
    category = SCORE_CATEGORY_TITLES.get(ScoreCategory(score.category), score.category)
    factors = "".join(
        f'<div class="frow {"plus" if factor.delta > 0 else "minus"}">'
        f'<span class="d">{factor.delta:+d}</span><span>{e(factor.reason)}</span></div>'
        for factor in score.factors
    )
    if not score.factors:
        # Иначе шкала «50 / 100» подаётся как измеренная средняя перспектива,
        # хотя это просто базовое значение, к которому ничего не прибавилось.
        factors = f'<p class="empty unchecked">{e(NO_FACTORS_NOTE)}</p>'

    confidence = round(score.confidence * 100)
    thin = " thin" if score.confidence < LOW_CONFIDENCE else ""
    caveat = (
        '<p class="empty unchecked">Оценка на неполных данных: часть источников не ответила.</p>'
        if score.confidence < LOW_CONFIDENCE
        else ""
    )
    body = (
        f'<div class="gauge{thin}">{_gauge_svg(score.score, score.category, score.confidence)}'
        f'<div><div class="val">{score.score}'
        f'<span style="color:var(--ink-3);font-size:var(--t-lg)"> / 100</span></div>'
        f'<div class="cat">{e(category)}</div></div>'
        f'<div class="conf"><span class="lbl">Уверенность данных</span>'
        f'<div class="meter"><i style="width:{confidence}%"></i></div>'
        f'<div class="num" style="font-size:var(--t-2xs)">{confidence}%</div></div></div>'
        f"{caveat}"
        f'<div class="factors" style="margin-top:15px">{factors}</div>'
    )
    return section("score", "Recovery Score", body)


# Дуга в 240° — форма, которую глаз читает как шкалу, а не как долю от целого.
_GAUGE_SWEEP = 240
_GAUGE_RADIUS = 38


def _gauge_svg(score: int, category: str, confidence: float) -> str:
    """Шкала оценки — и непокрытая данными часть на ней же.

    Рисуется вручную: дуга фона, дуга значения и внешняя дуга покрытия, на
    которой видно, какой доли данных у оценки не было. Библиотека графиков ради
    одного элемента не нужна, а SVG масштабируется и печатается.
    """
    tone = {
        ScoreCategory.HIGH.value: "var(--good)",
        ScoreCategory.MEDIUM.value: "var(--warn)",
        ScoreCategory.LOW.value: "var(--crit)",
    }.get(category, "var(--accent)")
    covered = max(0.0, min(1.0, confidence))
    inner = _arc(_GAUGE_RADIUS)
    outer = _arc(_GAUGE_RADIUS + 9)
    filled = inner.length * max(0, min(100, score)) / 100
    missing = outer.length * (1 - covered)
    gap = (
        f'<circle cx="52" cy="46" r="{_GAUGE_RADIUS + 9}" stroke="var(--ink-3)" '
        f'stroke-width="3" stroke-dasharray="{missing:.1f} {outer.circumference:.1f}" '
        f'transform="rotate({_GAUGE_SWEEP * covered:.1f} 52 46)">'
        f"<title>данных не хватило на {round((1 - covered) * 100)}%</title></circle>"
        if missing > 2
        else ""
    )
    return (
        '<svg width="120" height="94" viewBox="-8 -8 120 94" role="img" '
        f'aria-label="Оценка {score} из 100, данных хватило на {round(covered * 100)}%">'
        f'<g transform="rotate(150 52 46)" fill="none" stroke-linecap="butt">'
        f'<circle cx="52" cy="46" r="{_GAUGE_RADIUS}" stroke="var(--line-soft)" '
        f'stroke-width="9" stroke-linecap="round" '
        f'stroke-dasharray="{inner.length:.1f} {inner.circumference:.1f}"/>'
        f'<circle cx="52" cy="46" r="{_GAUGE_RADIUS}" stroke="{tone}" '
        f'stroke-width="9" stroke-linecap="round" '
        f'stroke-dasharray="{filled:.1f} {inner.circumference:.1f}"/>'
        f'<circle cx="52" cy="46" r="{_GAUGE_RADIUS + 9}" stroke="var(--line-soft)" '
        f'stroke-width="3" stroke-dasharray="{outer.length:.1f} {outer.circumference:.1f}"/>'
        f"{gap}"
        "</g></svg>"
    )


@dataclass(frozen=True, slots=True)
class _Arc:
    circumference: float
    length: float


def _arc(radius: int) -> _Arc:
    circumference = 2 * 3.14159265 * radius
    return _Arc(circumference, circumference * _GAUGE_SWEEP / 360)


def sources_section(report: DebtorReport) -> str:
    """Список источников со счётчиком и с ограничениями оценки.

    Ограничения переехали сюда из блока оценки: они описывают источники, а не
    балл, и не должны исчезать вместе с ним, когда балла нет.
    """
    answered, total, _ = coverage(report)
    rows = [
        f'<div class="srow"><span class="nm">{e(title)}</span>'
        f'<span class="st">{state_tag(state)}</span></div>'
        for title, state in _source_rows(report)
    ]
    score = report.recovery_score
    notes = "".join(f"<li>{e(note)}</li>" for note in score.confidence_notes) if score else ""
    limits = (
        '<p class="note">Ограничения оценки:</p>'
        f'<ul style="margin:4px 0 0;padding-left:18px;color:var(--ink-3);'
        f'font-size:var(--t-2xs)">{notes}</ul>'
        if notes
        else ""
    )
    header = f'<p class="note" style="margin:0 0 10px">Ответили {answered} из {total}.</p>'
    return section(
        "sources", "Источники", header + f'<div class="sources">{"".join(rows)}</div>{limits}'
    )


# ---------------------------------------------------------------- страницы


def build_blocks(report: DebtorReport) -> list[Block]:
    """Разделы отчёта в порядке чтения.

    «Источники» стоят до Recovery Score: они объясняют, чему верить, и должны
    идти до балла, а не после. Пустые секции отсеиваются здесь же, поэтому
    оглавление физически не может сослаться на несуществующий раздел.
    """
    candidates = [
        Block("internal", "Наши данные", internal_section(report)),
        Block("fssp", "ФССП", enforcement_section(report)),
        Block("bankruptcy", "Банкротство", bankruptcy_section(report)),
        Block("pledge", "Залоги", pledge_section(report)),
        Block("inheritance", "Наследственные дела", inheritance_section(report)),
        Block("court", "Суды", court_section(report)),
        Block("business", "Бизнес", business_section(report)),
        Block("sources", "Источники", sources_section(report)),
        Block("score", "Recovery Score", score_section(report)),
    ]
    return [block for block in candidates if block.html]


def render_report_page(
    report: DebtorReport,
    decision: VerdictDecision,
    *,
    app_name: str,
    generated_at: datetime | None = None,
    demo_mode: bool = False,
    exports: ExportLinks | None = None,
    print_mode: bool = False,
) -> str:
    """Страница отчёта по одному должнику.

    ``print_mode`` — та же страница, открытая на печать: браузер сам вызывает
    диалог печати, и на бумагу уходит ровно то, что человек видел.
    """
    when = generated_at or report.generated_at
    blocks = build_blocks(report)

    parts: list[str] = []
    if demo_mode:
        parts.append(demo_banner())
    if print_mode:
        parts.append(f'<p class="running copyhint">{e(PRINT_HINT)}</p>')
    parts.append(hero(report, decision, exports=None if print_mode else exports))
    parts.extend(block.html for block in blocks)
    parts.append(f"<footer>{e(DISCLAIMER)}</footer>")

    nav = navigation(
        app_name,
        [(block.anchor, block.title) for block in blocks],
        (f"Отчёт от {format_datetime(when)}",),
    )
    return document(
        title=f"Отчёт по должнику — {app_name}",
        nav=nav,
        body="".join(parts),
        foot=print_footer(app_name, when),
        auto_print=print_mode,
    )


def print_footer(app_name: str, when: datetime) -> str:
    """Колонтитул печатной копии.

    Браузер повторяет фиксированный блок на каждом листе. Адрес страницы сюда
    намеренно не попадает: в нём токен доступа, а лист уходит в дело.
    """
    return (
        f'<div class="printfoot"><span>{e(app_name)}</span>'
        f"<span>Сформировано {e(format_datetime(when))}</span></div>"
    )


def render_message_page(title: str, message: str, *, app_name: str) -> str:
    """Страница для случая, когда показывать нечего: ссылка истекла или неверна.

    Без оглавления и без пустых блоков: единственное, что тут можно сделать, —
    вернуться в бота и запросить отчёт заново, о чём и сказано текстом.
    """
    body = f'<header class="card"><h1>{e(title)}</h1><p>{e(message)}</p></header>'
    return document(title=f"{title} — {app_name}", nav=navigation(app_name, ()), body=body)


# ---------------------------------------------------------------- вспомогательное


def _export_actions(exports: ExportLinks | None) -> str:
    """Кнопки выгрузки. Живут в герое: печатают отчёт чаще, чем читают его до конца."""
    if exports is None:
        return ""
    return (
        '<div class="actions">'
        f'<a href="{e(exports.print_url)}">Распечатать или сохранить в PDF</a>'
        f'<a href="{e(exports.text_url)}" download>Скачать текстом</a>'
        "</div>"
    )


def _subject_line(report: DebtorReport) -> str:
    subject = report.subject
    parts: list[str] = []
    if subject.birth_date:
        parts.append(f"р. {format_date(subject.birth_date)}")
    if subject.inn:
        parts.append(f"ИНН {subject.inn}")
    record = report.internal_record
    if record and record.contract_number:
        parts.append(f"договор {record.contract_number}")
    return " · ".join(parts) if parts else "идентификаторов кроме ФИО нет"


def _facts_grid(rows: Iterable[tuple[str, str, str, str]]) -> str:
    cells = "".join(
        f'<div class="fact"><div class="lbl">{e(label)}</div>'
        f'<div class="v {e(tone)}">{e(value)}</div>'
        f'<div class="src">{e(note)}</div></div>'
        for label, value, tone, note in rows
    )
    return f'<div class="facts">{cells}</div>'


def _unchecked(result: ProviderResult | None, bridge: ProviderResult | None = None) -> str:
    """Разметка для источника, который не ответил.

    Отдельная ветка, а не пустой список: «не проверено» и «ничего не найдено» —
    разные утверждения, и подменять одно другим здесь нельзя. Текст берётся из
    :func:`app.services.reporting.unanswered_line`, чтобы веб и чат описывали
    состояние источника одними и теми же словами.

    ``bridge`` передают три раздела, которые ищут только по ИНН: страница
    обязана объяснять «нужен ИНН физлица» тем же уточнением, что и текст бота,
    иначе один и тот же должник получит два разных объяснения — а лист со
    страницы уходит в дело.
    """
    line = unanswered_line(result, bridge=bridge)
    return f'<p class="empty unchecked">{e(line)}</p>' if line else ""


def _empty_body(
    result: ProviderResult | None,
    empty_line: str,
    *,
    found: int,
    noun: str,
    scope: str = "",
) -> str:
    """Пустой раздел с точной причиной пустоты.

    Слова берутся из :func:`app.services.reporting.empty_reason` — той же
    функции, что печатает их в чат. Голое «не найдено» здесь имело бы право
    стоять только в одном случае из трёх, а печаталось во всех: запись,
    отсеянная по отождествлению, исчезала со страницы под подписью «не
    найдено», и оценка ещё начисляла за это плюс.
    """
    lines = empty_reason(result, found=found, noun=noun, empty_line=empty_line)
    body = "".join(f'<p class="empty">{e(line)}</p>' for line in lines)
    return body + _checked_note(result, scope=scope, notes=False)


def _checked_note(result: ProviderResult | None, *, scope: str = "", notes: bool = True) -> str:
    """Подвал раздела: что источник сказал о полноте ответа, оговорка охвата,
    отметка о проверке.

    ``result.notes`` печатаются здесь, а не в каждой секции по отдельности, и
    печатаются всегда — ровно как в текстовом отчёте (``_source_notes``).
    Страница, промолчавшая о том, что ФССП прислала 100 производств из 105,
    подписывает «производств не найдено» под ответом, который сам сообщил
    обратное; а именно этот лист уходит в дело.

    ``scope`` — оговорка о границах самого источника. Идёт после оговорок
    ответа и перед отметкой о проверке: тот же порядок, что в чате.

    ``notes=False`` ставят пустые разделы: там оговорки источника уже напечатаны
    как САМА причина пустоты (:func:`empty_reason`), и второй раз они бы только
    задвоились.
    """
    if result is None:
        return scope
    head = "".join(f'<p class="note">{e(note)}</p>' for note in result.notes) if notes else ""
    checked = f'<p class="note">Проверено: {e(format_datetime(result.fetched_at))}.</p>'
    return head + scope + checked


__all__ = [
    "Block",
    "ExportLinks",
    "build_blocks",
    "cell",
    "coverage",
    "document",
    "e",
    "navigation",
    "print_footer",
    "raw_cell",
    "render_message_page",
    "render_report_page",
    "section",
    "state_tag",
    "table",
]
