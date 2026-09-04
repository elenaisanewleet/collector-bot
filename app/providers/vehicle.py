"""Vehicle lookups.

There is no lawful, stable API available to this project for resolving a licence
plate or VIN to an owner, so no such integration exists here. The interface and
the normalized model are defined now so that adding a legitimate provider later
is a registration, not a redesign.

Plate and VIN *validation* is real and useful today: it catches typos before a
search is run, and it powers matching against our own records, where the plate
and VIN came from our own contracts.
"""

from __future__ import annotations

from abc import abstractmethod

from app.domain.enums import ProviderName
from app.domain.identity import SearchSubject
from app.domain.models import ProviderResult
from app.providers.base import BaseProvider


class VehicleProvider(BaseProvider):
    """Contract for any future vehicle data source."""

    name = ProviderName.VEHICLE
    title = "Авто"

    @abstractmethod
    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        """Look up a vehicle by plate or VIN."""


class UnconfiguredVehicleProvider(VehicleProvider):
    """The only vehicle provider shipped today.

    Always ``NOT_CONFIGURED`` — which is the truthful answer, as opposed to
    ``NO_RESULTS``, which would imply we checked.
    """

    @property
    def is_configured(self) -> bool:
        return False

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:  # pragma: no cover
        return self.not_configured("Проверка по госномеру/VIN пока не подключена")
