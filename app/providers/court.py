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
from app.domain.identity import PersonName, SearchSubject, compare_names, is_name_evidence
from app.domain.models import CourtCase, ProviderResult
from app.providers.mapping import as_text, dig
from app.providers.newdb import (
    MappedRows,
    NewDBMethodProvider,
    container_flag,
    container_int,
    individual_inn,
    inn_params,
)
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

        mapped, raw = await self.mapped_for(NEWDB_METHOD, inn_params(inn))
        name_key = self.option(
            NEWDB_METHOD, PARTICIPANT_NAME_KEY_OPTION, DEFAULT_PARTICIPANT_NAME_KEY
        )
        rows = mapped.records
        parsed = [
            case
            for row in rows[:MAX_RECORDS]
            if (case := _to_case(row, subject=subject, searched_inn=inn, name_key=name_key))
            is not None
        ]
        notes = _completeness_notes(mapped, shown=len(rows))
        if len(rows) > MAX_RECORDS:
            notes.append(
                f"Показаны первые {MAX_RECORDS} дел из {len(rows)}, полученных от источника"
            )
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if parsed else ProviderStatus.NO_RESULTS,
            records=list(parsed),
            is_partial=bool(notes),
            notes=tuple(notes),
            raw_response=self.raw_for(raw),
        )


def _completeness_notes(mapped: MappedRows, *, shown: int) -> list[str]:
    """Всё ли дела прислал источник — по его же счётчикам.

    Живая строка ``data`` у этого метода — не дело, а обёртка на запрос, и в
    ней вендор сам говорит, сколько дел нашёл (``total_count``) и сколько
    отдал (``pagination.limit`` = 10, ``pagination.has_more``). У должника с
    сорока делами придут десять, карта разберёт десять, ``unreadable`` будет
    ноль — и отчёт напечатает десять дел как всё, что есть, тем увереннее
    ошибаясь, чем хуже должник. Здесь это становится сказанным вслух.

    ``found: true`` при пустом ``cases[]`` — тот же случай с другой стороны:
    источник нашёл и не отдал, а нулевые записи прочитались бы как «дел нет».
    """
    notes: list[str] = []
    total = container_int(mapped.containers, "total_count")
    if total is not None and total > shown:
        notes.append(
            f"Источник нашёл {total} арбитражных дел, а прислал {shown}: "
            "ответ обрезан постранично, список неполный"
        )
    elif container_flag(mapped.containers, "has_more"):
        notes.append("Источник отдал не все арбитражные дела (has_more) — список неполный")
    if not shown and container_flag(mapped.containers, "found"):
        notes.append("Источник сообщил, что дела найдены, но не прислал ни одного")
    return notes


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
    #
    # Списки участников лежат внутри разобранной карточки дела, а карточку
    # вендор делает не для каждого дела: живой ответ прямо пишет «Подробно
    # разобрано 1 из текущих 1». У дела без карточки списков нет, роль осталась
    # бы OTHER, дело не попало бы в иски к должнику — и скоринг начислил бы
    # плюс «действующих исков не найдено», напечатав это самое дело строкой
    # выше. Поэтому запасной источник роли — плоские ``respondent`` и
    # ``plaintiff`` строки дела, которые есть всегда.
    if _names_include(record.get("participants_defendants"), subject.name, name_key):
        return CourtCaseRole.DEFENDANT
    if _names_include(record.get("participants_plaintiffs"), subject.name, name_key):
        return CourtCaseRole.PLAINTIFF
    if _names_include(record.get("respondent_name"), subject.name, name_key):
        return CourtCaseRole.DEFENDANT
    if _names_include(record.get("plaintiff_name"), subject.name, name_key):
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

    Достаточно — но именно доказательства, а не «чего угодно, кроме
    противоречия». Проверка была ``is not NameMatch.NONE``, то есть роль
    назначало и нечитаемое имя: строка «Данные скрыты» в списке ответчиков или
    одна фамилия без имени делали должника ответчиком по чужому иску, а
    «Леликов Андрей Петрович» при должнике Андрее Сергеевиче — тем более. См.
    :func:`app.domain.identity.is_name_evidence`.

    Одиночная строка принимается наравне со списком: у КАД сторона дела
    приходит и списком объектов (``card.participants.defendants``), и плоской
    строкой (``respondent``). Раньше строка отбрасывалась вместе с байтами —
    ``«Иванов Андрей Викторович»`` давал False, — и запасной путь к роли,
    единственный у дела без разобранной карточки, не работал бы вовсе.
    """
    if isinstance(participants, str):
        participants = [participants]
    if not isinstance(participants, Sequence) or isinstance(participants, bytes):
        return False
    for item in participants:
        raw = dig(item, name_key) if isinstance(item, Mapping) else item
        if is_name_evidence(compare_names(name, as_text(raw))):
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
