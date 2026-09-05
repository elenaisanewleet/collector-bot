"""Суды — арбитражные дела через метод NewDB ``arbitr_person``.

What this source answers is narrower than "суды" suggests, and the narrowness
matters: it covers **arbitration** (kad.arbitr.ru), which for a private
individual means cases they are party to as a sole proprietor. Courts of general
jurisdiction — where most consumer debt is litigated — are not in it. So an
empty answer here is "нет арбитражных дел", never "в суд на него никто не
подавал", and the report is worded accordingly.

Why it is worth the call anyway: a live claim against the debtor is a creditor
already ahead of us in the queue, and one about to convert into an enforcement
proceeding that competes with ours.

The method identifies a person by ИНН. Without one this provider reports
"недостаточно данных" instead of searching by name — an arbitration case
attached to the wrong person is a wrong reason to drop a debtor.

The answer is a wrapper per subject, not a row per case: ``data[0]`` carries
``total_count``, ``message``, ``pagination`` and two arrays — ``cases`` (the
short list) and ``detailed_cases`` (the same cases with their card parsed). The
map reads the second. The wrapper itself is read here, because two things in it
decide whether an empty result may be reported as one: a non-empty ``cases``
beside an empty ``detailed_cases`` means the vendor found cases and parsed none,
and ``pagination.limit`` is 10, so a debtor with forty cases has thirty nobody
looked at.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.enums import CourtCaseRole, ProviderName, ProviderStatus
from app.domain.identity import PersonName, SearchSubject
from app.domain.models import CourtCase, ProviderResult
from app.providers.base import ProviderUnavailableError
from app.providers.mapping import as_text, dig
from app.providers.newdb import NewDBMethodProvider, individual_inn, inn_params
from app.utils.dates import parse_date, utcnow
from app.utils.hashing import normalize_token
from app.utils.money import parse_amount

NEWDB_METHOD = "arbitr_person"
MAX_RECORDS = 50

# Ветки обёртки, проверенные на живом ответе.
CASES_KEY = "cases"
DETAILED_CASES_KEY = "detailed_cases"

_DEFENDANT_TOKENS = frozenset({"ответчик", "defendant", "должник"})
_PLAINTIFF_TOKENS = frozenset({"истец", "plaintiff", "заявитель", "взыскатель"})
# Substring markers, and the difference between two of them is the whole point:
# «рассмотрение по существу» is a case being heard right now, «рассмотрено» is
# one that is over. Matching the shared prefix would mark every live claim
# closed and quietly drop the strongest reason to hurry.
#
# This vocabulary is a heuristic over text we do not control. A deployment that
# knows its own status values normalizes them in the field map's ``value_maps``
# for ``status`` — mapping them onto ``closed`` — instead of relying on it.
_CLOSED_TOKENS = frozenset(
    {
        "рассмотрено",
        "завершено",
        "завершён",
        "прекращено",
        "прекращён",
        "closed",
        "completed",
        "decided",
    }
)


class NewDBArbitrationProvider(NewDBMethodProvider):
    """Арбитражные дела физлица / ИП."""

    name = ProviderName.COURT
    title = "Суды"
    methods = (NEWDB_METHOD,)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        inn = individual_inn(subject)
        if inn is None:
            return self.insufficient_query(
                "Для проверки арбитража нужен ИНН физлица (12 цифр) — "
                "поиск по одному ФИО дал бы чужие дела"
            )

        rows, containers, raw = await self.rows_and_containers(NEWDB_METHOD, inn_params(inn))
        parsed = [
            case
            for row in rows[:MAX_RECORDS]
            if (case := _to_case(row, subject=subject, searched_inn=inn)) is not None
        ]
        if not parsed and _found_but_unparsed(containers):
            raise ProviderUnavailableError(
                "unexpected_schema",
                "КАД вернул дела, но ни одно из них не разобрано — "
                "пустой результат здесь означал бы «дел нет»",
            )
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if parsed else ProviderStatus.NO_RESULTS,
            records=list(parsed),
            notes=_coverage_notes(containers, parsed=len(parsed)),
            raw_response=self.raw_for(raw),
        )

    def planned_calls(self, subject: SearchSubject) -> int:
        if not self.is_configured or individual_inn(subject) is None:
            return 0
        return 1


def _found_but_unparsed(containers: Sequence[Any]) -> bool:
    """The wrapper lists cases and the detailed array is empty."""
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        listed = container.get(CASES_KEY)
        detailed = container.get(DETAILED_CASES_KEY)
        if isinstance(listed, list) and listed and not (detailed or []):
            return True
    return False


def _coverage_notes(containers: Sequence[Any], *, parsed: int) -> tuple[str, ...]:
    """«Разобрано N из M» — из полей самой обёртки.

    Без этой строки усечённая страница выглядит как полный ответ: источник
    отдаёт по десять дел за раз, и «дел больше нет» после десятого — это не то,
    что он сказал.
    """
    notes: list[str] = []
    for container in containers:
        if not isinstance(container, Mapping):
            continue
        total = container.get("total_count")
        has_more = dig(container, "pagination.has_more")
        if isinstance(total, int) and total > parsed:
            notes.append(
                f"Источник нашёл дел: {total}, разобрано {parsed}. "
                "По остальным сведений нет."
            )
        elif has_more is True:
            notes.append("Источник отдал не все дела: следующая страница не запрашивалась.")
    return tuple(dict.fromkeys(notes))


def _to_case(
    record: Mapping[str, Any], *, subject: SearchSubject, searched_inn: str
) -> CourtCase | None:
    """A row with no case number is not a case we can show or verify.

    ``searched_inn`` fills the identity the row itself does not carry: the case
    was returned for that ИНН, so the ИНН belongs on the record. Without it every
    case comes back with no identifiers, the matcher rates it weak, and the
    report drops a live claim as somebody else's business.
    """
    case_number = as_text(record.get("case_number"))
    if case_number is None:
        return None
    return CourtCase(
        case_number=case_number,
        court_name=as_text(record.get("court_name")),
        case_type=as_text(record.get("case_type")),
        status=as_text(record.get("status")),
        amount=parse_amount(as_text(record.get("amount"))),
        filed_at=parse_date(as_text(record.get("filed_at"))),
        participant_name=as_text(record.get("participant_name")),
        inn=as_text(record.get("inn")) or searched_inn,
        role=_role_of(record, subject),
        is_closed=_is_closed(record),
        source_url=as_text(record.get("source_url")),
        fetched_at=utcnow(),
    )


def _role_of(record: Mapping[str, Any], subject: SearchSubject) -> CourtCaseRole:
    """Роль по делу — из плоского поля, а если его нет, из списков участников.

    У КАД плоского поля роли нет: истцы и ответчики приходят отдельными списками
    (``participants.plaintiffs`` / ``participants.defendants``), и роль
    определяется тем, в каком из них нашлось ФИО субъекта. Карта полей плоская и
    так не умеет, поэтому сопоставление делает код, а карта лишь показывает ему
    оба списка.
    """
    role = _parse_role(record.get("role"))
    if role is not CourtCaseRole.OTHER:
        return role
    if subject.name is None:
        return CourtCaseRole.OTHER
    # Должник-банкрот стоит в обоих списках сразу — он и заявитель, и лицо, к
    # которому предъявлены требования. Для взыскания весомее второе.
    if _names_include(record.get("participants_defendants"), subject.name):
        return CourtCaseRole.DEFENDANT
    if _names_include(record.get("participants_plaintiffs"), subject.name):
        return CourtCaseRole.PLAINTIFF
    return CourtCaseRole.OTHER


def _names_include(participants: Any, name: PersonName) -> bool:
    if not isinstance(participants, Sequence) or isinstance(participants, (str, bytes)):
        return False
    for item in participants:
        raw = item.get("name") if isinstance(item, Mapping) else item
        token = normalize_token(as_text(raw) or "")
        if token and token in {name.normalized, name.normalized_short}:
            return True
    return False


def _parse_role(raw: Any) -> CourtCaseRole:
    token = (as_text(raw) or "").lower()
    if any(marker in token for marker in _DEFENDANT_TOKENS):
        return CourtCaseRole.DEFENDANT
    if any(marker in token for marker in _PLAINTIFF_TOKENS):
        return CourtCaseRole.PLAINTIFF
    return CourtCaseRole.OTHER


def _is_closed(record: Mapping[str, Any]) -> bool:
    if as_text(record.get("closed_at")):
        return True
    token = (as_text(record.get("status")) or "").lower()
    return any(marker in token for marker in _CLOSED_TOKENS)


__all__ = ["NEWDB_METHOD", "NewDBArbitrationProvider"]
