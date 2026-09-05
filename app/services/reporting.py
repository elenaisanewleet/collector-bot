"""Rendering a report for Telegram.

Two rules shape everything here:

*   Never state an absence we did not verify. A source that was not consulted is
    printed as "не подключено", never as "не обнаружено".
*   Never issue a legal instruction. The tool reports a prospect and the facts
    behind it; the decision to litigate belongs to a lawyer.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.domain.enums import (
    BUSINESS_ROLE_TITLES,
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
DISCLAIMER = "Оценка является аналитической и не заменяет юридическую проверку."
DEMO_BANNER = "⚠️ ДЕМО-РЕЖИМ: данные вымышленные, внешние источники не опрашивались."


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
    unanswered = _unanswered_line(result)
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
    unanswered = _unanswered_line(result, bridge=report.result_for(ProviderName.INN_BRIDGE))
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
    state = "активно" if item.is_active else "завершено"
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
    unanswered = _unanswered_line(result, bridge=report.result_for(ProviderName.INN_BRIDGE))
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
    state = "действует" if item.is_active else "прекращено"
    name = item.name or item.inn or "—"
    return f"• {role}: {name} — {state} ({_match_note(item.match_level)})"


def _pledge_block(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.PLEDGE)
    header = "ЗАЛОГИ"
    unanswered = _unanswered_line(result)
    if unanswered:
        return f"{header}\n{unanswered}"

    usable = [item for item in report.pledges if item.is_usable]
    if not usable:
        return f"{header}\nЗаписей в реестре залогов не найдено.\n{_checked_at(result)}"

    lines = [header]
    for item in usable[:MAX_LISTED_PLEDGES]:
        lines.extend(_pledge_lines(item))
    hidden = len(usable) - MAX_LISTED_PLEDGES
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
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
    unanswered = _unanswered_line(result, bridge=report.result_for(ProviderName.INN_BRIDGE))
    if unanswered:
        return f"{header}\n{unanswered}"

    usable = [item for item in report.court_cases if item.is_usable]
    if not usable:
        return (
            f"{header}\nАрбитражных дел не найдено. "
            f"Суды общей юрисдикции этот источник не покрывает.\n{_checked_at(result)}"
        )

    lines = [header]
    for item in usable[:MAX_LISTED_CASES]:
        lines.extend(_court_lines(item))
    hidden = len(usable) - MAX_LISTED_CASES
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    lines.append("Суды общей юрисдикции этот источник не покрывает.")
    lines.append(_checked_at(result))
    return "\n".join(lines)


def _court_lines(item: CourtCase) -> list[str]:
    role = COURT_CASE_ROLE_TITLES.get(item.role, "участник")
    state = "идёт" if item.is_active else "завершено"
    lines = [f"• {item.case_number} — {role}, {state}"]
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
        lines.append("Факторов для оценки недостаточно — источники не дали данных.")
    if score.confidence_notes:
        lines.append("")
        lines.append("Ограничения оценки:")
        lines.extend(f"— {note}" for note in score.confidence_notes)

    lines.append("")
    lines.append(f"Предварительная перспектива взыскания: {category}.")
    return "\n".join(lines)


def _sources_block(report: DebtorReport) -> str:
    lines = ["ИСТОЧНИКИ"]
    if report.internal_records:
        lines.append("✓ Наши данные")
    else:
        lines.append("○ Наши данные — совпадений нет")
    for result in report.provider_results:
        lines.append(_source_line(result))
    return "\n".join(lines)


def _source_line(result: ProviderResult) -> str:
    title = PROVIDER_TITLES.get(result.provider, result.provider.value)
    if result.provider is ProviderName.INN_BRIDGE:
        return _bridge_source_line(result, title)
    match result.status:
        case ProviderStatus.SUCCESS:
            return f"✓ {title} — {len(result.records)} зап."
        case ProviderStatus.NO_RESULTS:
            return f"✓ {title} — проверено, записей нет"
        case ProviderStatus.NOT_CONFIGURED:
            return f"○ {title} — не подключено"
        case ProviderStatus.UNAVAILABLE:
            return f"✗ {title} — недоступно ({result.error_code or 'ошибка'})"
        case _:
            if result.error_code == "insufficient_query":
                return f"○ {title} — недостаточно данных для запроса"
            return f"✗ {title} — ошибка ({result.error_code or 'unknown'})"


def _bridge_source_line(result: ProviderResult, title: str) -> str:
    """Строка моста «паспорт → ИНН» в блоке ИСТОЧНИКИ.

    Своя ветка нужна прежде всего потому, что общая напечатала бы «✓ … — 0 зап.»
    для успешно полученного ИНН: мост записей не приносит, он их делает
    возможными.

    Сам ИНН здесь не печатается ни в каком виде, включая маскированный. Причина
    не приватность — для ИНН есть ``mask_inn`` — а согласованность двух показов:
    восстановленный из кэша ``ProviderResult`` значения не несёт, и «получен
    77********03» на первом показе против «получен» на втором было бы ровно тем
    расхождением, которое чинит обогащение субъекта в ``_load_cached``.
    Оператору нужен исход моста, а не значение.
    """
    match result.status:
        case ProviderStatus.SUCCESS:
            return f"✓ {title} — ИНН получен, банкротство, ИП и арбитраж проверены по нему"
        case ProviderStatus.NO_RESULTS:
            note = f"; {result.error_message}" if result.error_message else ""
            return f"✓ {title} — проверено, ИНН по этим данным не найден{note}"
        case ProviderStatus.NOT_CONFIGURED:
            return f"○ {title} — не подключено"
        case ProviderStatus.UNAVAILABLE:
            return f"✗ {title} — недоступно ({result.error_code or 'ошибка'})"
        case _:
            if result.error_code == "insufficient_query":
                return f"○ {title} — {result.error_message or 'недостаточно данных'}"
            return f"✗ {title} — ошибка ({result.error_code or 'unknown'})"


# ---------------------------------------------------------------- helpers


def _unanswered_line(
    result: ProviderResult | None, *, bridge: ProviderResult | None = None
) -> str | None:
    """The line used when a source did not actually answer.

    This is the guard that keeps "not checked" from reading as "nothing found".

    ``bridge`` уточняет ровно один случай — «нужен ИНН физлица» у трёх
    источников, которые ищут только по нему. Без уточнения одна и та же строка
    означала бы четыре разных вещи: паспорта не дали, мост выключен, **ФНС
    ответила и ИНН нет**, **мост не отработал**. Последние две — это ``NO_RESULTS``
    против ``UNAVAILABLE``, тот самый инвариант в миниатюре, ради которого мост и
    строился; потерять его здесь значило бы заплатить за различение и выбросить
    его.
    """
    if result is None:
        return "Источник не опрашивался."
    if result.is_answered:
        return None
    if result.status is ProviderStatus.NOT_CONFIGURED:
        return "Не проверено: источник не подключён."
    if result.error_code == "insufficient_query":
        reason = result.error_message or "недостаточно данных"
        return f"Не проверено: {reason}.{_bridge_note(bridge)}"
    if result.status is ProviderStatus.UNAVAILABLE:
        return "Не проверено: источник временно недоступен."
    return f"Не проверено: ошибка обращения к источнику ({result.error_code or 'unknown'})."


def _bridge_note(bridge: ProviderResult | None) -> str:
    """Почему ИНН, которого не хватило источнику, не был получен по паспорту."""
    if bridge is None:
        return ""
    match bridge.status:
        case ProviderStatus.SUCCESS:
            # Источнику хватило бы ИНН — такого сочетания быть не должно.
            return ""
        case ProviderStatus.NO_RESULTS:
            return " ФНС не нашла ИНН по паспорту."
        case ProviderStatus.NOT_CONFIGURED:
            return " Получение ИНН по паспорту не подключено."
        case _:
            if bridge.error_code == "insufficient_query":
                return f" {bridge.error_message}." if bridge.error_message else ""
            return (
                f" Получить ИНН по паспорту не удалось ({bridge.error_code or 'unknown'}) — "
                "это не значит, что записей нет."
            )


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
