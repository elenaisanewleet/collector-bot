"""Folding provider results into one report.

The aggregator is deliberately dumb about failure: it sorts records by type and
records every provider's status verbatim. A provider that errored contributes an
entry saying so, which is what lets the report distinguish "checked, clean" from
"never checked".
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.identity import SearchSubject
from app.domain.models import (
    BankruptcyRecord,
    BusinessRelation,
    CourtCase,
    DebtorReport,
    EnforcementProceeding,
    InternalDebtorRecord,
    PropertyRecord,
    ProviderResult,
    SourcedFact,
    VehicleRecord,
)
from app.services.identity import IdentityMatcher


class Aggregator:
    """Builds a :class:`DebtorReport` from provider results."""

    def __init__(self, matcher: IdentityMatcher | None = None) -> None:
        self._matcher = matcher or IdentityMatcher()

    def build(
        self,
        subject: SearchSubject,
        results: Sequence[ProviderResult],
        *,
        internal_records: Sequence[InternalDebtorRecord] = (),
    ) -> DebtorReport:
        report = DebtorReport(subject=subject, internal_records=list(internal_records))

        for result in results:
            report.provider_results.append(result)
            # Records are annotated per provider so a matcher failure on one
            # source cannot corrupt another's records.
            self._matcher.annotate(subject, list(result.records))
            for record in result.records:
                _dispatch(report, record)

        _sort_report(report)
        return report


def _dispatch(report: DebtorReport, record: SourcedFact) -> None:
    """Route a fact into the right section of the report."""
    if isinstance(record, EnforcementProceeding):
        report.enforcement_proceedings.append(record)
    elif isinstance(record, BankruptcyRecord):
        report.bankruptcies.append(record)
    elif isinstance(record, BusinessRelation):
        report.business_relations.append(record)
    elif isinstance(record, CourtCase):
        report.court_cases.append(record)
    elif isinstance(record, VehicleRecord):
        report.vehicles.append(record)
    elif isinstance(record, PropertyRecord):
        report.properties.append(record)
    elif isinstance(record, InternalDebtorRecord):
        report.internal_records.append(record)


def _sort_report(report: DebtorReport) -> None:
    """Most-confident and most-consequential items first."""
    report.enforcement_proceedings.sort(
        key=lambda item: (
            -item.match_confidence,
            not item.is_active,
            -(item.amount or 0),
        )
    )
    report.bankruptcies.sort(key=lambda item: (-item.match_confidence, not item.is_active))
    report.business_relations.sort(key=lambda item: (-item.match_confidence, not item.is_active))
    report.internal_records.sort(key=lambda item: -item.match_confidence)
