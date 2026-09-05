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
    PledgeStatus,
    ProviderName,
    ProviderStatus,
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

    return [
        ScoreFactor(
            name="no_bankruptcy",
            delta=NO_BANKRUPTCY_BONUS,
            reason="банкротство не обнаружено",
            source=ProviderName.FEDRESURS,
        )
    ]


# ---------------------------------------------------------------- enforcement


def _enforcement_factors(report: DebtorReport) -> list[ScoreFactor]:
    result = report.result_for(ProviderName.FSSP)
    if result is None or not result.is_answered:
        return []

    active = report.active_proceedings
    if not active:
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

    terminated = [item for item in usable if not item.is_active]
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
        if provider is ProviderName.INTERNAL:
            notes.extend(_internal_coverage(report))
            if _internal_answered(report):
                answered_weight += weight
            continue
        result = report.result_for(provider)
        if result is not None and result.is_answered:
            answered_weight += weight
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


def _internal_answered(report: DebtorReport) -> bool:
    """Ответил ли внутренний контур — а не «нашлось ли в нём что-нибудь».

    ``NO_RESULTS`` — это ответ, и он засчитывается в покрытие так же, как у
    внешних источников. Недоступная 1С — не ответ, и вес за неё не начисляется.
    """
    result = report.result_for(ProviderName.INTERNAL)
    if result is None:
        # Старый путь: статуса внутреннего контура в отчёте нет вовсе.
        return bool(report.internal_records)
    return result.is_answered


def _internal_coverage(report: DebtorReport) -> list[str]:
    result = report.result_for(ProviderName.INTERNAL)
    if result is not None and not result.is_answered:
        # Раньше здесь стояла та же заметка, что и у честно пустой базы:
        # оператор читал «нет данных во внутренней базе» и понимал это как
        # проверенный факт, а не как несостоявшуюся проверку.
        return [_unanswered_note(ProviderName.INTERNAL, result)]
    if not report.internal_records:
        return ["нет данных во внутренней базе"]
    return []


def _unanswered_note(provider: ProviderName, result: ProviderResult | None) -> str:
    title = PROVIDER_TITLES.get(provider, provider.value)
    if result is None:
        return f"{title}: источник не опрошен"
    if result.status is ProviderStatus.NOT_CONFIGURED:
        return f"{title}: источник не подключён"
    if result.status is ProviderStatus.UNAVAILABLE:
        return f"{title}: источник недоступен"
    return f"{title}: ошибка обращения к источнику"


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
