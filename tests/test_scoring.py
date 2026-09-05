"""Recovery-score rules.

The load-bearing assertion in this file is
:func:`test_unavailable_provider_is_not_treated_as_clean`: a source that failed
must never earn the "checked and clean" bonus.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from app.domain.enums import (
    BankruptcyStatus,
    PledgeStatus,
    ProviderName,
    ProviderStatus,
    ScoreCategory,
)
from app.domain.identity import SearchSubject
from app.domain.models import BankruptcyRecord, DebtorReport, FactRecord
from app.domain.scoring import ACTIVE_BANKRUPTCY_PENALTY, BASE_SCORE
from app.services.scoring import RecoveryScoreEngine
from tests.conftest import (
    make_bankruptcy,
    make_business,
    make_court_case,
    make_pledge,
    make_proceeding,
    provider_result,
)

ProviderSpec = tuple[ProviderStatus, Sequence[FactRecord]]


def build_report(
    subject: SearchSubject,
    *,
    fssp: ProviderSpec | None = None,
    fedresurs: ProviderSpec | None = None,
    fns: ProviderSpec | None = None,
    pledge: ProviderSpec | None = None,
    court: ProviderSpec | None = None,
) -> DebtorReport:
    """Assemble a report directly, bypassing the providers.

    Scoring is the unit under test here; going through the network layer would
    only add noise.
    """
    report = DebtorReport(subject=subject)
    buckets: list[tuple[ProviderName, ProviderSpec | None, list[Any]]] = [
        (ProviderName.FSSP, fssp, report.enforcement_proceedings),
        (ProviderName.FEDRESURS, fedresurs, report.bankruptcies),
        (ProviderName.FNS, fns, report.business_relations),
        (ProviderName.PLEDGE, pledge, report.pledges),
        (ProviderName.COURT, court, report.court_cases),
    ]
    for provider, spec, bucket in buckets:
        if spec is None:
            continue
        status, records = spec
        report.provider_results.append(provider_result(provider, status, records))
        bucket.extend(records)
    return report


def test_base_score_with_no_data(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    report = build_report(person_subject)
    score = score_engine.evaluate(report)
    assert score.score == BASE_SCORE
    assert score.factors == ()


def test_active_bankruptcy_sinks_the_score(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    report = build_report(
        person_subject,
        fedresurs=(ProviderStatus.SUCCESS, [make_bankruptcy(active=True)]),
    )
    score = score_engine.evaluate(report)
    assert score.score == BASE_SCORE - 35
    assert score.category == ScoreCategory.LOW.value
    assert any(factor.name == "active_bankruptcy" for factor in score.factors)


def test_a_bankruptcy_whose_state_was_not_read_gets_no_discount(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    """Непрочитанное состояние процедуры — не завершённая процедура.

    Между активным банкротством и завершённым 25 баллов разницы, и запись с
    непрочитанным состоянием попадала в дешёвую половину: источник промолчал —
    должник получил скидку. Дороже ошибиться в другую сторону: недооценённая
    живая процедура — это поданный иск и потраченная пошлина.
    """
    record = BankruptcyRecord(
        debtor_name="Тестов Андрей Сергеевич",
        case_number="А40-1/2026",
        status=BankruptcyStatus.UNKNOWN,
    )
    record.match_confidence = 1.0
    report = build_report(person_subject, fedresurs=(ProviderStatus.SUCCESS, [record]))

    score = score_engine.evaluate(report)
    factor = next(factor for factor in score.factors if factor.name == "bankruptcy_state_unknown")

    assert factor.delta == ACTIVE_BANKRUPTCY_PENALTY
    assert not any(factor.name == "completed_bankruptcy" for factor in score.factors)
    # И тем более не плюс за чистую проверку: дело-то нашлось.
    assert not any(factor.name == "no_bankruptcy" for factor in score.factors)


def test_clean_bankruptcy_check_earns_a_bonus(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    report = build_report(person_subject, fedresurs=(ProviderStatus.NO_RESULTS, []))
    score = score_engine.evaluate(report)
    assert score.score == BASE_SCORE + 10
    assert any(factor.name == "no_bankruptcy" for factor in score.factors)


@pytest.mark.parametrize(
    ("count", "expected_penalty"),
    [(1, -5), (2, -5), (3, -15), (5, -15), (6, -25), (9, -25)],
)
def test_enforcement_count_bands(
    person_subject: SearchSubject,
    score_engine: RecoveryScoreEngine,
    count: int,
    expected_penalty: int,
) -> None:
    proceedings = [make_proceeding(f"{index}/26/77001-ИП", amount="1000") for index in range(count)]
    report = build_report(person_subject, fssp=(ProviderStatus.SUCCESS, proceedings))
    score = score_engine.evaluate(report)
    count_factor = next(
        factor for factor in score.factors if factor.name == "active_enforcement_count"
    )
    assert count_factor.delta == expected_penalty


def test_large_enforcement_amount_adds_a_further_penalty(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    report = build_report(
        person_subject,
        fssp=(ProviderStatus.SUCCESS, [make_proceeding(amount="1500000")]),
    )
    score = score_engine.evaluate(report)
    amount_factor = next(factor for factor in score.factors if factor.name == "enforcement_amount")
    assert amount_factor.delta == -15


def test_active_sole_proprietor_raises_the_score(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    report = build_report(
        person_subject, fns=(ProviderStatus.SUCCESS, [make_business(active=True)])
    )
    score = score_engine.evaluate(report)
    assert score.score == BASE_SCORE + 10


def test_business_bonus_is_capped(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    relations = [make_business(active=True) for _ in range(5)]
    report = build_report(person_subject, fns=(ProviderStatus.SUCCESS, relations))
    score = score_engine.evaluate(report)
    total_bonus = sum(f.delta for f in score.factors if f.delta > 0)
    assert total_bonus == 20


@pytest.mark.parametrize(
    "status", [ProviderStatus.NOT_CONFIGURED, ProviderStatus.UNAVAILABLE, ProviderStatus.ERROR]
)
def test_unavailable_provider_is_not_treated_as_clean(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine, status: ProviderStatus
) -> None:
    """A source that did not answer earns neither a bonus nor a penalty.

    This is the invariant that keeps "мы не проверили" from being reported as
    "банкротства нет".
    """
    report = build_report(person_subject, fedresurs=(status, []))
    score = score_engine.evaluate(report)
    assert score.score == BASE_SCORE
    assert not any(factor.name == "no_bankruptcy" for factor in score.factors)


def test_missing_providers_lower_confidence(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    answered = build_report(
        person_subject,
        fssp=(ProviderStatus.NO_RESULTS, []),
        fedresurs=(ProviderStatus.NO_RESULTS, []),
        fns=(ProviderStatus.NO_RESULTS, []),
    )
    unanswered = build_report(
        person_subject,
        fssp=(ProviderStatus.NOT_CONFIGURED, []),
        fedresurs=(ProviderStatus.NOT_CONFIGURED, []),
        fns=(ProviderStatus.NOT_CONFIGURED, []),
    )
    assert score_engine.evaluate(answered).confidence > score_engine.evaluate(unanswered).confidence


def test_confidence_notes_name_the_missing_sources(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    report = build_report(person_subject, fssp=(ProviderStatus.NOT_CONFIGURED, []))
    score = score_engine.evaluate(report)
    # Формулировка приходит из общей таблицы состояний источника, а не из
    # собственного набора слов в скоринге.
    assert any("ФССП: не подключено" in note for note in score.confidence_notes)


def test_search_without_birth_date_lowers_confidence(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    with_dob = build_report(person_subject, fssp=(ProviderStatus.NO_RESULTS, []))
    without_dob = build_report(
        person_subject.model_copy(update={"birth_date": None}),
        fssp=(ProviderStatus.NO_RESULTS, []),
    )
    assert (
        score_engine.evaluate(without_dob).confidence < score_engine.evaluate(with_dob).confidence
    )


def test_weak_matches_do_not_drive_the_score(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    weak = make_proceeding(confidence=0.2)
    report = build_report(person_subject, fssp=(ProviderStatus.SUCCESS, [weak]))
    score = score_engine.evaluate(report)
    # The source answered and nothing usable matched -> the "clean" bonus, not a
    # penalty derived from someone else's proceedings.
    assert any(factor.name == "no_enforcement" for factor in score.factors)


@pytest.mark.parametrize("count", [0, 1, 3, 8, 20])
def test_score_always_within_bounds(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine, count: int
) -> None:
    report = build_report(
        person_subject,
        fssp=(
            ProviderStatus.SUCCESS,
            [make_proceeding(f"{i}/26/77001-ИП", amount="9000000") for i in range(count)],
        ),
        fedresurs=(ProviderStatus.SUCCESS, [make_bankruptcy(active=True)]),
        fns=(ProviderStatus.SUCCESS, [make_business(active=False)]),
    )
    score = score_engine.evaluate(report)
    assert 0 <= score.score <= 100
    assert 0.0 <= score.confidence <= 1.0


def test_scoring_is_deterministic(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    def build() -> DebtorReport:
        return build_report(
            person_subject,
            fssp=(ProviderStatus.SUCCESS, [make_proceeding(amount="120000")]),
            fedresurs=(ProviderStatus.NO_RESULTS, []),
            fns=(ProviderStatus.SUCCESS, [make_business()]),
        )

    first = score_engine.evaluate(build())
    second = score_engine.evaluate(build())
    assert first.score == second.score
    assert first.factors == second.factors


def test_total_enforcement_amount_sums_only_usable_records(person_subject: SearchSubject) -> None:
    report = build_report(
        person_subject,
        fssp=(
            ProviderStatus.SUCCESS,
            [
                make_proceeding("1/26/77001-ИП", amount="100"),
                make_proceeding("2/26/77001-ИП", amount="900", confidence=0.1),
            ],
        ),
    )
    assert report.total_enforcement_amount == Decimal("100")


# ---------------------------------------------------------------- залоги


def test_active_pledge_lowers_the_score(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    """Заложенная машина — не наше обеспечение, а чужое."""
    pledged = build_report(person_subject, pledge=(ProviderStatus.SUCCESS, [make_pledge()]))
    clear = build_report(person_subject, pledge=(ProviderStatus.NO_RESULTS, []))

    assert score_engine.evaluate(pledged).score < score_engine.evaluate(clear).score
    assert any(factor.name == "active_pledge" for factor in score_engine.evaluate(pledged).factors)


def test_excluded_pledge_is_not_counted_against_the_debtor(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    report = build_report(
        person_subject, pledge=(ProviderStatus.SUCCESS, [make_pledge(active=False)])
    )
    score = score_engine.evaluate(report)

    assert not any(factor.name == "active_pledge" for factor in score.factors)
    assert any(factor.name == "no_pledges" for factor in score.factors)


def test_a_notice_of_unknown_state_cancels_the_no_pledge_bonus(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    """Уведомление, тип сообщения которого не прочитан, — не снятый залог.

    Тот же класс ошибки, что и у банкротства, и та же цена: плюс «действующих
    залогов не найдено» означает «мы посмотрели и ничего, что могло бы
    действовать, не увидели». Найденная запись с непрочитанным состоянием это
    утверждение опровергает — в отчёте она печатается как «состояние записи не
    определено», и превращать её в плюс нельзя. Штрафа при этом нет:
    домысливать «залог действует» тоже не на чем.
    """
    record = make_pledge()
    record.status = PledgeStatus.UNKNOWN
    report = build_report(person_subject, pledge=(ProviderStatus.SUCCESS, [record]))

    score = score_engine.evaluate(report)
    names = {factor.name for factor in score.factors}

    assert "no_pledges" not in names
    assert "active_pledge" not in names


def test_the_no_pledge_bonus_claims_only_what_was_read(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    """Плюс называет прочитанный реестр, а не всё имущество должника.

    Из двух веток ответа ``pledge_*`` карта полей читает одну — ФНП. Пока это
    так, «имущество не обременено» шире проверенного ровно на лизинг, иные
    обременения Федресурса и ипотеку, которой в ФНП нет вовсе.
    """
    report = build_report(person_subject, pledge=(ProviderStatus.NO_RESULTS, []))
    factor = next(f for f in score_engine.evaluate(report).factors if f.name == "no_pledges")

    assert "не обременено" not in factor.reason
    assert "ФНП" in factor.reason


def test_pledge_and_claim_counts_agree_in_russian(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    """«1 действующих залог» в отчёте, который читает юрист, — брак."""
    one = build_report(
        person_subject,
        pledge=(ProviderStatus.SUCCESS, [make_pledge()]),
        court=(ProviderStatus.SUCCESS, [make_court_case()]),
    )
    reasons = {factor.name: factor.reason for factor in score_engine.evaluate(one).factors}

    assert reasons["active_pledge"].startswith("1 действующий залог")
    assert reasons["claims_against_debtor"].startswith("1 действующий арбитражный иск")

    two = build_report(
        person_subject,
        pledge=(ProviderStatus.SUCCESS, [make_pledge(), make_pledge()]),
        court=(
            ProviderStatus.SUCCESS,
            [make_court_case("А40-1/2026"), make_court_case("А40-2/2026")],
        ),
    )
    reasons = {factor.name: factor.reason for factor in score_engine.evaluate(two).factors}

    assert reasons["active_pledge"].startswith("2 действующих залога")
    assert reasons["claims_against_debtor"].startswith("2 действующих арбитражных иска")


def test_pledge_penalty_is_capped(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    from app.domain.scoring import MAX_PLEDGE_PENALTY

    many = [make_pledge() for _ in range(10)]
    report = build_report(person_subject, pledge=(ProviderStatus.SUCCESS, many))
    factor = next(f for f in score_engine.evaluate(report).factors if f.name == "active_pledge")

    assert factor.delta == MAX_PLEDGE_PENALTY


@pytest.mark.parametrize(
    "status", [ProviderStatus.NOT_CONFIGURED, ProviderStatus.UNAVAILABLE, ProviderStatus.ERROR]
)
def test_unchecked_pledges_earn_no_bonus(
    person_subject: SearchSubject,
    score_engine: RecoveryScoreEngine,
    status: ProviderStatus,
) -> None:
    """Не проверено — не значит «не обременено»."""
    report = build_report(person_subject, pledge=(status, []))
    score = score_engine.evaluate(report)

    assert not any(factor.name == "no_pledges" for factor in score.factors)


# ---------------------------------------------------------------- арбитраж


def test_live_claim_against_the_debtor_lowers_the_score(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    sued = build_report(person_subject, court=(ProviderStatus.SUCCESS, [make_court_case()]))
    clear = build_report(person_subject, court=(ProviderStatus.NO_RESULTS, []))

    assert score_engine.evaluate(sued).score < score_engine.evaluate(clear).score
    assert any(
        factor.name == "claims_against_debtor" for factor in score_engine.evaluate(sued).factors
    )


def test_a_case_the_debtor_brought_is_not_a_claim_against_them(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    report = build_report(
        person_subject, court=(ProviderStatus.SUCCESS, [make_court_case(defendant=False)])
    )
    score = score_engine.evaluate(report)

    assert not any(factor.name == "claims_against_debtor" for factor in score.factors)


def test_a_decided_case_is_not_a_live_claim(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    report = build_report(
        person_subject, court=(ProviderStatus.SUCCESS, [make_court_case(closed=True)])
    )
    score = score_engine.evaluate(report)

    assert not any(factor.name == "claims_against_debtor" for factor in score.factors)


@pytest.mark.parametrize(
    "status", [ProviderStatus.NOT_CONFIGURED, ProviderStatus.UNAVAILABLE, ProviderStatus.ERROR]
)
def test_unchecked_courts_earn_no_bonus(
    person_subject: SearchSubject,
    score_engine: RecoveryScoreEngine,
    status: ProviderStatus,
) -> None:
    report = build_report(person_subject, court=(status, []))
    score = score_engine.evaluate(report)

    assert not any(factor.name == "no_court_claims" for factor in score.factors)


def test_unconnected_pledge_and_court_are_named_in_the_limitations(
    person_subject: SearchSubject, score_engine: RecoveryScoreEngine
) -> None:
    """Оценка без залогов и арбитража видит меньше — и говорит об этом."""
    report = build_report(
        person_subject,
        fssp=(ProviderStatus.NO_RESULTS, []),
        fedresurs=(ProviderStatus.NO_RESULTS, []),
        fns=(ProviderStatus.NO_RESULTS, []),
    )
    notes = score_engine.evaluate(report).confidence_notes

    assert any("Залоги" in note for note in notes)
    assert any("Суды" in note for note in notes)
