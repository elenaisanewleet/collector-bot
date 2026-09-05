"""Залоги — реестр уведомлений о залоге движимого имущества (ФНП).

Why this source earns a place in a recovery tool: **a pledged thing is not
collateral we can reach.** The pledgeholder is satisfied ahead of an ordinary
creditor, so finding the debtor's only car in the register turns an apparent
asset into somebody else's security. Finding *nothing* is worth much less, and
is worded as such everywhere it surfaces: see the note on the two registries
below.

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
was actually checked, and not "имущество не обременено". The Федресурс branch is
paid for in every answer and read by nobody, so the report says so in as many
words instead of passing it off as checked.

There is a third branch, and it produced a live inversion. Alongside ``fnp`` the
answer carries ``fnp_urls`` — one link per notice. A captured response held
``"fnp": []`` and thirteen links: the vendor found thirteen notices and parsed
none of them. The map read ``fnp``, found an empty array — present, so not a
missing path — and the report told the operator the register was clean about a
debtor with thirteen registered pledge notices. Hence :func:`_unparsed_notices`:
links without notices are "не разобрано", never "чисто".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.domain.enums import PledgeStatus, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import PledgeRecord, ProviderResult
from app.providers.base import NO_CONTEXT, FetchContext, ProviderUnavailableError
from app.providers.mapping import as_text
from app.providers.newdb import COUNTRY_RU, NewDBMethodProvider, person_params_for
from app.utils.dates import parse_date, utcnow

PERSON_METHOD = "pledge_person"
VIN_METHOD = "pledge_vin"
MAX_RECORDS = 100

# Ветки одного контейнера, проверенные на живых ответах.
NOTICES_KEY = "fnp"
NOTICE_URLS_KEY = "fnp_urls"
FEDRESURS_KEY = "fedresurs"

FEDRESURS_NOTE = (
    "Сообщения Федресурса по залогу и лизингу в ответе есть, но не разбирались: "
    "финансовая аренда — тоже недоступный взыскателю актив."
)

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
        containers: list[Any] = []
        for method, params in plans:
            rows, method_containers, raw = await self.rows_and_containers(method, params)
            raw_bodies.append(raw)
            containers.extend(method_containers)
            records.extend(_to_pledge(row) for row in rows)

        unparsed = _unparsed_notices(containers)
        if unparsed:
            raise ProviderUnavailableError(
                "unexpected_schema",
                f"Реестр вернул {unparsed} ссылок на уведомления о залоге, "
                "но ни одного разобранного уведомления",
            )

        unique = _dedupe(records)[:MAX_RECORDS]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if unique else ProviderStatus.NO_RESULTS,
            records=list(unique),
            notes=(FEDRESURS_NOTE,) if _has_fedresurs_messages(containers) else (),
            raw_response=self.raw_for("\n".join(raw_bodies)),
        )

    def planned_calls(self, subject: SearchSubject, context: FetchContext = NO_CONTEXT) -> int:
        """One call per method this subject can actually be looked up by."""
        if not self.is_configured:
            return 0
        return len(self._plans(subject))

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


def _unparsed_notices(containers: list[Any]) -> int:
    """Notice links the answer carries without a single parsed notice.

    Checked against a live response where the two match one to one: one notice,
    one link. Links with no notices therefore mean the vendor found registrations
    and did not read them — the opposite of an empty register, and the only
    reading under which "залогов не найдено" would be a lie.
    """
    notices = 0
    links = 0
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        notices += _length_of(container.get(NOTICES_KEY))
        links += _length_of(container.get(NOTICE_URLS_KEY))
    return links if links and not notices else 0


def _has_fedresurs_messages(containers: list[Any]) -> bool:
    return any(
        isinstance(container, Mapping) and _length_of(container.get(FEDRESURS_KEY))
        for container in containers
    )


def _length_of(node: Any) -> int:
    return len(node) if isinstance(node, list) else 0


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
