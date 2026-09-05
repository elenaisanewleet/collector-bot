"""The recovery-score engine.

Deterministic arithmetic over the aggregated report — the same report always
produces the same score, and every point of movement is attributable to a named
factor. No model is involved, by design: an operator has to be able to explain
this number to a lawyer.

Two numbers come out, and they answer different questions:

``score``       how collectable the debt looks, given what we found
``confidence``  how much of the picture we actually managed to see
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.domain.enums import (
    PROVIDER_TITLES,
    BankruptcyStatus,
    BusinessStatus,
    CourtCaseRole,
    PledgeStatus,
    ProviderName,
)
from app.domain.models import (
    BusinessRelation,
    DebtorReport,
    ProviderResult,
    RecoveryScore,
    ScoreFactor,
)
from app.domain.scoring import (
    ACTIVE_BANKRUPTCY_PENALTY,
    ACTIVE_LEGAL_ENTITY_ROLE_BONUS,
    ACTIVE_PLEDGE_PENALTY,
    ACTIVE_SOLE_PROPRIETOR_BONUS,
    BASE_SCORE,
    CLAIM_AGAINST_DEBTOR_PENALTY,
    COMPLETED_BANKRUPTCY_PENALTY,
    CONFIRMED_PROPERTY_BONUS,
    CONFIRMED_VEHICLE_BONUS,
    ENFORCEMENT_AMOUNT_PENALTIES,
    ENFORCEMENT_COUNT_PENALTIES,
    MAX_BUSINESS_BONUS,
    MAX_CLAIM_PENALTY,
    MAX_PLEDGE_PENALTY,
    MAX_TERMINATED_BUSINESS_PENALTY,
    MIN_CONFIDENCE,
    NO_BANKRUPTCY_BONUS,
    NO_COURT_CLAIMS_BONUS,
    NO_ENFORCEMENT_BONUS,
    NO_PLEDGE_BONUS,
    PROBABLE_MATCH_CONFIDENCE_FACTOR,
    PROVIDER_CONFIDENCE_WEIGHTS,
    TERMINATED_BUSINESS_PENALTY,
    UNKNOWN_BANKRUPTCY_STATE_PENALTY,
    WEAK_IDENTITY_CONFIDENCE_FACTOR,
    categorize,
    clamp,
)
from app.utils.formatting import pluralize_ru


class RecoveryScoreEngine:
    """Applies the rules in :mod:`app.domain.scoring` to a report."""

    def evaluate(self, report: DebtorReport) -> RecoveryScore:
        factors: list[ScoreFactor] = []
        factors.extend(_bankruptcy_factors(report))
        factors.extend(_enforcement_factors(report))
        factors.extend(_business_factors(report))
        factors.extend(_pledge_factors(report))
        factors.extend(_court_factors(report))
        factors.extend(_asset_factors(report))

        total = BASE_SCORE + sum(factor.delta for factor in factors)
        score = clamp(total)
        confidence, notes = _confidence(report)

        return RecoveryScore(
            score=score,
            base_score=BASE_SCORE,
            category=categorize(score).value,
            confidence=confidence,
            factors=tuple(factors),
            confidence_notes=tuple(notes),
        )


# ---------------------------------------------------------------- bankruptcy


def _bankruptcy_factors(report: DebtorReport) -> list[ScoreFactor]:
    """Bankruptcy dominates the score — an active procedure effectively ends
    ordinary recovery, so it carries the single largest penalty."""
    result = report.result_for(ProviderName.FEDRESURS)
    if result is None or not result.is_answered:
        # Unchecked is not clean: no bonus, no penalty, and the confidence
        # calculation records the gap.
        return []

    active = report.active_bankruptcies
    if active:
        record = active[0]
        procedure = record.procedure or "процедура банкротства"
        return [
            ScoreFactor(
                name="active_bankruptcy",
                delta=ACTIVE_BANKRUPTCY_PENALTY,
                reason=f"активное банкротство: {procedure}",
                source=ProviderName.FEDRESURS,
            )
        ]

    usable = [item for item in report.bankruptcies if item.is_usable]

    # «Состояние не прочитано» — это не «процедура завершена». ``bankrot_person``
    # не отдаёт ни процедуры, ни дат начала и окончания, поэтому у найденного
    # дела состояние берётся из одной строки статуса, и незнакомая формулировка
    # оставляет запись в UNKNOWN. Пока она попадала в ``completed``, найденное
    # дело стоило должнику −10 вместо −35: неполнота ответа превращалась в
    # скидку. Проверяется раньше завершённых: из двух дел решает худшее.
    unknown = [item for item in usable if item.status is BankruptcyStatus.UNKNOWN]
    if unknown:
        return [
            ScoreFactor(
                name="bankruptcy_state_unknown",
                delta=UNKNOWN_BANKRUPTCY_STATE_PENALTY,
                reason=(
                    "найдено дело о банкротстве, состояние процедуры источник не сообщил — "
                    "считаем как незавершённое"
                ),
                source=ProviderName.FEDRESURS,
            )
        ]

    completed = [item for item in usable if not item.is_active]
    if completed:
        return [
            ScoreFactor(
                name="completed_bankruptcy",
                delta=COMPLETED_BANKRUPTCY_PENALTY,
                reason="завершённая процедура банкротства в анамнезе",
                source=ProviderName.FEDRESURS,
            )
        ]

    if _incomplete_answer(result, unmatched=_unmatched(report.bankruptcies)):
        return []

    return [
        ScoreFactor(
            name="no_bankruptcy",
            delta=NO_BANKRUPTCY_BONUS,
            reason="банкротство не обнаружено",
            source=ProviderName.FEDRESURS,
        )
    ]


def _incomplete_answer(result: ProviderResult, *, unmatched: int) -> bool:
    """Заслужен ли плюс «мы посмотрели и ничего не нашли».

    Он заслужен, только если посмотрели всё. Не заслужен в двух случаях, и оба
    выглядят как пустая выдача:

    *   источник сам сообщил, что прислал не всё (``is_partial``);
    *   записи пришли, но ни одна не сопоставлена с должником. Отсев по
        отождествлению — это «мы не уверены, что это он», а не «этого нет»;
        начислять за такое бонус значит платить должнику за то, что источник
        записал его фамилию иначе. Это верно для источников, которые
        адресуются точным идентификатором — ИНН или VIN, — то есть для всех,
        кто зовёт эту функцию; у ФССП, ищущей по ФИО, вопрос другой, и там
        учитывается только ``is_partial`` (см. ``_enforcement_factors``).

    Ни штрафа, ни бонуса: неизвестность — не факт. Причина попадает в
    «Ограничения оценки» через :func:`_confidence`.
    """
    return result.is_partial or unmatched > 0


def _unmatched(records: Sequence[object]) -> int:
    """Сколько записей источник прислал, а отождествление не пропустило."""
    return sum(1 for item in records if not getattr(item, "is_usable", False))


def _cases_with_unknown_role(report: DebtorReport) -> bool:
    """Есть ли действующее дело, роль должника в котором не определена.

    ``is_against_debtor`` требует роли ответчика, и дело с ролью OTHER не
    попадает ни в иски к должнику, ни куда-либо ещё, — то есть молча
    засчитывается в пользу должника. Роль остаётся неопределённой у дела,
    которое вендор не разобрал подробно, и у дела, где стороной записано ЮЛ, а
    не человек: и то и другое живьём встречается.
    """
    return any(
        item.is_usable and item.is_active and item.role is CourtCaseRole.OTHER
        for item in report.court_cases
    )


# ---------------------------------------------------------------- enforcement


def _enforcement_factors(report: DebtorReport) -> list[ScoreFactor]:
    result = report.result_for(ProviderName.FSSP)
    if result is None or not result.is_answered:
        return []

    active = report.active_proceedings
    if not active:
        if result.is_partial:
            return []
        # Несопоставленная запись здесь бонус НЕ отменяет, в отличие от
        # остальных источников, и разница не в осторожности, а в вопросе.
        # ФССП ищется по ФИО с датой рождения, то есть возвращает в том числе
        # однофамильцев: их производства — не «наши, но неузнанные», а чужие, и
        # молчать из-за них о чистой ФССП значило бы наказывать должника за
        # тёзку. Банкротство, арбитраж и залоги адресуются точным
        # идентификатором — там несовпавшее ФИО означает ровно обратное.
        return [
            ScoreFactor(
                name="no_enforcement",
                delta=NO_ENFORCEMENT_BONUS,
                reason="активных исполнительных производств не найдено",
                source=ProviderName.FSSP,
            )
        ]

    factors: list[ScoreFactor] = []
    count = len(active)
    for count_threshold, penalty in ENFORCEMENT_COUNT_PENALTIES:
        if count >= count_threshold:
            noun = pluralize_ru(count, "производство", "производства", "производств")
            factors.append(
                ScoreFactor(
                    name="active_enforcement_count",
                    delta=penalty,
                    reason=f"{count} активных исполнительных {noun}",
                    source=ProviderName.FSSP,
                )
            )
            break

    total = report.total_enforcement_amount
    for amount_threshold, amount_penalty in ENFORCEMENT_AMOUNT_PENALTIES:
        if total >= amount_threshold:
            factors.append(
                ScoreFactor(
                    name="enforcement_amount",
                    delta=amount_penalty,
                    reason=f"подтверждённая сумма взысканий {_round_amount(total)} ₽",
                    source=ProviderName.FSSP,
                )
            )
            break

    return factors


def _round_amount(value: Decimal) -> str:
    return f"{int(value):,}".replace(",", " ")


# ---------------------------------------------------------------- business


def _business_factors(report: DebtorReport) -> list[ScoreFactor]:
    """Business activity is a positive *indicator*, never proof of income."""
    result = report.result_for(ProviderName.FNS)
    if result is None or not result.is_answered:
        return []

    usable = [item for item in report.business_relations if item.is_usable]
    factors: list[ScoreFactor] = []

    bonus_total = 0
    for relation in usable:
        if not relation.is_active:
            continue
        delta = (
            ACTIVE_SOLE_PROPRIETOR_BONUS
            if relation.is_active_sole_proprietor
            else ACTIVE_LEGAL_ENTITY_ROLE_BONUS
        )
        if bonus_total + delta > MAX_BUSINESS_BONUS:
            delta = MAX_BUSINESS_BONUS - bonus_total
        if delta <= 0:
            break
        bonus_total += delta
        factors.append(
            ScoreFactor(
                name="active_business_relation",
                delta=delta,
                reason=_business_reason(relation),
                source=ProviderName.FNS,
            )
        )

    # Только явно прекращённые. ``not is_active`` сваливало сюда и UNKNOWN, а
    # живой ``egrul_ip`` не отдаёт статус у строк физлица вовсе: должник с
    # действующим ИП и двумя ролями в ЮЛ получал штраф за «3 прекращённых
    # бизнес-связи». Знак фактора был обратен факту.
    terminated = [item for item in usable if item.status is BusinessStatus.TERMINATED]
    if terminated:
        penalty = max(
            TERMINATED_BUSINESS_PENALTY * len(terminated),
            MAX_TERMINATED_BUSINESS_PENALTY,
        )
        count = len(terminated)
        adjective = pluralize_ru(count, "прекращённая", "прекращённые", "прекращённых")
        noun = pluralize_ru(count, "связь", "связи", "связей")
        factors.append(
            ScoreFactor(
                name="terminated_business_relation",
                delta=penalty,
                reason=f"{count} {adjective} бизнес-{noun}",
                source=ProviderName.FNS,
            )
        )

    return factors


def _business_reason(relation: BusinessRelation) -> str:
    if relation.is_active_sole_proprietor:
        return "действующее ИП"
    label = relation.name or "юрлицо"
    return f"активная роль в ЮЛ: {label}"


# ---------------------------------------------------------------- pledges


def _pledge_factors(report: DebtorReport) -> list[ScoreFactor]:
    """A pledged asset is somebody else's security, not our collateral."""
    result = report.result_for(ProviderName.PLEDGE)
    if result is None or not result.is_answered:
        return []

    active = report.active_pledges
    if not active:
        # Уведомление, состояние которого прочитать не удалось, — это не
        # снятый залог. Плюс здесь означает «мы посмотрели и ничего, что могло
        # бы действовать, не увидели», поэтому такая запись его отменяет: у
        # ФНП состояния как поля нет вообще, есть тип сообщения, и незнакомый
        # тип оставляет запись UNKNOWN. Штрафа при этом нет — залоговый штраф
        # считается за штуку и имеет потолок, а домысливать «залог
        # действует» по непрочитанному типу сообщения не на чем.
        unknown = [
            item
            for item in report.pledges
            if item.is_usable and item.status is PledgeStatus.UNKNOWN
        ]
        if unknown:
            return []
        if _incomplete_answer(result, unmatched=_unmatched(report.pledges)):
            # Тринадцать уведомлений ФНП, ни одно не сопоставленное по дате
            # рождения, — это не чистый реестр. Плюс здесь означал бы, что мы
            # посмотрели и не увидели ничего, что могло бы действовать.
            return []
        # Названо ровно тем, что проверено. Ответ pledge_* несёт две ветки, а
        # карта полей читает одну — ФНП; ипотеки и лизинга здесь нет вовсе.
        # «Имущество не обременено» было бы выводом обо всём имуществе на
        # основании одного реестра движимого, и одного действующего лизинга
        # хватило бы, чтобы этот плюс оказался ложью.
        return [
            ScoreFactor(
                name="no_pledges",
                delta=NO_PLEDGE_BONUS,
                reason="в реестре уведомлений ФНП действующих залогов не найдено",
                source=ProviderName.PLEDGE,
            )
        ]

    count = len(active)
    delta = max(ACTIVE_PLEDGE_PENALTY * count, MAX_PLEDGE_PENALTY)
    adjective = pluralize_ru(count, "действующий", "действующих", "действующих")
    noun = pluralize_ru(count, "залог", "залога", "залогов")
    return [
        ScoreFactor(
            name="active_pledge",
            delta=delta,
            reason=f"{count} {adjective} {noun}: залогодержатель удовлетворяется раньше нас",
            source=ProviderName.PLEDGE,
        )
    ]


# ---------------------------------------------------------------- courts


def _court_factors(report: DebtorReport) -> list[ScoreFactor]:
    """Live claims against the debtor are creditors already ahead of us."""
    result = report.result_for(ProviderName.COURT)
    if result is None or not result.is_answered:
        return []

    claims = report.claims_against_debtor
    if not claims:
        if _incomplete_answer(result, unmatched=_unmatched(report.court_cases)):
            # Десять дел из сорока — не повод утверждать, что исков нет.
            return []
        if _cases_with_unknown_role(report):
            # Дело есть, а на какой должник в нём стороне — неизвестно: у КАД
            # плоского поля роли нет, участники лежат в карточке, а карточку
            # вендор разбирает не у каждого дела. Такое дело печатается в
            # отчёте, и печатать рядом «исков к должнику не найдено» значит
            # опровергать собственный отчёт строкой ниже. Ни бонуса, ни штрафа:
            # «иная роль» — это не «истец».
            return []
        return [
            ScoreFactor(
                name="no_court_claims",
                delta=NO_COURT_CLAIMS_BONUS,
                reason="действующих арбитражных исков к должнику не найдено",
                source=ProviderName.COURT,
            )
        ]

    count = len(claims)
    delta = max(CLAIM_AGAINST_DEBTOR_PENALTY * count, MAX_CLAIM_PENALTY)
    adjective = pluralize_ru(count, "действующий", "действующих", "действующих")
    kind = pluralize_ru(count, "арбитражный", "арбитражных", "арбитражных")
    noun = pluralize_ru(count, "иск", "иска", "исков")
    return [
        ScoreFactor(
            name="claims_against_debtor",
            delta=delta,
            reason=f"{count} {adjective} {kind} {noun} к должнику",
            source=ProviderName.COURT,
        )
    ]


# ---------------------------------------------------------------- assets


def _asset_factors(report: DebtorReport) -> list[ScoreFactor]:
    """Only confirmed assets count. A probable match on a flat is not a flat.

    For real estate that means two conditions, not one. The ЕГРН source answers
    about an *object at an address* and does not name a rightholder, so a record
    from it can be a perfect match on the address and still say nothing about
    who owns the thing. ``owner_confirmed`` is what carries that, and this
    source never sets it: an object nobody attributed to the debtor must not add
    fifteen points to how collectable their debt looks.
    """
    factors: list[ScoreFactor] = []

    confirmed_property = [
        item for item in report.properties if item.is_confirmed and item.owner_confirmed
    ]
    if confirmed_property:
        factors.append(
            ScoreFactor(
                name="confirmed_property",
                delta=CONFIRMED_PROPERTY_BONUS,
                reason=f"подтверждённое имущество: {len(confirmed_property)} объект(ов)",
                source=ProviderName.PROPERTY,
            )
        )

    confirmed_vehicles = [item for item in report.vehicles if item.is_confirmed]
    if confirmed_vehicles:
        factors.append(
            ScoreFactor(
                name="confirmed_vehicle",
                delta=CONFIRMED_VEHICLE_BONUS,
                reason=f"подтверждённый транспорт: {len(confirmed_vehicles)} ед.",
                source=ProviderName.VEHICLE,
            )
        )

    return factors


# ---------------------------------------------------------------- confidence


def _confidence(report: DebtorReport) -> tuple[float, list[str]]:
    """Coverage-weighted confidence, reduced for weak identification.

    Sources that never answered contribute nothing to coverage, so a report
    built on one reachable provider cannot claim to be complete.
    """
    notes: list[str] = []
    total_weight = 0.0
    answered_weight = 0.0

    for provider_key, weight in PROVIDER_CONFIDENCE_WEIGHTS.items():
        total_weight += weight
        provider = ProviderName(provider_key)
        result = report.result_for(provider)
        if provider is ProviderName.INTERNAL:
            if result is not None and result.is_answered:
                # База ответила — вес засчитан. Пустой ответ это тоже ответ, и
                # называть его непроверенным значит занижать уверенность.
                answered_weight += weight
                if not report.internal_records:
                    notes.append("нет данных во внутренней базе")
                continue
            if result is None:
                # Состояния внутренней базы в отчёте нет вовсе — отчёт собран в
                # обход SearchService. Единственное честное утверждение выводится
                # из самого субъекта: «не нашли» и «нечем было искать» разные
                # вещи, а записи могут лежать в отчёте и без ProviderResult.
                if report.internal_records:
                    answered_weight += weight
                else:
                    notes.append(_internal_note(report))
                continue
            # База ответила отказом: причину назовёт общая ветка ниже, из ответа
            # источника, а не из догадки по субъекту.
        if result is not None and result.is_answered:
            answered_weight += weight
            if result.is_partial:
                # Источник ответил — вес засчитан, — но ответил не полностью, и
                # «Ограничения оценки» обязаны это назвать: иначе неполный
                # ответ неотличим от исчерпывающего.
                title = PROVIDER_TITLES.get(provider, provider.value)
                notes.append(f"{title}: источник прислал не всё, что нашёл")
        else:
            notes.append(_unanswered_note(provider, result))

    coverage = answered_weight / total_weight if total_weight else 0.0
    confidence = coverage

    if not report.subject.identity_key.has_strong_identifier:
        confidence *= WEAK_IDENTITY_CONFIDENCE_FACTOR
        notes.append("поиск выполнен без даты рождения или ИНН")

    if _has_only_probable_matches(report):
        confidence *= PROBABLE_MATCH_CONFIDENCE_FACTOR
        notes.append("часть записей сопоставлена как возможные совпадения")

    return max(MIN_CONFIDENCE, round(confidence, 2)), notes


def _internal_note(report: DebtorReport) -> str:
    """«Не нашли» и «нечем было искать» — разные утверждения.

    Поиск по одному ИНН во внутренней базе не реализован вовсе (см.
    ``SearchService._internal_candidates``), поэтому субъект, у которого нет ни
    ФИО, ни телефона, ни договора, ни машины, ни адреса, приходит сюда с пустым
    результатом, которого никто не получал.

    Запасной путь: он работает только там, где состояния источника в отчёте нет
    (отчёт собран мимо ``SearchService``). Когда состояние есть, то же самое
    говорит сам источник — ``insufficient_query`` из
    :meth:`app.services.search.SearchService.lookup_internal_result`. Предикат
    ниже перечисляет ровно те же поля, что ``search._has_internal_query``;
    правится один — правится и второй, иначе фолбэк начнёт врать про поиск,
    которого не было.
    """
    subject = report.subject
    vehicle = subject.vehicle
    searchable = any(
        (
            subject.name,
            subject.phone,
            subject.contract_number,
            subject.claim_number,
            subject.debtor_id,
            subject.address,
            vehicle.vin if vehicle else None,
            vehicle.plate if vehicle else None,
        )
    )
    if searchable:
        return "нет данных во внутренней базе"
    return "во внутренней базе искать было нечем: ни ФИО, ни телефона, ни договора"


def _unanswered_note(provider: ProviderName, result: ProviderResult | None) -> str:
    """Строка про непроверенный источник в «Ограничениях оценки».

    Формулировка берётся из общей таблицы состояний, а не сочиняется здесь:
    свой набор слов на четвёртом выводе уже терял разницу между «не хватило
    данных для запроса» и «ошибка обращения».

    Единственное исключение — «недостаточно данных». Таблица знает, что данных
    не хватило, но не знает, каких именно; знает это только провайдер, и
    оператору нужно ровно это. Без названия поля строка читается как сбой
    («попробуйте позже»), а не как «дошлите ИНН», — то есть «не спросили» опять
    маскируется под «спросили, не вышло». Такая же связка «таблица плюс
    сообщение провайдера» стоит в :mod:`app.services.verdict` и в
    :func:`app.services.reporting.unanswered_line`, третьего набора слов не
    появляется.
    """
    from app.services.reporting import SourceStateCode, source_state

    title = PROVIDER_TITLES.get(provider, provider.value)
    state = source_state(result)
    if state.code is SourceStateCode.INSUFFICIENT and result is not None and result.error_message:
        return f"{title}: {result.error_message}"
    return f"{title}: {state.label}"


def _has_only_probable_matches(report: DebtorReport) -> bool:
    matched: Sequence[object] = [
        *report.enforcement_proceedings,
        *report.bankruptcies,
        *report.business_relations,
        *report.pledges,
        *report.court_cases,
    ]
    usable = [item for item in matched if getattr(item, "is_usable", False)]
    if not usable:
        return False
    return not any(getattr(item, "is_confirmed", False) for item in usable)
