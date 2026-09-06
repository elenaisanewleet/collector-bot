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
    InheritanceCase,
    InternalDebtorRecord,
    PledgeRecord,
    PropertyRecord,
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
MAX_LISTED_PROPERTIES = 3
MAX_LISTED_INHERITANCE = 5
# Границы реестра наследственных дел, обе обязательны и обе печатаются в каждой
# ветке раздела.
#
# Первая — про то, как источник ищет: только по ФИО, всех однофамильцев разом,
# и отбор по дате рождения делаем мы, а не он. Без неё найденная запись
# читается как запись о должнике.
#
# Вторая — про то, чего пустой ответ не значит. Наследственное дело заводится
# по заявлению наследника, поэтому «дел не найдено» не является даже слабым
# доказательством того, что должник жив. Ровно поэтому у источника нет и
# положительного фактора в оценке.
INHERITANCE_SCOPE_NOTE = (
    "Реестр ФНП ищет только по ФИО и возвращает всех однофамильцев; отбор по дате\n"
    "рождения выполнен на нашей стороне, а в самих записях её часто нет.\n"
    "Наследственное дело открывается по заявлению наследника — его отсутствие\n"
    "НЕ означает, что должник жив."
)

# Обязательная строка блока ЕГРН. Источник не называет правообладателя, и любой
# текст рядом с найденным объектом читается как «нашли имущество должника», если
# прямо не сказано обратное.
OWNERSHIP_DISCLAIMER = (
    "ЕГРН не раскрывает правообладателя. Принадлежность объекта должнику\n"
    "НЕ подтверждена: по этому адресу он может быть только зарегистрирован."
)
NO_PROPERTY_FOUND = (
    "По указанному адресу объект в ЕГРН не найден. Это не значит, что у должника "
    "нет недвижимости: по ФИО Росреестр сведения о правах не выдаёт."
)
# Имущество ООО не является имуществом участника: взыскание обращается на долю
# в уставном капитале, а обороты компании — лишь оценка её стоимости.
COMPANY_ASSETS_DISCLAIMER = (
    "  Имущество ООО не является имуществом участника. Взыскание обращается\n"
    "  на долю в уставном капитале (ст. 74 ФЗ-229, ст. 25 ФЗ-14); обороты\n"
    "  компании — лишь оценка стоимости доли."
)
DISCLAIMER = "Оценка является аналитической и не заменяет юридическую проверку."
# Подписи состояния «источник подключён / не подключён» и строка, которой это
# состояние печатается в отчёте. Живут здесь по той же причине, что и
# SourceStateCode ниже: их показывает не только отчёт — те же слова стоят на
# экране «Откуда данные», в справке и в /status, и второй набор формулировок
# разъехался бы с первым за одну правку.
CONNECTED_LABEL = "подключено"
NOT_CONFIGURED_LABEL = "не подключено"
EMPTY_LABEL = "проверено, записей нет"
NOT_CONFIGURED_REPORT_LINE = "Не проверено: источник не подключён."
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
    PARTIAL = "partial"  # ответил, но сам сказал, что прислал не всё
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
    иначе «нам нечего было спросить» превратится в «источник лежал»; а
    ``is_partial`` — до ``SUCCESS``/``NO_RESULTS``, иначе источник, который сам
    сообщил, что прислал не всё, получит подпись «проверено, записей нет».
    """
    if result is None:
        return SourceState(SourceStateCode.NOT_QUERIED, "не опрашивался", "○", False)
    if result.is_partial and result.is_answered:
        count = len(result.records) if records is None else records
        return SourceState(SourceStateCode.PARTIAL, f"ответ неполный, {count} зап.", "⚠", True)
    if result.status is ProviderStatus.SUCCESS:
        count = len(result.records) if records is None else records
        return SourceState(SourceStateCode.FOUND, f"{count} зап.", "✓", True)
    if result.status is ProviderStatus.NO_RESULTS:
        return SourceState(SourceStateCode.EMPTY, EMPTY_LABEL, "—", True)
    if result.status is ProviderStatus.NOT_CONFIGURED:
        return SourceState(SourceStateCode.NOT_CONFIGURED, NOT_CONFIGURED_LABEL, "○", False)
    if result.error_code == "insufficient_query":
        return SourceState(SourceStateCode.INSUFFICIENT, "недостаточно данных", "?", False)
    if result.status is ProviderStatus.UNAVAILABLE:
        code = result.error_code or "ошибка"
        return SourceState(SourceStateCode.UNAVAILABLE, f"недоступно ({code})", "!", False)
    code = result.error_code or "unknown"
    return SourceState(SourceStateCode.ERROR, f"ошибка ({code})", "✗", False)


def unanswered_line(
    result: ProviderResult | None, *, bridge: ProviderResult | None = None
) -> str | None:
    """Строка для источника, который не ответил, или ``None``, если ответил.

    Это тот самый предохранитель, который не даёт «не проверено» прочитаться
    как «ничего не найдено». Разметку вокруг него каждый вывод делает свою,
    а текст — общий: функция здесь одна на чат и на страницу намеренно, второй
    набор формулировок разъедется с первым за одну правку.

    ``bridge`` уточняет ровно один случай — «нужен ИНН физлица» у трёх
    источников, которые ищут только по нему. Без уточнения одна и та же строка
    означала бы четыре разных вещи: паспорта не дали, мост выключен, **ФНС
    ответила и ИНН нет**, **мост не отработал**. Последние две — это
    ``NO_RESULTS`` против ``UNAVAILABLE``, тот самый инвариант в миниатюре, ради
    которого мост и строился; потерять его здесь значило бы заплатить за
    различение и выбросить его.
    """
    state = source_state(result)
    match state.code:
        case SourceStateCode.FOUND | SourceStateCode.EMPTY | SourceStateCode.PARTIAL:
            # Неполный ответ — всё же ответ: о его неполноте говорит сам раздел
            # (``_source_notes``/``_empty_block``), а не строка «не проверено».
            return None
        case SourceStateCode.NOT_QUERIED:
            return "Источник не опрашивался."
        case SourceStateCode.NOT_CONFIGURED:
            return NOT_CONFIGURED_REPORT_LINE
        case SourceStateCode.INSUFFICIENT:
            detail = (result.error_message if result else None) or "недостаточно данных"
            return f"Не проверено: {detail}.{_bridge_note(bridge)}"
        case SourceStateCode.UNAVAILABLE:
            return "Не проверено: источник временно недоступен."
        case _:
            code = (result.error_code if result else None) or "unknown"
            return f"Не проверено: ошибка обращения к источнику ({code})."


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
    blocks.append(_inheritance_block(report))
    blocks.append(_property_block(report))
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
    # Оговорки источника печатаются в обеих ветках, и во второй они важнее:
    # «активных производств не найдено» под ответом, который сам сообщил, что
    # прислал не всё (сотня производств — не весь список), — это подпись под
    # неправдой. Отсев по отождествлению здесь по-прежнему оговоркой не
    # считается: ФССП ищет по ФИО и штатно возвращает однофамильцев.
    notes = _source_notes(result)
    if not active:
        lines = [header, "Активных исполнительных производств не найдено.", *notes]
        lines.append(_checked_at(result))
        return "\n".join(lines)

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
    lines.extend(notes)
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
    unanswered = unanswered_line(result, bridge=report.result_for(ProviderName.INN_BRIDGE))
    if unanswered:
        return f"{header}\n{unanswered}"

    usable = [item for item in report.bankruptcies if item.is_usable]
    if not usable:
        return _empty_block(
            header, result, "Не обнаружено", found=len(report.bankruptcies), noun="запись"
        )

    lines = [header]
    for item in usable:
        lines.extend(_bankruptcy_lines(item))
    lines.extend(_source_notes(result))
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
    unanswered = unanswered_line(result, bridge=report.result_for(ProviderName.INN_BRIDGE))
    if unanswered:
        return f"{header}\n{unanswered}"

    # ИП — это сам должник под другим именем, и слабое совпадение по нему
    # означает чужого человека: такие записи отсеиваются. Юрлицо совпадением по
    # ФИО не проверяется вовсе (название ООО не является именем), поэтому
    # отсеять его было бы не осторожностью, а потерей находки; вместо этого
    # рядом с ним печатается, чем именно связь подтверждена.
    usable = [item for item in report.business_relations if item.is_usable or item.is_legal_entity]
    if not usable:
        return _empty_block(
            header,
            result,
            "Связей с ИП и юрлицами не найдено.",
            found=len(report.business_relations),
            noun="связь",
        )

    lines = [header]
    for item in usable[:MAX_LISTED_BUSINESSES]:
        lines.extend(_business_line(item, report))
    hidden = len(usable) - MAX_LISTED_BUSINESSES
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    lines.extend(_source_notes(result))
    lines.append(_checked_at(result))
    return "\n".join(lines)


def _business_line(item: BusinessRelation, report: DebtorReport) -> list[str]:
    # Через словарь, а не через ``is_active``: состояний три, а у булева флага
    # два. ``egrul_ip`` не отдаёт статус у строк физлица вовсе — во всех живых
    # записях ``status: null``, — и печатать это как «прекращено» значит
    # закрывать действующее ИП должника одним словом. Отсутствие признака
    # никогда не выводится как прекращение.
    state = BUSINESS_STATUS_TITLES[item.status]
    # ``person_name`` первым: у живых строк ``egrul_ip`` ``name`` пуст, ФИО
    # лежит в нём, и без него найденное ИП покажется строкой «— ИНН».
    name = item.person_name or item.name or item.inn or "—"
    role = BUSINESS_ROLE_TITLES.get(item.role, "связь")
    lines = [f"• {role}: {name} — {state} ({_match_note(item.match_level)})"]
    lines.extend(_company_cases_lines(item, report))
    return lines


def _company_cases_lines(item: BusinessRelation, report: DebtorReport) -> list[str]:
    """Арбитраж компании — подразделом внутри БИЗНЕСа, а не своим разделом.

    Это факт о компании, а компания уже здесь. Отдельный верхнеуровневый раздел
    «СУДЫ КОМПАНИЙ» рядом с «СУДЫ» читался бы как продолжение дел самого
    должника, чем он не является.

    Молчать здесь нельзя ни в одном из трёх случаев: цепочка оплачивается
    отдельными вызовами, и источник, который опрошен и ничего не печатает,
    стоит денег и не даёт ничего взамен.
    """
    result = report.result_for(ProviderName.COURT_LEGAL)
    if result is None or not item.inn:
        return []
    cases = [case for case in report.legal_entity_cases if case.company_inn == item.inn]
    if not result.status.is_answered:
        return ["  Арбитраж компании: не проверено"]
    if not cases:
        return ["  Арбитраж компании: дел не найдено"]

    total = next((case.total_count for case in cases if case.total_count is not None), None)
    analyzed = next(
        (case.analyzed_count for case in cases if case.analyzed_count is not None), None
    )
    coverage = f"дел {total}, разобрано {analyzed} из {total}" if total is not None else ""
    lines = [f"  Арбитраж компании: {coverage or f'{len(cases)} дел'}"]

    against = [case for case in cases if case.is_claim_against_company]
    by_company = [case for case in cases if not case.is_claim_against_company]
    if by_company:
        amount = sum((case.amount or 0) for case in by_company)
        suffix = f" на {format_amount(amount)}" if amount else ""
        lines.append(f"    — компания истец: {len(by_company)} дел{suffix} (дебиторка)")
    lines.append(f"    — исков к компании: {len(against)}")
    for case in cases[:MAX_LISTED_CASES]:
        if case.enforcement_signal:
            lines.append(f"    — сигнал принудительного взыскания по делу {case.case_number}")
    lines.append(COMPANY_ASSETS_DISCLAIMER)
    return lines


def _pledge_block(report: DebtorReport) -> str:
    result = report.result_for(ProviderName.PLEDGE)
    header = "ЗАЛОГИ"
    unanswered = unanswered_line(result)
    if unanswered:
        return f"{header}\n{unanswered}"

    usable = [item for item in report.pledges if item.is_usable]
    if not usable:
        return _empty_block(
            header,
            result,
            "Записей в реестре залогов не найдено.",
            found=len(report.pledges),
            noun="запись",
            tail=PLEDGE_SCOPE_NOTE,
        )

    lines = [header]
    for item in usable[:MAX_LISTED_PLEDGES]:
        lines.extend(_pledge_lines(item))
    hidden = len(usable) - MAX_LISTED_PLEDGES
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    lines.extend(_source_notes(result))
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


def _inheritance_block(report: DebtorReport) -> str:
    """Наследственные дела — и границы того, что этот реестр вообще знает.

    Раздел не называется «наследственные дела должника» и не может так
    называться: реестр ищет по одному ФИО и возвращает всех однофамильцев.
    Поэтому подтверждённая запись подписана прямым выводом («должник умер»), а
    возможная — оговоркой, и оговорка охвата печатается в обеих ветках.

    Оговорка идёт через ``tail``, а не через ``result.notes``: notes у пустого
    раздела печатаются ВМЕСТО счётчика найденного (см. :func:`empty_reason`), и
    строка про границы источника стёрла бы фразу «в реестре найдено 1730 дел» —
    ровно ту, ради которой раздел и написан.
    """
    result = report.result_for(ProviderName.INHERITANCE)
    header = "НАСЛЕДСТВЕННЫЕ ДЕЛА"
    unanswered = unanswered_line(result)
    if unanswered:
        return "\n".join([header, unanswered, INHERITANCE_SCOPE_NOTE])

    usable = [item for item in report.inheritance_cases if item.is_usable]
    if not usable:
        return _empty_block(
            header,
            result,
            "Наследственных дел по этому ФИО не найдено.",
            found=len(report.inheritance_cases),
            noun="дело",
            tail=INHERITANCE_SCOPE_NOTE,
        )

    confirmed = [item for item in usable if item.is_confirmed]
    notes = _source_notes(result)
    lines = [header]
    if confirmed:
        # Главный вывод — первой строкой, до перечисления. Ради него источник и
        # подключён: иск к умершему суд не примет.
        lines.append(
            "Должник умер: дата рождения в записи реестра совпала. "
            "Требование предъявляется наследникам."
        )
    else:
        # Ни одного подтверждённого — значит, ниже идут однофамильцы, и сказать
        # об этом надо ДО списка, а не после. Список дел с фамилией должника,
        # начинающийся без объяснения, читается как список его дел, и подпись
        # «возможное совпадение» под каждой строкой этого не перебивает.
        lines.extend(notes)
        notes = []
    for item in usable[:MAX_LISTED_INHERITANCE]:
        lines.extend(_inheritance_lines(item))
    hidden = len(usable) - MAX_LISTED_INHERITANCE
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    lines.extend(notes)
    lines.append(INHERITANCE_SCOPE_NOTE)
    lines.append(_checked_at(result))
    return "\n".join(lines)


def _inheritance_lines(item: InheritanceCase) -> list[str]:
    state = "открыто" if item.is_open else "закрыто"
    lines = [f"• Дело {item.case_number or 'без номера'} — {state}"]
    if item.deceased_name:
        lines.append(f"  Наследодатель: {truncate(item.deceased_name, 90)}")
    if item.death_date:
        lines.append(f"  Дата смерти: {format_date(item.death_date)}")
    if item.case_date:
        lines.append(f"  Дело открыто: {format_date(item.case_date)}")
    if item.notary_name:
        # Контакт нотариуса — единственный практический следующий шаг: круг
        # наследников знает он, и больше никто.
        lines.append(f"  Нотариус: {truncate(item.notary_name, 90)}")
    if item.chamber_name:
        lines.append(f"  Палата: {truncate(item.chamber_name, 90)}")
    lines.append(f"  {_match_note(item.match_level)}")
    return lines


def _property_block(report: DebtorReport) -> str:
    """ЕГРН — про объект по известному нам адресу, а не про имущество должника.

    Раздел называется так и звучит так намеренно. Источник возвращает
    кадастровый номер, стоимость, обременения и число долей — и не возвращает ни
    одного ФИО, потому что сведения о правах конкретного лица ЕГРН выдаёт самому
    лицу, суду и приставу. Всё, что здесь напечатано, поэтому сопровождается
    оговоркой о непринадлежности; убрать её значит превратить справку об
    объекте в утверждение об имуществе.
    """
    result = report.result_for(ProviderName.PROPERTY)
    header = "ОБЪЕКТ ПО АДРЕСУ (ЕГРН)"
    if not report.properties:
        return _empty_block(
            header,
            result,
            NO_PROPERTY_FOUND,
            found=0,
            noun="объект",
            tail=OWNERSHIP_DISCLAIMER,
        )

    lines = [header]
    queried = report.subject.address
    if queried:
        lines.append(f"Проверен адрес из нашей карточки: {truncate(queried, 160)}")
    for item in report.properties[:MAX_LISTED_PROPERTIES]:
        lines.extend(_property_lines(item))
    hidden = len(report.properties) - MAX_LISTED_PROPERTIES
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    lines.extend(_source_notes(result))
    lines.append(OWNERSHIP_DISCLAIMER)
    lines.append(_checked_at(result))
    return "\n".join(lines)


def _property_lines(item: PropertyRecord) -> list[str]:
    title = item.property_type or "объект недвижимости"
    area = f" — {item.area} м²" if item.area else ""
    lines = [f"• {title}{area}"]
    if item.cadastral_number:
        lines.append(f"  Кадастровый номер: {item.cadastral_number}")
    if item.cancelled_at:
        # Снятый с учёта объект — не актив, и печатать его стоимость рядом с
        # «взыскание» значило бы предлагать обратить взыскание на несуществующее.
        lines.append(f"  Объект снят с кадастрового учёта {format_date(item.cancelled_at)}")
    if item.encumbrances:
        # Впереди стоимости: ипотека означает, что перед нами уже стоит банк.
        lines.append(f"  Обременения в ЕГРН: {len(item.encumbrances)}")
        lines.extend(f"    — {truncate(text, 100)}" for text in item.encumbrances)
    elif item.encumbrances_checked:
        lines.append("  Обременений в ЕГРН не зарегистрировано")
    if item.cadastral_cost is not None and not item.cancelled_at:
        date_note = f" (на {format_date(item.cost_date)})" if item.cost_date else ""
        lines.append(f"  Кадастровая стоимость: {format_amount(item.cadastral_cost)}{date_note}")
    lines.append(f"  {_rights_line(item)}")
    return lines


def _rights_line(item: PropertyRecord) -> str:
    if not item.rights_count:
        # Не «объект ничей»: сведений о правах в ответе просто нет.
        return "Сведения о правах в ответе отсутствуют"
    kinds = ", ".join(text.lower() for text in item.right_types) or "право собственности"
    noun = pluralize_ru(item.rights_count, "запись", "записи", "записей")
    shares = f" (доли {', '.join(item.shares)})" if item.shares else ""
    return f"Права: {kinds}, {item.rights_count} {noun}{shares}"


def _court_block(report: DebtorReport) -> str:
    """Арбитраж — и только он.

    Названо честно в самом тексте: суды общей юрисдикции этот источник не
    покрывает, а «дел не найдено» без такой оговорки прочиталось бы как «на него
    никто не подавал».
    """
    result = report.result_for(ProviderName.COURT)
    header = "СУДЫ (АРБИТРАЖ)"
    unanswered = unanswered_line(result, bridge=report.result_for(ProviderName.INN_BRIDGE))
    if unanswered:
        return f"{header}\n{unanswered}"

    usable = [item for item in report.court_cases if item.is_usable]
    if not usable:
        return _empty_block(
            header,
            result,
            "Арбитражных дел не найдено.",
            found=len(report.court_cases),
            noun="дело",
            tail=COURT_SCOPE_NOTE,
        )

    lines = [header]
    for item in usable[:MAX_LISTED_CASES]:
        lines.extend(_court_lines(item))
    hidden = len(usable) - MAX_LISTED_CASES
    if hidden > 0:
        lines.append(f"…и ещё {hidden}")
    # Порядок «оговорки источника → оговорка охвата → отметка о проверке» тот
    # же, что в _pledge_block: источник говорит о полноте своего ответа, а
    # константа — о том, чего он не видит в принципе. Это разные вещи, и обе
    # обязаны быть напечатаны.
    lines.extend(_source_notes(result))
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
    # Мост записей не приносит, он их делает возможными: общая ветка напечатала
    # бы «✓ … — 0 зап.» для успешно полученного ИНН.
    if result.provider is ProviderName.INN_BRIDGE:
        return _bridge_source_line(result, title)
    # У внутренней базы записи лежат в самом отчёте, а не в результате: он
    # несёт только состояние источника.
    records = len(report.internal_records) if result.provider is ProviderName.INTERNAL else None
    # Все остальные состояния, включая «ответ неполный», приходят из общей
    # таблицы source_state: подпись в списке источников и чип на веб-странице
    # обязаны говорить об одном источнике одно и то же.
    state = source_state(result, records=records)
    return f"{state.mark} {title} — {state.label}"


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


def _empty_block(
    header: str,
    result: ProviderResult | None,
    empty_line: str,
    *,
    found: int,
    noun: str,
    tail: str = "",
) -> str:
    """Секция без единой показанной записи — и точный ответ, почему.

    «Ничего не найдено» здесь имеет право быть напечатанным ровно в одном
    случае: источник ответил, ответил целиком, и в ответе действительно ничего
    не было. Остальные два случая выглядят так же — ноль строк на экране, — и
    оба означают обратное:

    *   источник сказал, что нашёл больше, чем прислал (``is_partial``: ФНП с
        тринадцатью несопоставленными уведомлениями, арбитраж с десятью делами
        из сорока);
    *   записи пришли, но ни одна не сопоставлена с должником настолько, чтобы
        её показывать. Найденное дело, отсеянное как чужое, — это повод для
        ручной проверки, а не повод написать «не обнаружено» и начислить плюс.

    ``tail`` — оговорка о границах самого источника: что он покрывает, а что
    нет. Печатается во всех трёх случаях, потому что говорит о другом — о том,
    чего этот источник не знает в принципе, независимо от полноты ответа.
    """
    lines = [header, *empty_reason(result, found=found, noun=noun, empty_line=empty_line)]
    lines.append(tail)
    lines.append(_checked_at(result))
    return "\n".join(line for line in lines if line)


def empty_reason(
    result: ProviderResult | None, *, found: int, noun: str, empty_line: str
) -> list[str]:
    """Строки о том, ПОЧЕМУ в разделе не показано ни одной записи.

    Публичная и общая на чат и на страницу намеренно: раньше решение принимали
    оба вывода порознь, и веб-страница печатала голое «не найдено» там, где
    текст бота уже говорил «источник вернул 1 запись, сопоставить не удалось».
    Один и тот же должник получал два разных ответа, причём неправ был именно
    лист, который уходит в дело.

    Оговорки источника возвращаются ВМЕСТО «не найдено», а не вместе с ним:
    «записей не найдено» рядом с «в реестре ФНП найдено 13 уведомлений» —
    это две взаимоисключающие фразы в одном абзаце.
    """
    notes = _source_notes(result)
    if notes:
        return notes
    if found:
        word = pluralize_ru(found, noun, _plural_noun(noun), _genitive_noun(noun))
        return [
            f"Источник вернул {found} {word}, но сопоставить с должником не удалось "
            "ни одну — требуется ручная проверка."
        ]
    return [empty_line]


_NOUN_FORMS: dict[str, tuple[str, str]] = {
    "запись": ("записи", "записей"),
    "связь": ("связи", "связей"),
    "дело": ("дела", "дел"),
}


def _plural_noun(noun: str) -> str:
    return _NOUN_FORMS[noun][0]


def _genitive_noun(noun: str) -> str:
    return _NOUN_FORMS[noun][1]


def _source_notes(result: ProviderResult | None) -> list[str]:
    """То, что источник сказал о полноте собственного ответа."""
    return list(result.notes) if result is not None else []


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


def _notes(result: ProviderResult | None) -> list[str]:
    """Оговорки самого источника о полноте ответа.

    Печатаются и когда записей нет: «дел не найдено» после усечённой страницы —
    это не то, что источник сказал.
    """
    return list(result.notes) if result is not None else []


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
