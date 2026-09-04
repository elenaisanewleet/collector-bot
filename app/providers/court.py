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
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.enums import CourtCaseRole, ProviderName, ProviderStatus
from app.domain.identity import NameMatch, PersonName, SearchSubject, compare_names
from app.domain.models import CourtCase, ProviderResult
from app.providers.mapping import as_text, dig
from app.providers.newdb import NewDBMethodProvider, individual_inn, inn_params
from app.utils.dates import parse_date, utcnow
from app.utils.money import parse_amount

NEWDB_METHOD = "arbitr_person"
MAX_RECORDS = 50

# Участник дела приходит объектом, а не строкой, и ключ с именем внутри него —
# такая же часть контракта вендора, как путь до самого списка. Путь живёт в
# карте полей, поэтому и ключ живёт там же: ``options.participant_name_key``.
# Здесь — только значение по умолчанию, то самое, что стоит в документации.
PARTICIPANT_NAME_KEY_OPTION = "participant_name_key"
DEFAULT_PARTICIPANT_NAME_KEY = "name"

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

        rows, raw = await self.rows_for(NEWDB_METHOD, inn_params(inn))
        name_key = self.option(
            NEWDB_METHOD, PARTICIPANT_NAME_KEY_OPTION, DEFAULT_PARTICIPANT_NAME_KEY
        )
        parsed = [
            case
            for row in rows[:MAX_RECORDS]
            if (case := _to_case(row, subject=subject, searched_inn=inn, name_key=name_key))
            is not None
        ]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if parsed else ProviderStatus.NO_RESULTS,
            records=list(parsed),
            raw_response=self.raw_for(raw),
        )


def _to_case(
    record: Mapping[str, Any], *, subject: SearchSubject, searched_inn: str, name_key: str
) -> CourtCase | None:
    """A row with no case number is not a case we can show or verify.

    ``searched_inn`` fills the identity the row itself does not carry: the case
    was returned for that ИНН, so the ИНН belongs on the record. Without it every
    case comes back with no identifiers, the matcher rates it weak, and the
    report drops a live claim as somebody else's business.

    Safe here in a way it would not be everywhere: a row of this method is a
    *case*, not a person, and every case in the answer came back for the ИНН
    that was asked about. Methods whose rows are per-subject containers — the
    bankruptcy one — read the subject's own identifiers out of the container
    instead (``row_fields``).
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
        role=_role_of(record, subject, name_key),
        is_closed=_is_closed(record),
        source_url=as_text(record.get("source_url")),
        fetched_at=utcnow(),
    )


def _role_of(record: Mapping[str, Any], subject: SearchSubject, name_key: str) -> CourtCaseRole:
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
    if _names_include(record.get("participants_defendants"), subject.name, name_key):
        return CourtCaseRole.DEFENDANT
    if _names_include(record.get("participants_plaintiffs"), subject.name, name_key):
        return CourtCaseRole.PLAINTIFF
    return CourtCaseRole.OTHER


def _names_include(participants: Any, name: PersonName, name_key: str) -> bool:
    """Стоит ли субъект в этом списке участников.

    Сравнение идёт через ``compare_names``, поэтому не зависит ни от порядка
    слов, ни от того, записано ли имя целиком: КАД печатает участников то
    «Фамилия Имя Отчество», то наоборот, то «Бычков Д.Ю.», то «ИП Иванов И.И.».
    Точное равенство строк промахивалось по всем трём поводам, и промах давал
    роль OTHER — дело выпадало из исков к должнику, а скоринг начислял плюс за
    то, что исков не найдено, показывая при этом само дело в отчёте.

    Инициалов здесь достаточно, и это не поблажка в отождествлении: дело уже
    вернулось по ИНН должника, и вопрос стоит не «его ли это дело», а «на какой
    он в нём стороне». Кем считать однофамильца с теми же инициалами в чужом
    деле, этот код не решает — такое дело сюда не попадает.
    """
    if not isinstance(participants, Sequence) or isinstance(participants, (str, bytes)):
        return False
    for item in participants:
        raw = dig(item, name_key) if isinstance(item, Mapping) else item
        if compare_names(name, as_text(raw)) is not NameMatch.NONE:
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
