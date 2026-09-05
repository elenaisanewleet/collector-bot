"""Deciding whether an external record describes *this* person.

This is the safety-critical part of the tool. Two people can share a name, and
acting on someone else's enforcement proceedings is both wrong and expensive, so
a name match on its own never produces a confirmed result. Confidence only
reaches the confirmed band when a discriminating identifier — date of birth,
INN, or a VIN the operator themself put in the query — agrees.

The mirror-image failure is just as bad and less obvious: a record that *is* the
debtor's, scored as somebody else's, vanishes from the report and the score
rewards its absence. Two guards against it live here — names are compared
without regard to word order (:func:`app.domain.identity.compare_names`), and an
exact query VIN carries a record that has no other identifier at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.domain.identity import (
    NameMatch,
    PersonName,
    SearchSubject,
    compare_names,
    normalize_phone,
    normalize_vin,
)
from app.domain.models import (
    BankruptcyRecord,
    BusinessRelation,
    CourtCase,
    EnforcementProceeding,
    InternalDebtorRecord,
    LegalEntityCase,
    PledgeRecord,
    SourcedFact,
    VehicleRecord,
)
from app.utils.hashing import normalize_token

# Weights. Tuned so that name-only never crosses the confirmed threshold (0.85)
# and a date-of-birth conflict always sinks a record.
FULL_NAME_MATCH = 0.60
SHORT_NAME_MATCH = 0.45
# «Бычков Д.Ю.»: фамилия целиком, имя и отчество — инициалами. Меньше короткого
# совпадения, потому что доказывает меньше, но обязательно больше, чем
# ``NAME_UNKNOWN``: имя, которое сошлось хотя бы инициалами, не может стоить
# дешевле отсутствующего. Само по себе оно по-прежнему не доводит запись даже до
# «возможного совпадения» — только вместе с датой рождения или ИНН.
INITIALS_NAME_MATCH = 0.30
NAME_UNKNOWN = 0.25
BIRTH_DATE_MATCH_BONUS = 0.30
INN_MATCH_BONUS = 0.30
INN_MISMATCH_PENALTY = -0.35
PHONE_MATCH_BONUS = 0.20
NO_DISCRIMINATOR_PENALTY = -0.05
COMMON_SURNAME_PENALTY = -0.05
CONFLICTING_BIRTH_DATE_CONFIDENCE = 0.05
IDENTIFIER_LOOKUP_CONFIDENCE = 1.0
# A VIN the operator typed into the query, echoed back by the record.
VIN_QUERY_MATCH_CONFIDENCE = 0.95

# Surnames common enough that a name match carries noticeably less evidence.
COMMON_SURNAMES: frozenset[str] = frozenset(
    {
        "иванов",
        "иванова",
        "смирнов",
        "смирнова",
        "кузнецов",
        "кузнецова",
        "попов",
        "попова",
        "васильев",
        "васильева",
        "петров",
        "петрова",
        "соколов",
        "соколова",
        "михайлов",
        "михайлова",
        "новиков",
        "новикова",
        "федоров",
        "федорова",
        "морозов",
        "морозова",
        "волков",
        "волкова",
        "алексеев",
        "алексеева",
        "лебедев",
        "лебедева",
        "семенов",
        "семенова",
        "егоров",
        "егорова",
        "павлов",
        "павлова",
        "козлов",
        "козлова",
        "степанов",
        "степанова",
        "николаев",
        "николаева",
        "орлов",
        "орлова",
    }
)


@dataclass(frozen=True, slots=True)
class MatchAssessment:
    confidence: float
    reasons: tuple[str, ...]


class IdentityMatcher:
    """Scores how strongly a record belongs to the search subject."""

    def assess(self, subject: SearchSubject, record: SourcedFact) -> MatchAssessment:
        if _is_about_a_company(record):
            # Запись об организации — не факт о человеке. Сопоставлять её с ФИО
            # не с чем (название ООО не является именем), а ИНН в ней —
            # компании, а не должника. Единственное доступное основание — то,
            # как её нашли: запрос по идентификатору должника связывает её с
            # ним, запрос по ФИО не связывает ни с чем.
            #
            # Раньше такая запись шла общим путём и получала 0.20 — «слабое
            # совпадение», — после чего блок БИЗНЕС её отбрасывал и печатал
            # «связей с ИП и юрлицами не найдено» про должника с действующим
            # ООО. С ИНН физлица в субъекте выходило ещё хуже: ИНН компании
            # сравнивался с ИНН человека, не совпадал, и запись падала в ноль.
            if _linked_by_identifier(record):
                return MatchAssessment(
                    confidence=IDENTIFIER_LOOKUP_CONFIDENCE,
                    reasons=("связь получена по ИНН должника",),
                )
            return MatchAssessment(
                confidence=NAME_UNKNOWN,
                reasons=("связь с должником не подтверждена идентификатором",),
            )

        record_name = _record_name(record)
        record_birth_date = _record_birth_date(record)
        record_inn = _record_inn(record)
        record_phone = _record_phone(record)

        confidence, reasons = self._name_component(subject.name, record_name)

        if subject.birth_date and record_birth_date:
            if subject.birth_date == record_birth_date:
                confidence += BIRTH_DATE_MATCH_BONUS
                reasons = (*reasons, "совпадает дата рождения")
            else:
                # A different date of birth is a disqualifier, not a deduction:
                # no amount of name agreement outweighs it.
                return MatchAssessment(
                    confidence=CONFLICTING_BIRTH_DATE_CONFIDENCE,
                    reasons=(*reasons, "дата рождения не совпадает"),
                )

        contradicted = False
        if subject.inn and record_inn:
            if _digits_equal(subject.inn, record_inn):
                confidence += INN_MATCH_BONUS
                reasons = (*reasons, "совпадает ИНН")
            else:
                confidence += INN_MISMATCH_PENALTY
                reasons = (*reasons, "ИНН не совпадает")
                contradicted = True

        if subject.phone and record_phone and _phones_equal(subject.phone, record_phone):
            confidence += PHONE_MATCH_BONUS
            reasons = (*reasons, "совпадает телефон")

        if not contradicted and _vin_from_query_matches(subject, record):
            return MatchAssessment(
                confidence=max(_clamp(confidence), VIN_QUERY_MATCH_CONFIDENCE),
                reasons=(*reasons, "совпадает VIN, по которому шёл поиск"),
            )

        if not _has_discriminator(subject, record_birth_date, record_inn):
            confidence += NO_DISCRIMINATOR_PENALTY
            reasons = (*reasons, "нет уточняющих идентификаторов")
            if subject.name and _is_common_surname(subject.name):
                confidence += COMMON_SURNAME_PENALTY
                reasons = (*reasons, "распространённая фамилия")

        return MatchAssessment(confidence=_clamp(confidence), reasons=reasons)

    def _name_component(
        self, subject_name: PersonName | None, record_name: str | None
    ) -> tuple[float, tuple[str, ...]]:
        if subject_name is None or not record_name:
            return NAME_UNKNOWN, ("ФИО не сопоставлено",)
        # Order-free by construction: see ``compare_names``. Sources disagree
        # about where the surname goes, and that disagreement is about
        # formatting, not about who the person is.
        match compare_names(subject_name, record_name):
            case NameMatch.FULL:
                return FULL_NAME_MATCH, ("полное совпадение ФИО",)
            case NameMatch.SHORT:
                return SHORT_NAME_MATCH, ("совпадают фамилия и имя",)
            case NameMatch.INITIALS:
                return INITIALS_NAME_MATCH, ("фамилия совпадает, имя — по инициалам",)
            case _:
                return 0.0, ("ФИО не совпадает",)

    def annotate(
        self,
        subject: SearchSubject,
        records: list[SourcedFact],
        *,
        confidence_floor: float | None = None,
    ) -> None:
        """Attach a confidence and its explanation to each record in place.

        ``confidence_floor`` is used for records retrieved by an exact
        identifier (a contract number, an internal id): the lookup itself is the
        evidence, so a missing date of birth must not demote them.
        """
        for record in records:
            assessment = self.assess(subject, record)
            confidence = assessment.confidence
            reasons = assessment.reasons
            if confidence_floor is not None and confidence < confidence_floor:
                confidence = confidence_floor
                reasons = (*reasons, "найдено по точному идентификатору")
            record.match_confidence = confidence
            record.match_reasons = reasons


def _record_name(record: SourcedFact) -> str | None:
    if isinstance(record, InternalDebtorRecord):
        return record.full_name
    if isinstance(record, (EnforcementProceeding, BankruptcyRecord)):
        return record.debtor_name
    if isinstance(record, PledgeRecord):
        return record.pledgor_name
    if isinstance(record, CourtCase):
        return record.participant_name
    if isinstance(record, BusinessRelation):
        return _strip_business_prefix(record.name)
    return None


def _strip_business_prefix(name: str | None) -> str | None:
    """``ИП Тестов Андрей Сергеевич`` -> ``Тестов Андрей Сергеевич``.

    Only the sole-proprietor prefix is stripped: a company name is not a person's
    name, and pretending otherwise would manufacture matches.
    """
    if not name:
        return None
    token = name.strip()
    for prefix in ("ИП ", "Индивидуальный предприниматель "):
        if token.startswith(prefix):
            return token[len(prefix) :].strip()
    return None


def _record_birth_date(record: SourcedFact) -> date | None:
    if isinstance(record, InternalDebtorRecord):
        return record.birth_date
    if isinstance(record, EnforcementProceeding):
        return record.debtor_birth_date
    if isinstance(record, PledgeRecord):
        return record.pledgor_birth_date
    return None


def _is_about_a_company(record: SourcedFact) -> bool:
    """Записи, чей субъект — организация, а не человек."""
    if isinstance(record, LegalEntityCase):
        return True
    return isinstance(record, BusinessRelation) and record.is_legal_entity


def _linked_by_identifier(record: SourcedFact) -> bool:
    if isinstance(record, LegalEntityCase):
        # Дело найдено по ИНН компании, а компания — по ИНН должника: цепочка
        # держится на идентификаторах от начала до конца.
        return True
    return isinstance(record, BusinessRelation) and record.linked_by_identifier


def _record_inn(record: SourcedFact) -> str | None:
    if isinstance(record, BusinessRelation):
        # У связи с юрлицом ИНН принадлежит компании, а не человеку: десять
        # цифр против двенадцати. Сравнивать их бессмысленно, а штраф за
        # «несовпадение» стирал бы из отчёта ровно то юрлицо, ради которого
        # источник и опрашивали.
        return None if record.is_legal_entity else record.inn
    if isinstance(record, (BankruptcyRecord, CourtCase)):
        return record.inn
    if isinstance(record, PledgeRecord):
        return record.pledgor_inn
    return None


def _record_phone(record: SourcedFact) -> str | None:
    # Only our own records carry a phone; no external source is queried by phone.
    return record.phone if isinstance(record, InternalDebtorRecord) else None


def _record_vin(record: SourcedFact) -> str | None:
    if isinstance(record, (PledgeRecord, VehicleRecord, InternalDebtorRecord)):
        return record.vin
    return None


def _vin_from_query_matches(subject: SearchSubject, record: SourcedFact) -> bool:
    """Did the operator ask about this exact vehicle, and does the record echo it?

    Why this is a confidence floor and not a hole in the matching
    ------------------------------------------------------------
    ``pledge_vin`` answers with a notice and a pledgor name, and with neither a
    date of birth nor an ИНН — so the ordinary rules cap such a record at a weak
    match and the report drops it. The record then disappears *even though the
    query was an exact 17-character VIN*, which is a stronger identifier than
    anything the answer could have contained. That is the same inversion the
    rest of this module exists to prevent, arriving from the other side.

    The floor is granted by the **query**, never by the record, and that is what
    keeps it honest:

    *   It needs ``subject.vehicle.vin`` — a VIN the operator typed. A record
        cannot talk its way into a match by carrying a VIN we never asked about.
    *   Both sides go through :func:`normalize_vin`, so only a real VIN counts.
        ``pledge_subject_ids_raw`` also carries non-VIN equipment numbers, and a
        short shared identifier is not a unique one.
    *   It never overrides a contradiction: a conflicting date of birth has
        already returned above, and a conflicting ИНН suppresses it here. Only a
        *name* disagreement is overridden, deliberately — for a VIN search the
        vehicle is the subject, the pledgor may be a previous owner or a name
        we never knew, and "this car is somebody's collateral" is true and
        material either way.
    """
    subject_vin = normalize_vin(subject.vehicle.vin) if subject.vehicle else None
    if subject_vin is None:
        return False
    return subject_vin == normalize_vin(_record_vin(record))


def _has_discriminator(
    subject: SearchSubject, record_birth_date: date | None, record_inn: str | None
) -> bool:
    if subject.birth_date and record_birth_date:
        return True
    return bool(subject.inn and record_inn)


def _is_common_surname(name: PersonName) -> bool:
    return normalize_token(name.last_name) in COMMON_SURNAMES


def _digits_equal(left: str, right: str) -> bool:
    return "".join(filter(str.isdigit, left)) == "".join(filter(str.isdigit, right))


def _phones_equal(left: str, right: str) -> bool:
    normalized = normalize_phone(left)
    return normalized is not None and normalized == normalize_phone(right)


def _clamp(value: float) -> float:
    """Clamp to [0, 1] and round away binary-float noise.

    Without the rounding, 0.60 - 0.05 lands at 0.5499999999999999 and a record
    that should sit exactly on the "possible match" threshold falls below it.
    """
    return round(max(0.0, min(1.0, value)), 4)
