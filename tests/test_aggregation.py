"""Aggregation and report rendering.

The central case: one provider failing must not remove another provider's data
from the report, and the report must say which source failed.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from app.domain.enums import ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import ProviderResult
from app.services.aggregation import Aggregator
from app.services.reporting import render_report
from app.services.scoring import RecoveryScoreEngine
from tests.conftest import (
    make_bankruptcy,
    make_business,
    make_court_case,
    make_pledge,
    make_proceeding,
    provider_result,
)


@pytest.fixture
def aggregator() -> Aggregator:
    return Aggregator()


def test_fns_failure_does_not_destroy_fssp_results(
    aggregator: Aggregator, person_subject: SearchSubject
) -> None:
    results = [
        provider_result(
            ProviderName.FSSP, ProviderStatus.SUCCESS, [make_proceeding(confidence=0.0)]
        ),
        provider_result(ProviderName.FNS, ProviderStatus.UNAVAILABLE),
        provider_result(ProviderName.FEDRESURS, ProviderStatus.NO_RESULTS),
    ]
    report = aggregator.build(person_subject, results)

    assert len(report.enforcement_proceedings) == 1
    assert report.result_for(ProviderName.FNS) is not None
    assert report.result_for(ProviderName.FNS).is_failure  # type: ignore[union-attr]
    assert report.result_for(ProviderName.FSSP).status is ProviderStatus.SUCCESS  # type: ignore[union-attr]


def test_records_are_routed_to_the_right_sections(
    aggregator: Aggregator, person_subject: SearchSubject
) -> None:
    results = [
        provider_result(
            ProviderName.FSSP, ProviderStatus.SUCCESS, [make_proceeding(confidence=0.0)]
        ),
        provider_result(
            ProviderName.FEDRESURS, ProviderStatus.SUCCESS, [make_bankruptcy(confidence=0.0)]
        ),
        provider_result(ProviderName.FNS, ProviderStatus.SUCCESS, [make_business(confidence=0.0)]),
        provider_result(ProviderName.PLEDGE, ProviderStatus.SUCCESS, [make_pledge(confidence=0.0)]),
        provider_result(
            ProviderName.COURT, ProviderStatus.SUCCESS, [make_court_case(confidence=0.0)]
        ),
    ]
    report = aggregator.build(person_subject, results)

    assert len(report.enforcement_proceedings) == 1
    assert len(report.bankruptcies) == 1
    assert len(report.business_relations) == 1
    assert len(report.pledges) == 1
    assert len(report.court_cases) == 1


def test_aggregator_annotates_match_confidence(
    aggregator: Aggregator, person_subject: SearchSubject
) -> None:
    record = make_proceeding(confidence=0.0)
    aggregator.build(
        person_subject,
        [provider_result(ProviderName.FSSP, ProviderStatus.SUCCESS, [record])],
    )
    assert record.match_confidence > 0.0
    assert record.match_reasons


def test_records_are_sorted_by_confidence(
    aggregator: Aggregator, person_subject: SearchSubject
) -> None:
    from datetime import date

    strong = make_proceeding("1/26/77001-ИП", confidence=0.0)
    weak = make_proceeding("2/26/77001-ИП", birth_date=date(1960, 1, 1), confidence=0.0)
    report = aggregator.build(
        person_subject,
        [provider_result(ProviderName.FSSP, ProviderStatus.SUCCESS, [weak, strong])],
    )
    assert report.enforcement_proceedings[0].proceeding_number == "1/26/77001-ИП"


# ---------------------------------------------------------------- rendering


def render_for(person_subject: SearchSubject, results: Sequence[ProviderResult]) -> str:
    aggregator = Aggregator()
    report = aggregator.build(person_subject, results)
    report.recovery_score = RecoveryScoreEngine().evaluate(report)
    return render_report(report)


def test_unconfigured_source_is_never_rendered_as_clean(
    person_subject: SearchSubject,
) -> None:
    """The report must not say "банкротство не обнаружено" for a source that
    was never queried."""
    text = render_for(
        person_subject,
        [provider_result(ProviderName.FEDRESURS, ProviderStatus.NOT_CONFIGURED)],
    )
    assert "Не проверено: источник не подключён." in text
    assert "Не обнаружено" not in text


def test_checked_and_empty_source_is_rendered_as_clean(
    person_subject: SearchSubject,
) -> None:
    text = render_for(
        person_subject,
        [provider_result(ProviderName.FEDRESURS, ProviderStatus.NO_RESULTS)],
    )
    assert "Не обнаружено" in text


def test_unavailable_source_is_rendered_as_unavailable(
    person_subject: SearchSubject,
) -> None:
    text = render_for(
        person_subject, [provider_result(ProviderName.FSSP, ProviderStatus.UNAVAILABLE)]
    )
    assert "временно недоступен" in text


def test_report_never_issues_legal_instructions(person_subject: SearchSubject) -> None:
    text = render_for(
        person_subject,
        [
            provider_result(
                ProviderName.FSSP, ProviderStatus.SUCCESS, [make_proceeding(confidence=0.0)]
            )
        ],
    )
    assert "не заменяет юридическую проверку" in text
    for forbidden in ("подавайте в суд", "обязательно подавайте", "нужно подать иск"):
        assert forbidden not in text.lower()


def test_report_includes_score_explanation_and_confidence(
    person_subject: SearchSubject,
) -> None:
    text = render_for(
        person_subject,
        [
            provider_result(ProviderName.FEDRESURS, ProviderStatus.NO_RESULTS),
            provider_result(
                ProviderName.FNS, ProviderStatus.SUCCESS, [make_business(confidence=0.0)]
            ),
        ],
    )
    assert "RECOVERY SCORE" in text
    assert "Уверенность данных:" in text
    assert "Положительные факторы:" in text
    assert "банкротство не обнаружено" in text


def test_report_lists_every_source(person_subject: SearchSubject) -> None:
    text = render_for(
        person_subject,
        [
            provider_result(ProviderName.FSSP, ProviderStatus.NO_RESULTS),
            provider_result(ProviderName.COURT, ProviderStatus.NOT_CONFIGURED),
        ],
    )
    assert "ИСТОЧНИКИ" in text
    assert "○ Суды — не подключено" in text


def test_long_report_splits_into_deliverable_chunks(
    person_subject: SearchSubject,
) -> None:
    from app.utils.formatting import TELEGRAM_MESSAGE_LIMIT, split_message

    proceedings = [make_proceeding(f"{index}/26/77001-ИП", confidence=0.0) for index in range(60)]
    text = render_for(
        person_subject,
        [provider_result(ProviderName.FSSP, ProviderStatus.SUCCESS, proceedings)],
    )
    chunks = split_message(text)
    assert all(len(chunk) <= TELEGRAM_MESSAGE_LIMIT for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_unconnected_pledges_are_not_rendered_as_unencumbered(
    person_subject: SearchSubject,
) -> None:
    text = render_for(
        person_subject,
        [provider_result(ProviderName.PLEDGE, ProviderStatus.NOT_CONFIGURED)],
    )
    assert "ЗАЛОГИ" in text
    assert "Не проверено: источник не подключён." in text
    assert "Записей в реестре залогов не найдено" not in text


def test_pledge_is_rendered_with_its_holder(person_subject: SearchSubject) -> None:
    text = render_for(
        person_subject,
        [provider_result(ProviderName.PLEDGE, ProviderStatus.SUCCESS, [make_pledge()])],
    )
    assert "Автомобиль LADA VESTA, 2021" in text
    assert "действует" in text
    assert "Залогодержатель:" in text


def test_court_block_says_what_it_does_not_cover(person_subject: SearchSubject) -> None:
    """«Дел не найдено» без оговорки прочиталось бы как «в суд на него не подавали»."""
    text = render_for(
        person_subject,
        [provider_result(ProviderName.COURT, ProviderStatus.NO_RESULTS)],
    )
    assert "Арбитражных дел не найдено." in text
    assert "Суды общей юрисдикции этот источник не покрывает." in text


def test_court_case_is_rendered_with_role_and_state(person_subject: SearchSubject) -> None:
    text = render_for(
        person_subject,
        [provider_result(ProviderName.COURT, ProviderStatus.SUCCESS, [make_court_case()])],
    )
    assert "А40-227414/2026" in text
    assert "ответчик" in text
    assert "идёт" in text
