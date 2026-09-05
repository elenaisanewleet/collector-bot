"""Rendering a report for Telegram.

Two rules shape everything here:

*   Never state an absence we did not verify. A source that was not consulted is
    printed as "не подключено", never as "не обнаружено".
*   Never issue a legal instruction. The tool reports a prospect and the facts
    behind it; the decision to litigate belongs to a lawyer.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from app.domain.enums import (
    BANKRUPTCY_STATUS_TITLES,
    BUSINESS_ROLE_TITLES,
    BUSINESS_STATUS_TITLES,
    COURT_CASE_ROLE_TITLES,
    MATCH_LEVEL_TITLES,
    PLEDGE_STATUS_TITLES,
    PROVIDER_TITLES,
    SCORE_CATEGORY_TITLES,
    MatchLevel,
    ProviderName,
    ProviderStatus,
    ScoreCategory,
)
from app.domain.models import (
    BankruptcyRecord,
    BusinessRelation,
    CourtCase,
    DebtorReport,
    EnforcementProceeding,
    InternalDebtorRecord,
    PledgeRecord,
    ProviderResult,
    RecoveryScore,
)
from app.utils.dates import format_date, format_datetime
from app.utils.formatting import percent, pluralize_ru, signed, truncate
from app.utils.masking import mask_phone, mask_vin
from app.utils.money import format_amount

MAX_LISTED_PROCEEDINGS = 5
MAX_LISTED_BUSINESSES = 5
MAX_LISTED_PLEDGES = 5
MAX_LISTED_CASES = 5
# Что именно закрывает источник залогов. Ответ pledge_* несёт две ветки — ФНП и
# Федресурс, — а карта полей описывает один набор строк и читает только первую.
# Без этой оговорки «залогов не найдено» прочиталось бы как «имущество не
# обременено», то есть шире проверенного.
PLEDGE_SCOPE_NOTE = (
    "Проверен только реестр уведомлений ФНП: лизинг и иные обременения "
    "Федресурса, а также ипотека в Росреестре сюда не входят."
)
# Что этот источник не покрывает. Печатается и когда дела найдены, и когда их
# нет: «арбитражных дел не найдено» без оговорки читается как «на него никто не
# подавал», а иски к физлицу идут в суд общей юрисдикции.
COURT_SCOPE_NOTE = "Суды общей юрисдикции этот источник не покрывает."
DISCLAIMER = "Оценка является аналитической и не заменяет юридическую проверку."
DEMO_BANNER = "⚠️ ДЕМО-РЕЖИМ: данные вымышленные, внешние источники не опрашивались."
NO_FACTORS_NOTE = "Факторов для оценки недостаточно — источники не дали данных."


# ---------------------------------------------------------------- состояния источника


class SourceStateCode(StrEnum):
    """Различимых состояний источника ровно столько, сколько здесь.

    Ответ источника и его отсутствие — разные утверждения, и ни одно из
    состояний ниже не сводится к другому. Всё, что показывает эти состояния —
    текстовый отчёт, веб-страница, карточка в чате, ограничения оценки, — берёт
    подписи отсюда: второй набор формулировок неизбежно разъедется с первым, и
    тогда «не проверено» где-нибудь да прочитается как «чисто».
    """

    FOUND = "found"  # ответил и нашёл
    EMPTY = "empty"  # ответил и не нашёл
    NOT_CONFIGURED = "not_configured"  # не подключён
    INSUFFICIENT = "insufficient"  # не хватило данных для запроса
    UNAVAILABLE = "unavailable"  # временно недоступен
    ERROR = "error"  # ошибка обращения
    NOT_QUERIED = "not_queried"  # результата нет вовсе: источник не опрашивался


@dataclass(frozen=True, slots=True)
class SourceState:
    """Одно состояние источника во всех видах, какие нужны отображению."""

    code: SourceStateCode
    # Короткая подпись для списка источников: «проверено, записей нет».
    label: str
    # Знак, различимый без цвета: в чате, в ч/б печати и для дальтоника это
    # единственный носитель смысла, а цвет — только усиление.
    mark: str
    answered: bool

    @property
    def is_unchecked(self) -> bool:
        return not self.answered


def source_state(result: ProviderResult | None, *, records: int | None = None) -> SourceState:
    """Состояние источника по его результату.

    ``records`` — сколько записей показать в подписи, когда они живут не в
    ``result.records``: у внутренней базы записи лежат в самом отчёте, а
    результат несёт только состояние.

    Порядок веток важен: ``insufficient_query`` проверяется до ``UNAVAILABLE``,
    иначе «нам нечего было спросить» превратится в «источник лежал».
    """
    if result is None:
        return SourceState(SourceStateCode.NOT_QUERIED, "не опрашивался", "○", False)
    if result.status is ProviderStatus.SUCCESS:
        count = len(result.records) if records is None else records
        return SourceState(SourceStateCode.FOUND, f"{count} зап.", "✓", True)
    if result.status is ProviderStatus.NO_RESULTS:
        return SourceState(SourceStateCode.EMPTY, "проверено, записей нет", "—", True)
    if result.status is ProviderStatus.NOT_CONFIGURED:
        return SourceState(SourceStateCode.NOT_CONFIGURED, "не подключено", "○", False)
    if result.error_code == "insufficient_query":
        return SourceState(SourceStateCode.INSUFFICIENT, "недостаточно данных", "?", False)
    if result.status is ProviderStatus.UNAVAILABLE:
        code = result.error_code or "ошибка"
        return SourceState(SourceStateCode.UNAVAILABLE, f"недоступно ({code})", "!", False)
    code = result.error_code or "unknown"
    return SourceState(SourceStateCode.ERROR, f"ошибка ({code})", "✗", False)


def unanswered_line(result: ProviderResult | None) -> str | None:
    """Строка для источника, который не ответил, или ``None``, если ответил.

    Это тот самый предохранитель, который не даёт «не проверено» прочитаться
    как «ничего не найдено». Разметку вокруг него каждый вывод делает свою,
    а текст — общий.
    """
    state = source_state(result)
    match state.code:
        case SourceStateCode.FOUND | SourceStateCode.EMPTY:
            return None
        case SourceStateCode.NOT_QUERIED:
            return "Источник не опрашивался."
        case SourceStateCode.NOT_CONFIGURED:
            return "Не проверено: источник не подключён."
        case SourceStateCode.INSUFFICIENT:
            detail = (result.error_message if result else None) or "недостаточно данных"
            return f"Не проверено: {detail}."
        case SourceStateCode.UNAVAILABLE:
            return "Не проверено: источник временно недоступен."
        case _:
            code = (result.error_code if result else None) or "unknown"
            return f"Не проверено: ошибка обращения к источнику ({code})."


def unchecked_titles(report: DebtorReport) -> list[str]:
    """Названия источников, которые не ответили.

    Одна функция на чат, страницу и печать: список непроверенного обязан
    совпадать везде, где рядом стоит вердикт.
    """
    return [
        PROVIDER_TITLES.get(result.provider, result.provider.value)
        for result in report.provider_results
        if not result.is_answered
    ]


def answered_count(report: DebtorReport) -> tuple[int, int]:
    """Сколько источников ответило из скольких опрошенных."""
    total = len(report.provider_results)
    answered = sum(1 for result in report.provider_results if result.is_answered)
    return answered, total


def render_report(report: DebtorReport, *, demo_mode: bool = False) -> str:
    """Full report text. The caller splits it into Telegram-sized messages."""
    blocks: list[str] = []
    if demo_mode:
        blocks.append(DEMO_BANNER)
    blocks.append(_header(report))
    if report.from_cache:
        blocks.append(
            "♻️ Использованы кэшированные данные.\n"
            f"Последняя проверка: {format_datetime(report.cached_at)}"
        )
    blocks.append(_internal_block(report))
    blocks.append(_enforcement_block(report))
    blocks.append(_bankruptcy_block(report))
    blocks.append(_business_block(report))
    blocks.append(_pledge_block(report))
    blocks.append(_court_block(report))
    blocks.append(_score_block(report.recovery_score))
    blocks.append(_sources_block(report))
    blocks.append(DISCLAIMER)
    return "\n\n".join(block for block in blocks if block)


# ---------------------------------------------------------------- sections


def _header(report: DebtorReport) -> str:
    subject = report.subject
    lines = [f"👤 {subject.display_name}"]
    if subject.birth_date:
        lines.append(f"Дата рождения: {format_date(subject.birth_date)}")
    if subject.regions:
        lines.append(f"Регион: {_regions_label(subject.regions)}")
    if subject.vehicle and subject.vehicle.has_unique_identifier:
        lines.append(f"Транспорт: {subject.vehicle.title}")
    return "\n".join(lines)


def _regions_label(regions: Iterable[str]) -> str:
    from app.domain.enums import REGION_TITLES, Region

    titles: list[str] = []
    for value in regions:
        try:
            titles.append(REGION_TITLES[Region(value)])
        except (ValueError, KeyError):
            continue
    return " + ".join(titles) if titles else "не указан"


def render_internal_card(record: InternalDebtorRecord) -> str:
    """The standalone "our data" card shown by the contract flow."""
    return "\n".join(["НАШИ ДАННЫЕ", *_internal_lines(record)])


def _internal_block(report: DebtorReport) -> str:
    record = report.internal_record
    if record is None:
        return "НАШИ ДАННЫЕ\nСовпадений во внутренней базе не найдено."

    lines = ["НАШИ ДАННЫЕ"]
    lines.extend(_internal_lines(record))
    extra = len(report.internal_records) - 1
    if extra > 0:
        noun = pluralize_ru(extra, "запись", "записи", "записей")
        lines.append(f"Ещё {extra} похожих {noun} во внутренней базе.")
    return "\n".join(lines)


def _internal_lines(record: InternalDebtorRecord) -> list[str]:
    lines: list[str] = []
    if record.full_name:
        lines.append(f"ФИО: {record.full_name}")
    if record.birth_date:
        lines.append(f"Дата рождения: {format_date(record.birth_date)}")
    phone = record.phone_masked or mask_phone(record.phone)
    if phone:
        lines.append(f"Телефон: {phone}")
    if record.contract_number:
        lines.append(f"Договор: {record.contract_number}")
    if record.claim_number:
        lines.append(f"Заявка: {record.claim_number}")
    if record.debt_amount is not None:
        lines.append(f"Задолженность: {format_amount(record.debt_amount)}")
    if record.address:
        lines.append(f"Адрес: {truncate(record.address, 120)}")
    if record.vehicle_plate:
        lines.append(f"Госномер: {record.vehicle_plate}")
    if record.vin:
        lines.append(f"VIN: {mask_vin(record.vin)}")
    return lines


def _enforcement_block(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.FSSP)
    header = "ФССП"
    unanswered = unanswered_line(result)
    if unanswered:
        return f"{header}\n{unanswered}"

    active = report.active_proceedings
    if not active:
        return f"{header}\nАктивных исполнительных производств не найдено.\n{_checked_at(result)}"

    lines = [header, f"Активных производств: {len(active)}"]
    total = report.total_enforcement_amount
    if total:
        lines.append(f"Подтверждённая сумма: {format_amount(total)}")
    lines.append("")
    for item in active[:MAX_LISTED_PROCEEDINGS]:
        lines.extend(_proceeding_lines(item))
    hidden = len(active) - MAX_LISTED_PROCEEDINGS
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    lines.append(_checked_at(result))
    return "\n".join(lines)


def _proceeding_lines(item: EnforcementProceeding) -> list[str]:
    lines = [f"• {item.proceeding_number}"]
    if item.amount is not None:
        lines.append(f"  {format_amount(item.amount)}")
    if item.subject:
        lines.append(f"  {truncate(item.subject, 90)}")
    lines.append(f"  {_match_note(item.match_level)}")
    return lines


def _bankruptcy_block(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.FEDRESURS)
    header = "БАНКРОТСТВО"
    unanswered = unanswered_line(result)
    if unanswered:
        return f"{header}\n{unanswered}"

    usable = [item for item in report.bankruptcies if item.is_usable]
    if not usable:
        return f"{header}\nНе обнаружено\n{_checked_at(result)}"

    lines = [header]
    for item in usable:
        lines.extend(_bankruptcy_lines(item))
    lines.append(_checked_at(result))
    return "\n".join(lines)


def _bankruptcy_lines(item: BankruptcyRecord) -> list[str]:
    # Через словарь, а не через ``is_active``: у булева флага два значения, а
    # состояний три. Источник отдаёт состояние не всегда — ``bankrot_person``,
    # например, не отдаёт ни процедуры, ни дат, — и непрочитанное состояние,
    # напечатанное как «завершено», сообщает оператору обратное правде.
    state = BANKRUPTCY_STATUS_TITLES.get(item.status, "состояние процедуры не определено")
    lines = [f"• {item.procedure or 'процедура банкротства'} — {state}"]
    if item.case_number:
        lines.append(f"  Дело: {item.case_number}")
    if item.started_at:
        lines.append(f"  Начало: {format_date(item.started_at)}")
    if item.completed_at:
        lines.append(f"  Завершение: {format_date(item.completed_at)}")
    lines.append(f"  {_match_note(item.match_level)}")
    return lines


def _business_block(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.FNS)
    header = "БИЗНЕС"
    unanswered = unanswered_line(result)
    if unanswered:
        return f"{header}\n{unanswered}"

    usable = [item for item in report.business_relations if item.is_usable]
    if not usable:
        return f"{header}\nСвязей с ИП и юрлицами не найдено.\n{_checked_at(result)}"

    lines = [header]
    for item in usable[:MAX_LISTED_BUSINESSES]:
        lines.append(_business_line(item))
    hidden = len(usable) - MAX_LISTED_BUSINESSES
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    lines.append(_checked_at(result))
    return "\n".join(lines)


def _business_line(item: BusinessRelation) -> str:
    role = BUSINESS_ROLE_TITLES.get(item.role, "связь")
    # Через словарь, а не через ``is_active``: состояний три, и непрочитанное,
    # напечатанное как «прекращено», прячет от оператора живое ИП.
    state = BUSINESS_STATUS_TITLES.get(item.status, "состояние не определено")
    name = item.name or item.inn or "—"
    return f"• {role}: {name} — {state} ({_match_note(item.match_level)})"


def _pledge_block(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.PLEDGE)
    header = "ЗАЛОГИ"
    unanswered = unanswered_line(result)
    if unanswered:
        return f"{header}\n{unanswered}"

    usable = [item for item in report.pledges if item.is_usable]
    if not usable:
        return (
            f"{header}\nЗаписей в реестре залогов не найдено. "
            f"{PLEDGE_SCOPE_NOTE}\n{_checked_at(result)}"
        )

    lines = [header]
    for item in usable[:MAX_LISTED_PLEDGES]:
        lines.extend(_pledge_lines(item))
    hidden = len(usable) - MAX_LISTED_PLEDGES
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    lines.append(PLEDGE_SCOPE_NOTE)
    lines.append(_checked_at(result))
    return "\n".join(lines)


def _pledge_lines(item: PledgeRecord) -> list[str]:
    state = PLEDGE_STATUS_TITLES.get(item.status, "состояние записи не определено")
    lines = [f"• {truncate(item.subject or 'предмет залога не указан', 90)} — {state}"]
    if item.pledgee_name:
        lines.append(f"  Залогодержатель: {truncate(item.pledgee_name, 90)}")
    if item.vin:
        lines.append(f"  VIN: {mask_vin(item.vin)}")
    if item.registration_number:
        lines.append(f"  Уведомление: {item.registration_number}")
    if item.registered_at:
        lines.append(f"  Зарегистрирован: {format_date(item.registered_at)}")
    lines.append(f"  {_match_note(item.match_level)}")
    return lines


def _court_block(report: DebtorReport) -> str:
    """Арбитраж — и только он.

    Названо честно в самом тексте: суды общей юрисдикции этот источник не
    покрывает, а «дел не найдено» без такой оговорки прочиталось бы как «на него
    никто не подавал».
    """
    result = report.result_for(ProviderName.COURT)
    header = "СУДЫ (АРБИТРАЖ)"
    unanswered = unanswered_line(result)
    if unanswered:
        return f"{header}\n{unanswered}"

    usable = [item for item in report.court_cases if item.is_usable]
    if not usable:
        return f"{header}\nАрбитражных дел не найдено. {COURT_SCOPE_NOTE}\n{_checked_at(result)}"

    lines = [header]
    for item in usable[:MAX_LISTED_CASES]:
        lines.extend(_court_lines(item))
    hidden = len(usable) - MAX_LISTED_CASES
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    lines.append(COURT_SCOPE_NOTE)
    lines.append(_checked_at(result))
    return "\n".join(lines)


def _court_lines(item: CourtCase) -> list[str]:
    role = COURT_CASE_ROLE_TITLES.get(item.role, "участник")
    state = "идёт" if item.is_active else "завершено"
    lines = [f"• {item.case_number} — {role}, {state}"]
    if item.case_type:
        # Категория дела заполняется картой полей и нормализуется — значит, её
        # надо и показывать. Для взыскания она не декорация: арбитражное дело,
        # классифицированное как банкротство, меняет план действий целиком.
        lines.append(f"  Категория: {truncate(item.case_type, 60)}")
    if item.amount is not None:
        lines.append(f"  {format_amount(item.amount)}")
    if item.court_name:
        lines.append(f"  {truncate(item.court_name, 90)}")
    if item.filed_at:
        lines.append(f"  Подано: {format_date(item.filed_at)}")
    lines.append(f"  {_match_note(item.match_level)}")
    return lines


def _score_block(score: RecoveryScore | None) -> str:
    if score is None:
        return ""
    category = SCORE_CATEGORY_TITLES.get(ScoreCategory(score.category), score.category)
    lines = [
        "RECOVERY SCORE",
        f"{score.score} / 100 — {category}",
        "",
        f"Уверенность данных: {percent(score.confidence)}",
    ]

    positives = [factor for factor in score.factors if factor.delta > 0]
    negatives = [factor for factor in score.factors if factor.delta < 0]

    if positives:
        lines.append("")
        lines.append("Положительные факторы:")
        lines.extend(f"{signed(f.delta)} — {f.reason}" for f in positives)
    if negatives:
        lines.append("")
        lines.append("Риски:")
        lines.extend(f"{signed(f.delta)} — {f.reason}" for f in negatives)
    if not score.factors:
        lines.append("")
        lines.append(NO_FACTORS_NOTE)
    if score.confidence_notes:
        lines.append("")
        lines.append("Ограничения оценки:")
        lines.extend(f"— {note}" for note in score.confidence_notes)

    lines.append("")
    lines.append(f"Предварительная перспектива взыскания: {category}.")
    return "\n".join(lines)


def _sources_block(report: DebtorReport) -> str:
    answered, total = answered_count(report)
    lines = [f"ИСТОЧНИКИ (ответили {answered} из {total})"]
    lines.extend(_source_line(report, result) for result in report.provider_results)
    return "\n".join(lines)


def _source_line(report: DebtorReport, result: ProviderResult) -> str:
    title = PROVIDER_TITLES.get(result.provider, result.provider.value)
    # У внутренней базы записи лежат в самом отчёте, а не в результате: он
    # несёт только состояние источника.
    records = len(report.internal_records) if result.provider is ProviderName.INTERNAL else None
    state = source_state(result, records=records)
    return f"{state.mark} {title} — {state.label}"


# ---------------------------------------------------------------- helpers


def _checked_at(result: ProviderResult | None) -> str:
    if result is None:
        return ""
    return f"Проверено: {format_datetime(result.fetched_at)}"


def _match_note(level: MatchLevel) -> str:
    return MATCH_LEVEL_TITLES.get(level, "")


def render_history_line(
    index: int,
    *,
    created_at: str,
    search_type: str,
    masked_query: str,
    score: int | None,
    category: str | None,
    provider_summary: str,
) -> str:
    score_text = (
        f"{score}/100 — {SCORE_CATEGORY_TITLES.get(ScoreCategory(category), category)}"
        if score is not None and category
        else "оценка недоступна"
    )
    return (
        f"{index}. {created_at} · {search_type}\n"
        f"   {masked_query}\n"
        f"   {score_text}\n"
        f"   {provider_summary}"
    )


def render_provider_summary(results: Iterable[tuple[str, str]]) -> str:
    """Compact per-provider status line for the history list."""
    icons = {
        ProviderStatus.SUCCESS.value: "✓",
        ProviderStatus.NO_RESULTS.value: "✓",
        ProviderStatus.NOT_CONFIGURED.value: "○",
        ProviderStatus.UNAVAILABLE.value: "✗",
        ProviderStatus.ERROR.value: "✗",
    }
    parts: list[str] = []
    for provider, status in results:
        try:
            title = PROVIDER_TITLES[ProviderName(provider)]
        except (ValueError, KeyError):
            title = provider
        parts.append(f"{icons.get(status, '?')}{title}")
    return " ".join(parts) if parts else "—"
