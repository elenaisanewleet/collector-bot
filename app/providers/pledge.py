"""Залоги — реестр уведомлений о залоге движимого имущества (ФНП).

Why this source earns a place in a recovery tool: **a pledged thing is not
collateral we can reach.** The pledgeholder is satisfied ahead of an ordinary
creditor, so finding the debtor's only car in the register turns an apparent
asset into somebody else's security — and finding *nothing* means whatever the
debtor owns is at least unencumbered.

Two NewDB methods feed it:

``pledge_person``  everything registered against the person
``pledge_vin``     one vehicle, by VIN — used when the operator searched by VIN
                   or when our own contract records one

Both are enabled by describing their rows in ``NEWDB_FIELD_MAP``. A method with
no entry there is not queried, and the report says the source was not checked.

Both answers carry two registries side by side: ``fnp`` — the pledge register —
and ``fedresurs`` — leasing contracts and other encumbrances. One map entry
describes one set of rows, so the shipped map reads the ФНП branch only. Hence
the wording of the report: "записей в реестре залогов не найдено", which is what
was actually checked, and not "имущество не обременено".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.enums import PledgeStatus, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import PledgeRecord, ProviderResult
from app.providers.mapping import as_text
from app.providers.newdb import COUNTRY_RU, NewDBMethodProvider, person_params_for
from app.utils.dates import parse_date, utcnow

PERSON_METHOD = "pledge_person"
VIN_METHOD = "pledge_vin"
MAX_RECORDS = 100

# Дата рождения зовётся здесь иначе, чем в ФССП. Документация pledge_person
# называет её ``datebirth`` во всех четырёх местах — во входной схеме, в примере
# запроса, в примере ответа и в x-ai-схеме, — тогда как fssp_person везде пишет
# ``dob``. На живом сервисе это не проверено (сайт документации мёртв), но из
# двух вариантов правдоподобнее тот, который написан на странице метода.
DATE_BIRTH_KEY = "datebirth"

_TERMINATED_TOKENS = frozenset({"terminated", "excluded", "исключ", "прекращ", "погашен", "снят"})
_ACTIVE_TOKENS = frozenset({"active", "действует", "действующее", "актуальн", "зарегистрирован"})


class NewDBPledgeProvider(NewDBMethodProvider):
    """Залоги движимого имущества через NewDB."""

    name = ProviderName.PLEDGE
    title = "Залоги"
    methods = (PERSON_METHOD, VIN_METHOD)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if not _searchable_by(subject):
            return self.insufficient_query(
                "Для проверки залогов нужен VIN либо ФИО с датой рождения"
            )
        plans = self._plans(subject)
        if not plans:
            # Искать есть по чему, но метод под это не описан. Это пробел в
            # настройке, а не в данных, и путать их нельзя.
            return self.not_configured("Метод NewDB под этот запрос не описан в NEWDB_FIELD_MAP")

        records: list[PledgeRecord] = []
        raw_bodies: list[str] = []
        for method, params in plans:
            rows, raw = await self.rows_for(method, params)
            raw_bodies.append(raw)
            records.extend(_to_pledge(row) for row in rows)

        unique = _dedupe(records)[:MAX_RECORDS]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if unique else ProviderStatus.NO_RESULTS,
            records=list(unique),
            raw_response=self.raw_for("\n".join(raw_bodies)),
        )

    def _plans(self, subject: SearchSubject) -> list[tuple[str, dict[str, Any]]]:
        """Which of the two methods this subject can actually be looked up by.

        A method the deployment has not described is skipped rather than
        guessed at.
        """
        mapped = set(self.mapped_methods)
        plans: list[tuple[str, dict[str, Any]]] = []

        vin = _subject_vin(subject)
        if vin and VIN_METHOD in mapped:
            plans.append((VIN_METHOD, {"country": COUNTRY_RU, "vin": vin}))

        if _has_identity(subject) and PERSON_METHOD in mapped:
            plans.append((PERSON_METHOD, person_params_for(subject, birth_date_key=DATE_BIRTH_KEY)))
        return plans


def _searchable_by(subject: SearchSubject) -> bool:
    return bool(_subject_vin(subject)) or _has_identity(subject)


def _has_identity(subject: SearchSubject) -> bool:
    return subject.name is not None and subject.birth_date is not None


def _subject_vin(subject: SearchSubject) -> str | None:
    return subject.vehicle.vin if subject.vehicle else None


def _to_pledge(record: Mapping[str, Any]) -> PledgeRecord:
    return PledgeRecord(
        registration_number=as_text(record.get("registration_number")),
        registered_at=parse_date(as_text(record.get("registered_at"))),
        terminated_at=parse_date(as_text(record.get("terminated_at"))),
        pledgor_name=as_text(record.get("pledgor_name")),
        pledgor_birth_date=parse_date(as_text(record.get("pledgor_birth_date"))),
        pledgor_inn=as_text(record.get("pledgor_inn")),
        pledgee_name=as_text(record.get("pledgee_name")),
        subject=as_text(record.get("subject")),
        vin=_normalized_vin(record.get("vin")),
        status=_parse_status(record),
        source_url=as_text(record.get("source_url")),
        fetched_at=utcnow(),
    )


def _normalized_vin(raw: Any) -> str | None:
    text = as_text(raw)
    return text.upper() if text else None


def _parse_status(record: Mapping[str, Any]) -> PledgeStatus:
    """A termination date is stronger evidence than a status string.

    ``UNKNOWN`` is deliberate rather than optimistic: an entry whose state we
    cannot read is not counted as an active pledge, and is not counted as a
    cleared one either.
    """
    if as_text(record.get("terminated_at")):
        return PledgeStatus.TERMINATED
    token = (as_text(record.get("status")) or "").lower()
    if any(marker in token for marker in _TERMINATED_TOKENS):
        return PledgeStatus.TERMINATED
    if any(marker in token for marker in _ACTIVE_TOKENS):
        return PledgeStatus.ACTIVE
    if as_text(record.get("registered_at")):
        # Registered, never excluded: the notice stands.
        return PledgeStatus.ACTIVE
    return PledgeStatus.UNKNOWN


def _dedupe(records: list[PledgeRecord]) -> list[PledgeRecord]:
    """The person search and the VIN search legitimately return the same notice."""
    seen: set[str] = set()
    unique: list[PledgeRecord] = []
    for record in records:
        key = (record.registration_number or record.vin or "").strip().lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(record)
    return unique


__all__ = ["PERSON_METHOD", "VIN_METHOD", "NewDBPledgeProvider"]
