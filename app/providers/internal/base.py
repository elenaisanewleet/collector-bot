"""The internal (customer-owned) data source interface.

``SearchService`` talks only to this interface. Whether the records live in a
CSV export, in PostgreSQL, or eventually in the customer's 1С instance is
invisible above this line — which is the whole point of having it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from app.domain.enums import ProviderStatus
from app.domain.models import InternalDebtorRecord


@dataclass(frozen=True, slots=True)
class InternalSourceFailure:
    """One internal source that did not answer, named.

    Without this the internal contour has no status channel at all: a 1С that
    timed out and a database with no such debtor both arrive as an empty list,
    and the report prints the same "совпадений не найдено" for both.
    """

    source: str
    status: ProviderStatus
    error_code: str
    error_message: str


class InternalRecords(list[InternalDebtorRecord]):
    """The records, plus the sources that never got as far as producing any.

    A subclass of ``list`` on purpose: every ``find_by_*`` signature stays
    ``list[InternalDebtorRecord]``, ``if not records`` and ``records.extend()``
    keep working, and the CSV and database providers need no changes at all.
    """

    __slots__ = ("failures",)

    def __init__(
        self,
        records: Iterable[InternalDebtorRecord] = (),
        *,
        failures: Iterable[InternalSourceFailure] = (),
    ) -> None:
        super().__init__(records)
        self.failures: tuple[InternalSourceFailure, ...] = tuple(failures)


class InternalDebtorProvider(ABC):
    """Lookup by each identifier the business actually has to hand.

    Implementations return every plausible candidate; deciding which candidates
    are the same person is the :class:`~app.services.identity.IdentityMatcher`'s
    job, not the storage layer's.
    """

    # Shown to the operator when this source is the one that failed. "1С —
    # недоступно" is actionable; "источник недоступен" is not.
    source_label: str = "внутренний источник"

    @abstractmethod
    async def find_by_fio(
        self, fio: str, *, birth_date: date | None = None
    ) -> list[InternalDebtorRecord]:
        """Find by full name, optionally narrowed by date of birth."""

    @abstractmethod
    async def find_by_phone(self, phone: str) -> list[InternalDebtorRecord]:
        """Find by normalized phone number (``+7XXXXXXXXXX``)."""

    @abstractmethod
    async def find_by_contract(self, contract_number: str) -> list[InternalDebtorRecord]:
        """Find by contract number."""

    @abstractmethod
    async def find_by_claim(self, claim_number: str) -> list[InternalDebtorRecord]:
        """Find by claim / application number."""

    @abstractmethod
    async def find_by_debtor_id(self, debtor_id: str) -> list[InternalDebtorRecord]:
        """Find by the identifier used in the customer's own system."""

    async def find_by_plate(self, plate: str) -> list[InternalDebtorRecord]:
        """Find by licence plate. Optional — defaults to no results."""
        return []

    async def find_by_vin(self, vin: str) -> list[InternalDebtorRecord]:
        """Find by VIN. Optional — defaults to no results."""
        return []

    async def find_by_address(self, address: str) -> list[InternalDebtorRecord]:
        """Find by address substring. Optional — defaults to no results."""
        return []


__all__ = [
    "InternalDebtorProvider",
    "InternalRecords",
    "InternalSourceFailure",
]
