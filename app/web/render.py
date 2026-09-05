"""Отрисовка веб-отчёта.

Отчёт по ссылке, а не простынёй в чат: в сообщении Telegram нет ни таблиц, ни
навигации, а смотреть надо на десятки строк производств сразу, сравнивать
суммы и копировать номера дел.

Правила разметки те же, что и у текстового отчёта, и это не совпадение —
инвариант проекта живёт в данных, а не в оформлении:

*   источник, который не ответил, показывается как «не проверено», и никогда
    как «ничего не найдено»;
*   у каждой записи видно, насколько уверенно она сопоставлена с должником;
*   вердикт всегда идёт вместе с причиной.

Шаблонизатора нет намеренно: страница одна, зависимость лишняя, а каждая
секция — обычная функция, которую можно проверить тестом по отдельности.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime
from html import escape

from app.domain.enums import (
    BUSINESS_ROLE_TITLES,
    MATCH_LEVEL_TITLES,
    PROVIDER_TITLES,
    SCORE_CATEGORY_TITLES,
    MatchLevel,
    ProviderName,
    ProviderStatus,
    ScoreCategory,
)
from app.domain.models import (
    DebtorReport,
    InternalDebtorRecord,
    ProviderResult,
)
from app.domain.verdict import FEE_BASIS_TITLES, VERDICT_TITLES, VerdictDecision
from app.utils.dates import format_date, format_datetime
from app.utils.masking import mask_phone, mask_vin
from app.utils.money import format_amount
from app.web.style import CSS

DISCLAIMER = (
    "Оценка аналитическая и не заменяет юридическую проверку. "
    "Ссылка временная и открывается без пароля — не пересылайте её посторонним."
)
FONTS = (
    "https://fonts.googleapis.com/css2?"
    "family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500;600;700&display=swap"
)


def e(value: object) -> str:
    """Экранирование. Всё, что приходит из данных, проходит через него."""
    return escape("" if value is None else str(value), quote=True)


# ---------------------------------------------------------------- каркас


def document(*, title: str, nav: str, body: str) -> str:
    return (
        "<!doctype html>\n"
        '<html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        # Страница с персональными данными не должна попадать в поисковики.
        '<meta name="robots" content="noindex,nofollow,noarchive">'
        '<meta name="referrer" content="no-referrer">'
        f"<title>{e(title)}</title>"
        f'<link rel="preconnect" href="https://fonts.googleapis.com">'
        f'<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
        f'<link rel="stylesheet" href="{FONTS}">'
        f"<style>{CSS}</style></head>"
        f'<body><div class="shell">{nav}<main>{body}</main></div>{_SCRIPT}</body></html>'
    )


_SCRIPT = """<script>
document.addEventListener('click', function (event) {
  var el = event.target.closest('.copy');
  if (!el) return;
  var text = el.dataset.copy || el.textContent.trim();
  navigator.clipboard && navigator.clipboard.writeText(text).then(function () {
    var was = el.textContent;
    el.textContent = 'скопировано';
    setTimeout(function () { el.textContent = was; }, 900);
  });
});
</script>"""


def navigation(brand: str, items: Sequence[tuple[str, str]], meta: Sequence[str] = ()) -> str:
    links = "".join(
        f'<li><a href="#{e(anchor)}"><span class="n">{index:02d}</span>{e(title)}</a></li>'
        for index, (anchor, title) in enumerate(items, start=1)
    )
    note = "".join(f"<div>{e(line)}</div>" for line in meta)
    return (
        f'<nav><div class="brand">{e(brand)}</div><ol>{links}</ol>'
        f'<div class="meta">{note}</div></nav>'
    )


def section(anchor: str, title: str, body: str, *, number: int | None = None) -> str:
    mark = f' data-n="{number:02d}"' if number is not None else ""
    return f'<section class="card" id="{e(anchor)}"><h2{mark}>{e(title)}</h2>{body}</section>'


def table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    head = "".join(f"<th>{e(header)}</th>" for header in headers)
    body = "".join("<tr>" + "".join(cells) + "</tr>" for cells in rows)
    return (
        f'<div class="scroll"><table><thead><tr>{head}</tr></thead>'
        f"<tbody>{body}</tbody></table></div>"
    )


def cell(value: object, *, numeric: bool = False, right: bool = False, copy: bool = False) -> str:
    classes = " ".join(filter(None, ["n" if numeric else "", "r" if right else ""]))
    attr = f' class="{classes}"' if classes else ""
    text = e(value if value not in (None, "") else "—")
    inner = f'<span class="copy">{text}</span>' if copy and value else text
    return f"<td{attr}>{inner}</td>"


def match_tag(level: MatchLevel) -> str:
    tone = {
        MatchLevel.CONFIRMED: "good",
        MatchLevel.PROBABLE: "warn",
        MatchLevel.WEAK: "mute",
    }[level]
    return f'<span class="tag {tone}">{e(MATCH_LEVEL_TITLES[level])}</span>'


# ---------------------------------------------------------------- секции


def hero(report: DebtorReport, decision: VerdictDecision) -> str:
    """Ответ на главный вопрос — крупно и первым.

    Страницу открывают не «посмотреть данные», а решить, нести ли этого
    должника в суд. Поэтому вердикт, долг и пошлина стоят до всех таблиц.
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
    reason_list = (
        f'<ul style="margin:10px 0 0;padding-left:18px;color:var(--hero-dim);font-size:13.5px">'
        f"{reasons}</ul>"
        if reasons
        else ""
    )
    cached = (
        f'<p class="sub" style="margin-top:9px">Данные проверки от '
        f"{e(format_datetime(report.cached_at))}</p>"
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
        f'<div class="nums">{"".join(numbers)}</div></header>'
    )


def internal_section(report: DebtorReport) -> str:
    record = report.internal_record
    if record is None:
        return section(
            "internal",
            "Наши данные",
            '<p class="empty">Совпадений во внутренней базе нет.</p>',
            number=1,
        )
    facts = _internal_facts(record)
    extra = len(report.internal_records) - 1
    note = (
        f'<p class="note">Ещё {extra} похожих записей во внутренней базе.</p>' if extra > 0 else ""
    )
    return section("internal", "Наши данные", _facts_grid(facts) + note, number=1)


def _internal_facts(record: InternalDebtorRecord) -> list[tuple[str, str, str, str]]:
    phone = record.phone_masked or mask_phone(record.phone)
    rows: list[tuple[str, str, str, str]] = [
        ("ФИО", record.full_name or "—", "", ""),
        ("Дата рождения", format_date(record.birth_date), "", ""),
        ("Телефон", phone or "—", "", "маскирован"),
        ("Договор", record.contract_number or "—", "", ""),
        ("Задолженность", format_amount(record.debt_amount), "", ""),
    ]
    if record.address:
        rows.append(("Адрес", record.address, "", ""))
    if record.vehicle_plate:
        rows.append(("Госномер", record.vehicle_plate, "", ""))
    if record.vin:
        rows.append(("VIN", mask_vin(record.vin) or "—", "", "маскирован"))
    return rows


def enforcement_section(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.FSSP)
    unchecked = _unchecked(result)
    if unchecked:
        return section("fssp", "ФССП", unchecked, number=2)

    active = report.active_proceedings
    if not active:
        return section(
            "fssp",
            "ФССП",
            '<p class="empty">Активных исполнительных производств не найдено.</p>'
            + _checked_note(result),
            number=2,
        )

    rows = [
        (
            cell(item.proceeding_number, numeric=True, copy=True),
            cell(format_amount(item.amount), numeric=True, right=True),
            cell(item.subject),
            cell(item.department),
            f"<td>{match_tag(item.match_level)}</td>",
        )
        for item in active
    ]
    total = report.total_enforcement_amount
    summary = (
        f'<p class="note">Активных производств: {len(active)}. '
        f"Подтверждённая сумма: {e(format_amount(total))}.</p>"
    )
    return section(
        "fssp",
        "ФССП",
        table(("Производство", "Сумма", "Предмет", "Отдел", "Совпадение"), rows)
        + summary
        + _checked_note(result),
        number=2,
    )


def bankruptcy_section(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.FEDRESURS)
    unchecked = _unchecked(result)
    if unchecked:
        return section("bankruptcy", "Банкротство", unchecked, number=3)

    usable = [item for item in report.bankruptcies if item.is_usable]
    if not usable:
        return section(
            "bankruptcy",
            "Банкротство",
            '<p class="empty">Не обнаружено.</p>' + _checked_note(result),
            number=3,
        )

    rows = [
        (
            cell(item.case_number, numeric=True, copy=True),
            cell(item.procedure),
            f'<td><span class="tag {"crit" if item.is_active else "mute"}">'
            f"{'активно' if item.is_active else 'завершено'}</span></td>",
            cell(format_date(item.started_at), numeric=True),
            f"<td>{match_tag(item.match_level)}</td>",
        )
        for item in usable
    ]
    return section(
        "bankruptcy",
        "Банкротство",
        table(("Дело", "Процедура", "Статус", "Начало", "Совпадение"), rows)
        + _checked_note(result),
        number=3,
    )


def business_section(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.FNS)
    unchecked = _unchecked(result)
    if unchecked:
        return section("business", "Бизнес", unchecked, number=4)

    usable = [item for item in report.business_relations if item.is_usable]
    if not usable:
        return section(
            "business",
            "Бизнес",
            '<p class="empty">Связей с ИП и юрлицами не найдено.</p>' + _checked_note(result),
            number=4,
        )

    rows = [
        (
            cell(BUSINESS_ROLE_TITLES.get(item.role, "связь")),
            cell(item.name),
            cell(item.inn, numeric=True, copy=True),
            f'<td><span class="tag {"good" if item.is_active else "mute"}">'
            f"{'действует' if item.is_active else 'прекращено'}</span></td>",
            f"<td>{match_tag(item.match_level)}</td>",
        )
        for item in usable
    ]
    return section(
        "business",
        "Бизнес",
        table(("Роль", "Наименование", "ИНН", "Статус", "Совпадение"), rows)
        + _checked_note(result),
        number=4,
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
    notes = "".join(f"<li>{e(note)}</li>" for note in score.confidence_notes)
    limits = (
        '<p class="note">Ограничения оценки:</p>'
        f'<ul style="margin:4px 0 0;padding-left:18px;color:var(--ink-3);font-size:12.5px">'
        f"{notes}</ul>"
        if notes
        else ""
    )
    confidence = round(score.confidence * 100)
    body = (
        f'<div class="gauge">{_gauge_svg(score.score, score.category)}'
        f'<div><div class="val">{score.score}'
        f'<span style="color:var(--ink-3);font-size:17px"> / 100</span></div>'
        f'<div class="cat">{e(category)}</div></div>'
        f'<div class="conf"><span class="lbl">Уверенность данных</span>'
        f'<div class="meter"><i style="width:{confidence}%"></i></div>'
        f'<div class="num" style="font-size:12.5px">{confidence}%</div></div></div>'
        f'<div class="factors" style="margin-top:15px">{factors}</div>{limits}'
    )
    return section("score", "Recovery Score", body, number=5)


# Дуга в 240° — форма, которую глаз читает как шкалу, а не как долю от целого.
_GAUGE_SWEEP = 240
_GAUGE_RADIUS = 38


def _gauge_svg(score: int, category: str) -> str:
    """Полукруглая шкала оценки.

    Рисуется вручную: одна дуга фона, одна дуга значения. Библиотека графиков
    ради одного элемента не нужна, а SVG масштабируется и печатается.
    """
    tone = {
        ScoreCategory.HIGH.value: "var(--good)",
        ScoreCategory.MEDIUM.value: "var(--warn)",
        ScoreCategory.LOW.value: "var(--crit)",
    }.get(category, "var(--accent)")
    circumference = 2 * 3.14159265 * _GAUGE_RADIUS
    arc = circumference * _GAUGE_SWEEP / 360
    filled = arc * max(0, min(100, score)) / 100
    return (
        '<svg width="104" height="82" viewBox="0 0 104 82" role="img" '
        f'aria-label="Оценка {score} из 100">'
        f'<g transform="rotate(150 52 46)" fill="none" stroke-linecap="round">'
        f'<circle cx="52" cy="46" r="{_GAUGE_RADIUS}" stroke="var(--line-soft)" '
        f'stroke-width="9" stroke-dasharray="{arc:.1f} {circumference:.1f}"/>'
        f'<circle cx="52" cy="46" r="{_GAUGE_RADIUS}" stroke="{tone}" '
        f'stroke-width="9" stroke-dasharray="{filled:.1f} {circumference:.1f}"/>'
        "</g></svg>"
    )


def sources_section(report: DebtorReport) -> str:
    rows = []
    internal_state = (
        '<span class="tag good">есть совпадение</span>'
        if report.internal_records
        else '<span class="tag mute">совпадений нет</span>'
    )
    rows.append(
        f'<div class="srow"><span class="nm">{e(PROVIDER_TITLES[ProviderName.INTERNAL])}</span>'
        f'<span class="st">{internal_state}</span></div>'
    )
    for result in report.provider_results:
        title = PROVIDER_TITLES.get(result.provider, result.provider.value)
        rows.append(
            f'<div class="srow"><span class="nm">{e(title)}</span>'
            f'<span class="st">{_source_state(result)}</span></div>'
        )
    return section("sources", "Источники", f'<div class="sources">{"".join(rows)}</div>', number=6)


# ---------------------------------------------------------------- страницы


def render_report_page(
    report: DebtorReport,
    decision: VerdictDecision,
    *,
    app_name: str,
    generated_at: datetime | None = None,
) -> str:
    """Страница отчёта по одному должнику."""
    subject = report.subject
    when = generated_at or report.generated_at

    blocks = [
        hero(report, decision),
        internal_section(report),
        enforcement_section(report),
        bankruptcy_section(report),
        business_section(report),
        score_section(report),
        sources_section(report),
        f"<footer>{e(DISCLAIMER)}</footer>",
    ]

    nav = navigation(
        app_name,
        (
            ("internal", "Наши данные"),
            ("fssp", "ФССП"),
            ("bankruptcy", "Банкротство"),
            ("business", "Бизнес"),
            ("score", "Recovery Score"),
            ("sources", "Источники"),
        ),
        (f"Отчёт от {format_datetime(when)}",),
    )
    return document(title=f"{subject.display_name} — отчёт", nav=nav, body="".join(blocks))


def render_message_page(title: str, message: str, *, app_name: str) -> str:
    """Страница для случая, когда показывать нечего: ссылка истекла или неверна."""
    nav = navigation(app_name, ())
    body = (
        f'<header class="card"><h1>{e(title)}</h1>'
        f'<p class="sub" style="font-family:var(--sans);font-size:14px">{e(message)}</p></header>'
    )
    return document(title=title, nav=nav, body=body)


# ---------------------------------------------------------------- вспомогательное


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


def _unchecked(result: ProviderResult | None) -> str:
    """Разметка для источника, который не ответил.

    Отдельная ветка, а не пустой список: «не проверено» и «ничего не найдено» —
    разные утверждения, и подменять одно другим здесь нельзя.
    """
    if result is None:
        return '<p class="empty unchecked">Источник не опрашивался.</p>'
    if result.is_answered:
        return ""
    if result.status is ProviderStatus.NOT_CONFIGURED:
        return '<p class="empty unchecked">Не проверено: источник не подключён.</p>'
    if result.error_code == "insufficient_query":
        detail = result.error_message or "недостаточно данных"
        return f'<p class="empty unchecked">Не проверено: {e(detail)}.</p>'
    if result.status is ProviderStatus.UNAVAILABLE:
        return '<p class="empty unchecked">Не проверено: источник временно недоступен.</p>'
    code = result.error_code or "unknown"
    return f'<p class="empty unchecked">Не проверено: ошибка обращения ({e(code)}).</p>'


def _checked_note(result: ProviderResult | None) -> str:
    if result is None:
        return ""
    return f'<p class="note">Проверено: {e(format_datetime(result.fetched_at))}.</p>'


def _source_state(result: ProviderResult) -> str:
    match result.status:
        case ProviderStatus.SUCCESS:
            return f'<span class="tag good">{len(result.records)} зап.</span>'
        case ProviderStatus.NO_RESULTS:
            return '<span class="tag good">проверено, записей нет</span>'
        case ProviderStatus.NOT_CONFIGURED:
            return '<span class="tag mute">не подключено</span>'
        case ProviderStatus.UNAVAILABLE:
            return f'<span class="tag crit">недоступно ({e(result.error_code or "ошибка")})</span>'
        case _:
            if result.error_code == "insufficient_query":
                return '<span class="tag warn">недостаточно данных</span>'
            return f'<span class="tag crit">ошибка ({e(result.error_code or "unknown")})</span>'


__all__ = [
    "cell",
    "document",
    "e",
    "navigation",
    "render_message_page",
    "render_report_page",
    "section",
    "table",
]
