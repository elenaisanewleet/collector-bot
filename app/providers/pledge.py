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
describes one set of rows, so the shipped map reads the ФНП branch only, and a
debtor whose only encumbrance is a leasing contract comes back ``NO_RESULTS``.

Everything downstream is therefore worded to the branch that was actually read:
the report prints "записей в реестре залогов не найдено" followed by
``PLEDGE_SCOPE_NOTE``, and the score's positive factor says "в реестре
уведомлений ФНП действующих залогов не найдено". Neither says "имущество не
обременено" — that would be a claim about all of the debtor's property drawn
from one register of movables, and one leasing contract would make it a lie.

**Третья ветка того же ответа — ``fnp_urls``.** Живой вызов 05.09.2026 вернул
пустые ``fnp`` и ``fedresurs`` при тринадцати ссылках в ``fnp_urls``: реестр
нашёл тринадцать уведомлений по ФИО и отфильтровал их все по дате рождения.
Ноль записей здесь — это «найдено, не сопоставлено», и такой ответ помечается
``is_partial`` со списком ссылок в ``notes``, а не выдаётся за чистый реестр.
См. :func:`_unmatched_notices`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from app.domain.enums import MissingInput, PledgeStatus, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject, normalize_vin
from app.domain.models import PledgeRecord, ProviderResult
from app.providers.base import NO_CONTEXT, FetchContext
from app.providers.mapping import as_text
from app.providers.newdb import (
    COUNTRY_RU,
    NewDBMethodProvider,
    container_list,
    person_params_for,
)
from app.utils.dates import parse_date, utcnow
from app.utils.formatting import pluralize_ru

PERSON_METHOD = "pledge_person"
VIN_METHOD = "pledge_vin"
MAX_RECORDS = 100

# Дата рождения зовётся здесь иначе, чем в ФССП: ``datebirth`` против ``dob``.
# Подтверждено живым вызовом 05.09.2026 — сервис принял параметр и вернул
# ``state: complete`` (эхо запроса видно в ``params`` захваченного ответа,
# tests/data/newdb_live_pledge_person_unmatched.json).
DATE_BIRTH_KEY = "datebirth"

# Предмет залога описан перечнем номеров: «XUS22270280002514, 15218-1, 15274-1».
_SUBJECT_ID_SPLIT = re.compile(r"[,;]+")

_TERMINATED_TOKENS = frozenset({"terminated", "excluded", "исключ", "прекращ", "погашен", "снят"})
_ACTIVE_TOKENS = frozenset({"active", "действует", "действующее", "актуальн", "зарегистрирован"})


class NewDBPledgeProvider(NewDBMethodProvider):
    """Залоги движимого имущества через NewDB."""

    name = ProviderName.PLEDGE
    title = "Залоги"
    methods = (PERSON_METHOD, VIN_METHOD)

    def missing_input_for(self, subject: SearchSubject) -> tuple[MissingInput, ...]:
        """Два пути: VIN либо ФИО с датой рождения. VIN закрывает оба гейта."""
        return () if _searchable_by(subject) else _missing_for(subject)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        missing = self.missing_input_for(subject)
        if missing:
            return self.insufficient_query(
                "Для проверки залогов нужен VIN либо ФИО с датой рождения",
                missing=missing,
            )
        plans = self._plans(subject)
        if not plans:
            # Искать есть по чему, но метод под это не описан. Это пробел в
            # настройке, а не в данных, и путать их нельзя.
            return self.not_configured("Метод NewDB под этот запрос не описан в NEWDB_FIELD_MAP")

        records: list[PledgeRecord] = []
        raw_bodies: list[str] = []
        notices: list[str] = []
        for method, params in plans:
            mapped, raw = await self.mapped_for(method, params)
            raw_bodies.append(raw)
            records.extend(_to_pledge(row) for row in mapped.records)
            notices.extend(container_list(mapped.containers, "notice_urls"))

        unique = _dedupe(records)[:MAX_RECORDS]
        unmatched = _unmatched_notices(notices, unique)
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if unique else ProviderStatus.NO_RESULTS,
            records=list(unique),
            is_partial=bool(unmatched),
            notes=_unmatched_note(unmatched, parsed=len(unique)),
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


MAX_LISTED_NOTICES = 5


def _unmatched_notices(notices: list[str], records: list[PledgeRecord]) -> list[str]:
    """Уведомления, которые реестр нашёл, а в разобранные записи не попали.

    Живой ответ ``pledge_person`` от 05.09.2026: ``fnp: []``, ``fedresurs: []`` —
    и тринадцать ссылок в ``fnp_urls``. Документация метода объясняет механику:
    дата рождения «используется для фильтрации релевантных записей», то есть
    реестр ФНП нашёл по ФИО тринадцать уведомлений, а в ``fnp`` положил только
    прошедшие фильтр — здесь ни одного. Ноль записей при непустом ``fnp_urls``
    означает «найдено, не сопоставлено», а вовсе не «залогов нет», и разница
    здесь ровно та, ради которой написан весь этот модуль: залог, показанный
    отсутствующим, превращается в плюс к оценке взыскиваемости.

    То же самое, только мягче, бывает и при непустом ``fnp``: ``fnp_urls`` —
    надмножество ``fnp``, и уведомление, которое реестр нашёл, но не разобрал,
    иначе исчезло бы молча. Сопоставление идёт по ссылке: у разобранной записи
    в ``source_url`` стоит тот же адрес уведомления.
    """
    if not notices:
        return []
    seen = {
        (record.source_url or "").strip().rstrip("/") for record in records if record.source_url
    }
    unmatched: list[str] = []
    for url in notices:
        key = url.strip().rstrip("/")
        if key and key not in seen and key not in unmatched:
            unmatched.append(key)
    return unmatched


def _unmatched_note(unmatched: list[str], *, parsed: int) -> tuple[str, ...]:
    """Сказать про несопоставленные уведомления так, чтобы можно было проверить.

    Ссылки печатаются, а не пересчитываются: единственное, что взыскатель может
    с этим сделать, — открыть их руками, и номер уведомления в реестре по
    количеству не восстанавливается.
    """
    if not unmatched:
        return ()
    count = len(unmatched)
    noun = pluralize_ru(count, "уведомление", "уведомления", "уведомлений")
    head = (
        f"В реестре ФНП найдено {count} {noun} на это ФИО, "
        "ни одно не сопоставлено по дате рождения — нужна ручная проверка:"
        if not parsed
        else (
            f"Ещё {count} {noun} ФНП разобрать не удалось — "
            "они не вошли в список выше, нужна ручная проверка:"
        )
    )
    lines = [head, *(f"— {url}" for url in unmatched[:MAX_LISTED_NOTICES])]
    hidden = count - MAX_LISTED_NOTICES
    if hidden > 0:
        lines.append(f"— …и ещё {hidden}")
    return tuple(lines)


def _searchable_by(subject: SearchSubject) -> bool:
    return bool(_subject_vin(subject)) or _has_identity(subject)


def _missing_for(subject: SearchSubject) -> tuple[MissingInput, ...]:
    """Чего не хватило именно этому субъекту.

    VIN здесь не называется: у поиска по человеку его нет и быть не должно, а
    предложить оператору «дайте VIN» вместо «дайте дату рождения» значило бы
    отправить его за тем, чего он не найдёт. Строка сообщения по-прежнему
    называет оба пути.
    """
    if subject.name is None:
        return (MissingInput.NAME, MissingInput.BIRTH_DATE)
    return (MissingInput.BIRTH_DATE,)


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
    """Вытащить VIN из перечня идентификаторов предмета залога.

    ``pledge_subject_ids_raw`` — это НЕ одно поле с VIN, а список
    идентификаторов предмета через запятую: «XUS22270280002514, 15218-1,
    15274-1». Целиком такая строка не VIN ни по длине, ни по алфавиту, поэтому
    :func:`normalize_vin` возвращала ``None``, и уведомление про ту самую машину
    переставало сходиться с VIN, по которому шёл поиск: единственный
    идентификатор записи пропадал, матчер оценивал её как слабое совпадение, и
    залог на искомый автомобиль исчезал из отчёта.

    Токены перебираются по очереди, и первый настоящий VIN становится ``vin``
    записи. Если VIN нет ни одного, значение сохраняется как есть: это всё
    равно единственное описание предмета, и потерять его нельзя — просто
    отождествление по VIN на нём не сработает, что честно.
    """
    tokens = _subject_id_tokens(raw)
    for token in tokens:
        vin = normalize_vin(token)
        if vin is not None:
            return vin
    return ", ".join(tokens).upper() or None


def _subject_id_tokens(raw: Any) -> list[str]:
    """Список приходит и строкой через запятую, и массивом."""
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    tokens: list[str] = []
    for value in values:
        text = as_text(value)
        if text is None:
            continue
        tokens.extend(part for chunk in _SUBJECT_ID_SPLIT.split(text) if (part := chunk.strip()))
    return tokens


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
    """The person search and the VIN search legitimately return the same notice.

    Keyed by the notice, never by the car. A VIN is shared by every notice about
    that vehicle — registration, amendment, exclusion — and collapsing them
    would throw away the most valuable one of the three without a sound: the
    other fields parse, so nothing counts as unreadable. The notice URL is the
    fallback because it is unique by construction and, unlike the registration
    number, was confirmed to exist on the live answer.
    """
    seen: set[str] = set()
    unique: list[PledgeRecord] = []
    for record in records:
        key = (record.registration_number or record.source_url or "").strip().lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        unique.append(record)
    return unique


__all__ = ["PERSON_METHOD", "VIN_METHOD", "NewDBPledgeProvider"]
