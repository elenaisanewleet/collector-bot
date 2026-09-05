"""The provider contract.

One rule governs this layer: a provider never raises at its caller. Whatever
happens inside — a timeout, a 500, JSON that does not parse, a bug — comes back
as a :class:`ProviderResult` with an honest status. The aggregator can then
report "ФССП недоступна" instead of silently producing a clean report.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.domain.enums import ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import ProviderResult
from app.logging_setup import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class FetchContext:
    """Everything about *this* lookup that is not the subject.

    Two things live here, and both exist because they change what a source is
    allowed to cost:

    ``batch``     this is one debtor out of a run of eight hundred. Sources
                  priced per debtor are switched off here unless the deployment
                  turned them on knowingly.
    ``upstream``  results the first phase already produced, for the one source
                  whose input is another source's answer.
    """

    batch: bool = False
    upstream: tuple[ProviderResult, ...] = field(default_factory=tuple)

    def result_for(self, provider: ProviderName) -> ProviderResult | None:
        return next((item for item in self.upstream if item.provider is provider), None)


NO_CONTEXT = FetchContext()


class ProviderError(Exception):
    """Internal-to-the-provider failure, translated into a status at the boundary."""

    status: ProviderStatus = ProviderStatus.ERROR

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message or code


class ProviderUnavailableError(ProviderError):
    """The source could not be reached or refused to answer right now."""

    status = ProviderStatus.UNAVAILABLE


class ProviderNotConfiguredError(ProviderError):
    """Credentials or endpoints are missing. Not an error, and never an absence
    of records."""

    status = ProviderStatus.NOT_CONFIGURED

    def __init__(self, message: str = "provider is not configured") -> None:
        super().__init__("not_configured", message)


class BaseProvider(ABC):
    """Base class for every source of facts.

    Subclasses implement :meth:`_fetch`; :meth:`fetch` handles configuration
    checks, timing and exception isolation for all of them.
    """

    name: ProviderName
    title: str = ""
    # A chained source is fed by another source's answer rather than by the
    # subject, so it runs in a second phase. See ``SearchService._run_chained``.
    is_chained: bool = False

    @property
    @abstractmethod
    def is_configured(self) -> bool:
        """Whether this provider has everything it needs to make a real call."""

    @property
    def supports_subject(self) -> bool:
        """Overridden by providers that only handle certain search types."""
        return True

    def planned_calls(self, subject: SearchSubject, context: FetchContext = NO_CONTEXT) -> int:
        """How many paid calls this subject will cost, at minimum.

        The batch estimate is what the operator confirms before money moves, so
        it has to count *calls*, not providers: ФССП searches once per region,
        pledges search by person and by VIN, and a source with nothing to search
        by costs nothing at all.
        """
        return 1 if self.is_configured and self.supports_subject else 0

    def max_planned_calls(self, subject: SearchSubject, context: FetchContext = NO_CONTEXT) -> int:
        """The ceiling. Differs from :meth:`planned_calls` only for a chain,
        whose length is not known until the first source has answered."""
        return self.planned_calls(subject, context)

    @abstractmethod
    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        """Perform the lookup. May raise :class:`ProviderError`."""

    async def _dispatch(self, subject: SearchSubject, context: FetchContext) -> ProviderResult:
        """Route one fetch. Providers that read the context override this."""
        return await self._fetch(subject)

    async def fetch(
        self, subject: SearchSubject, context: FetchContext = NO_CONTEXT
    ) -> ProviderResult:
        started = time.perf_counter()
        if not self.is_configured:
            return self._result(
                ProviderStatus.NOT_CONFIGURED,
                started,
                error_code="not_configured",
                error_message="Источник не подключён",
            )
        try:
            result = await self._dispatch(subject, context)
        except ProviderError as exc:
            logger.warning(
                "provider.failed",
                provider=self.name.value,
                status=exc.status.value,
                error_code=exc.code,
            )
            return self._result(exc.status, started, error_code=exc.code, error_message=exc.message)
        except Exception as exc:
            # A provider bug must degrade one section of the report, not the run.
            logger.exception(
                "provider.unhandled",
                provider=self.name.value,
                error_type=type(exc).__name__,
            )
            return self._result(
                ProviderStatus.ERROR,
                started,
                error_code="unhandled_exception",
                error_message=type(exc).__name__,
            )
        if not result.duration_ms:
            result.duration_ms = _elapsed_ms(started)
        return result

    def _result(
        self,
        status: ProviderStatus,
        started: float,
        *,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            status=status,
            error_code=error_code,
            error_message=error_message,
            duration_ms=_elapsed_ms(started),
        )

    def not_configured(
        self, message: str = "Источник не подключён", *, notes: Sequence[str] = ()
    ) -> ProviderResult:
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.NOT_CONFIGURED,
            error_code="not_configured",
            error_message=message,
            notes=tuple(notes),
        )

    def insufficient_query(self, message: str) -> ProviderResult:
        """The source was not queried because the input lacked what it needs.

        Reported as an error rather than ``NO_RESULTS``: we did not look, so we
        must not imply there was nothing to find.
        """
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.ERROR,
            error_code="insufficient_query",
            error_message=message,
        )


class StubProvider(BaseProvider):
    """A source whose shape is known but for which no lawful, stable integration
    exists yet.

    It always answers ``NOT_CONFIGURED``. That is the honest answer, and it keeps
    the report's source list complete instead of pretending the source is not
    part of the picture.
    """

    def __init__(self, name: ProviderName, title: str, note: str) -> None:
        self.name = name
        self.title = title
        self.note = note

    @property
    def is_configured(self) -> bool:
        return False

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:  # pragma: no cover
        raise ProviderNotConfiguredError(self.note)


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
