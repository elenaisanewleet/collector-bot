"""The internal (customer-owned) data source interface.

``SearchService`` talks only to this interface. Whether the records live in a
CSV export, in PostgreSQL, or eventually in the customer's 1С instance is
invisible above this line — which is the whole point of having it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from app.domain.models import InternalDebtorRecord


class InternalDebtorProvider(ABC):
    """Lookup by each identifier the business actually has to hand.

    Implementations return every plausible candidate; deciding which candidates
    are the same person is the :class:`~app.services.identity.IdentityMatcher`'s
    job, not the storage layer's.
    """

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
