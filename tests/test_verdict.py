"""Правила вердикта.

Порядок правил — это и есть содержание движка, поэтому тесты проверяют не
только каждое правило по отдельности, но и что более сильное правило
перекрывает более слабое.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

import pytest

from app.config import Settings
from app.domain.enums import ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import DebtorReport, InternalDebtorRecord
from app.domain.verdict import FeeBasis, Verdict
from app.services.scoring import RecoveryScoreEngine
from app.services.verdict import VerdictEngine
from tests.conftest import make_bankruptcy, make_internal, make_proceeding, provider_result


@pytest.fixture
def engine(settings: Settings) -> VerdictEngine:
    return VerdictEngine(settings)


def build_report(
    subject: SearchSubject,
    *,
    internal: InternalDebtorRecord | None = None,
    fssp: tuple[ProviderStatus, Sequence[Any]] | None = (ProviderStatus.NO_RESULTS, []),
    fedresurs: tuple[ProviderStatus, Sequence[Any]] | None = (ProviderStatus.NO_RESULTS, []),
    fns: tuple[ProviderStatus, Sequence[Any]] | None = (ProviderStatus.NO_RESULTS, []),
) -> DebtorReport:
    """Отчёт с уже опрошенными источниками — как после реального поиска."""
    record = internal if internal is not None else make_internal()
    report = DebtorReport(subject=subject, internal_records=[record])
    buckets: list[tuple[ProviderName, tuple[ProviderStatus, Sequence[Any]] | None, list[Any]]] = [
        (ProviderName.FSSP, fssp, report.enforcement_proceedings),
        (ProviderName.FEDRESURS, fedresurs, report.bankruptcies),
        (ProviderName.FNS, fns, report.business_relations),
    ]
    for provider, spec, bucket in buckets:
        if spec is None:
            continue
        status, records = spec
        report.provider_results.append(provider_result(provider, status, records))
        bucket.extend(records)
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return report


# ---------------------------------------------------------------- blocking


def test_active_bankruptcy_stops_the_claim(
    engine: VerdictEngine, person_subject: SearchSubject
) -> None:
    report = build_report(
        person_subject, fedresurs=(ProviderStatus.SUCCESS, [make_bankruptcy(active=True)])
    )
    decision = engine.decide(report)

    assert decision.verdict is Verdict.DROP
    assert "реестр кредиторов" in decision.headline
    assert decision.fee_basis is FeeBasis.NONE


def test_bankruptcy_still_reports_the_avoided_fee(
    engine: VerdictEngine, person_subject: SearchSubject
) -> None:
    """Смысл отсева — показать, сколько не придётся платить."""
    report = build_report(
        person_subject,
        internal=make_internal(debt="612750.50"),
        fedresurs=(ProviderStatus.SUCCESS, [make_bankruptcy(active=True)]),
    )
    decision = engine.decide(report)

    assert decision.state_fee == Decimal("17255")
    assert decision.fee_basis is FeeBasis.NONE


def test_bankruptcy_outranks_a_healthy_score(
    engine: VerdictEngine, person_subject: SearchSubject
) -> None:
    """Даже при хорошем score банкротство закрывает вопрос."""
    from tests.conftest import make_business

    report = build_report(
        person_subject,
        fedresurs=(ProviderStatus.SUCCESS, [make_bankruptcy(active=True)]),
        fns=(ProviderStatus.SUCCESS, [make_business(active=True)]),
    )
    assert engine.decide(report).verdict is Verdict.DROP


# ---------------------------------------------------------------- data gaps


def test_missing_birth_date_goes_to_review_not_drop(
    engine: VerdictEngine, person_subject: SearchSubject
) -> None:
    """Мы не знаем — это «проверить», а не «не подавать»."""
    subject = person_subject.model_copy(update={"birth_date": None})
    decision = engine.decide(build_report(subject))

    assert decision.verdict is Verdict.REVIEW
    assert "подтвердить нельзя" in decision.headline


def test_silent_source_goes_to_review(engine: VerdictEngine, person_subject: SearchSubject) -> None:
    report = build_report(person_subject, fssp=(ProviderStatus.NOT_CONFIGURED, []))
    decision = engine.decide(report)

    assert decision.verdict is Verdict.REVIEW
    assert any(reason.source is ProviderName.FSSP for reason in decision.reasons)


def test_unavailable_source_goes_to_review(
    engine: VerdictEngine, person_subject: SearchSubject
) -> None:
    report = build_report(person_subject, fedresurs=(ProviderStatus.UNAVAILABLE, []))
    assert engine.decide(report).verdict is Verdict.REVIEW


def test_missing_debt_amount_goes_to_review(
    engine: VerdictEngine, person_subject: SearchSubject
) -> None:
    report = build_report(person_subject, internal=make_internal(debt=None))
    decision = engine.decide(report)

    assert decision.verdict is Verdict.REVIEW
    assert "суммы долга" in decision.headline


# ---------------------------------------------------------------- economics


def test_fee_outweighing_the_debt_is_dropped(
    engine: VerdictEngine, person_subject: SearchSubject
) -> None:
    """Пошлина 2 000 ₽ против долга 3 000 ₽ — процесс не окупается."""
    report = build_report(person_subject, internal=make_internal(debt="3000"))
    decision = engine.decide(report)

    assert decision.verdict is Verdict.DROP
    assert "несоразмерна" in decision.headline


def test_debt_just_above_the_threshold_is_kept(
    engine: VerdictEngine, person_subject: SearchSubject
) -> None:
    report = build_report(person_subject, internal=make_internal(debt="5000"))
    assert engine.decide(report).verdict is Verdict.ORDER


def test_low_prospects_go_to_a_human(engine: VerdictEngine, person_subject: SearchSubject) -> None:
    heavy = [make_proceeding(f"{i}/26/77001-ИП", amount="400000") for i in range(6)]
    report = build_report(person_subject, fssp=(ProviderStatus.SUCCESS, heavy))
    decision = engine.decide(report)

    assert decision.verdict is Verdict.REVIEW
    assert "Перспектива низкая" in decision.headline
    assert decision.reasons


# ---------------------------------------------------------------- procedure


def test_small_debt_becomes_a_court_order(
    engine: VerdictEngine, person_subject: SearchSubject
) -> None:
    report = build_report(person_subject, internal=make_internal(debt="38400"))
    decision = engine.decide(report)

    assert decision.verdict is Verdict.ORDER
    assert decision.fee_basis is FeeBasis.COURT_ORDER
    assert decision.state_fee == Decimal("2000")


def test_large_debt_becomes_a_claim(engine: VerdictEngine, person_subject: SearchSubject) -> None:
    report = build_report(person_subject, internal=make_internal(debt="900000"))
    decision = engine.decide(report)

    assert decision.verdict is Verdict.FILE
    assert decision.fee_basis is FeeBasis.CLAIM
    assert decision.state_fee == Decimal("23000")


def test_threshold_edge_stays_an_order(
    engine: VerdictEngine, person_subject: SearchSubject, settings: Settings
) -> None:
    at = build_report(person_subject, internal=make_internal(debt="500000"))
    above = build_report(person_subject, internal=make_internal(debt="500001"))

    assert engine.decide(at).verdict is Verdict.ORDER
    assert engine.decide(above).verdict is Verdict.FILE


def test_actionable_verdicts(engine: VerdictEngine, person_subject: SearchSubject) -> None:
    order = engine.decide(build_report(person_subject, internal=make_internal(debt="38400")))
    dropped = engine.decide(
        build_report(
            person_subject, fedresurs=(ProviderStatus.SUCCESS, [make_bankruptcy(active=True)])
        )
    )
    assert order.is_actionable
    assert not dropped.is_actionable


def test_decision_carries_confidence(engine: VerdictEngine, person_subject: SearchSubject) -> None:
    decision = engine.decide(build_report(person_subject))
    assert 0.0 <= decision.confidence <= 1.0


def test_verdict_is_deterministic(engine: VerdictEngine, person_subject: SearchSubject) -> None:
    first = engine.decide(build_report(person_subject))
    second = engine.decide(build_report(person_subject))
    assert first.verdict is second.verdict
    assert first.headline == second.headline
