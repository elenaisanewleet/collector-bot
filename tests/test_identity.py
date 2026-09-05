"""Identity matching and input normalization.

The behaviour under test is a safety property: a shared name must never be
enough to declare two records the same person.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.enums import MatchLevel, SearchType
from app.domain.identity import (
    NameParseError,
    PersonName,
    SearchSubject,
    normalize_inn,
    normalize_passport,
    normalize_phone,
    normalize_plate,
    normalize_vin,
    parse_fio,
)
from app.domain.models import match_level_for
from app.services.identity import IdentityMatcher
from tests.conftest import make_bankruptcy, make_business, make_proceeding


@pytest.fixture
def matcher() -> IdentityMatcher:
    return IdentityMatcher()


def subject_for(
    name: str = "Тестов Андрей Сергеевич",
    birth_date: date | None = date(1985, 3, 12),
    inn: str | None = None,
) -> SearchSubject:
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=parse_fio(name),
        birth_date=birth_date,
        inn=inn,
    )


# ---------------------------------------------------------------- parsing


def test_parse_full_name() -> None:
    name = parse_fio("иванов иван иванович")
    assert (name.last_name, name.first_name, name.middle_name) == (
        "Иванов",
        "Иван",
        "Иванович",
    )


def test_parse_name_without_patronymic() -> None:
    name = parse_fio("Петров Пётр")
    assert name.middle_name is None
    assert name.full == "Петров Пётр"


def test_parse_hyphenated_surname() -> None:
    assert parse_fio("петров-водкин кузьма сергеевич").last_name == "Петров-Водкин"


@pytest.mark.parametrize(
    "raw", ["", "   ", "Иванов", "Иванов Иван Иванович Иванович", "Иванов И3ан"]
)
def test_malformed_names_are_rejected_not_guessed(raw: str) -> None:
    with pytest.raises(NameParseError):
        parse_fio(raw)


# ---------------------------------------------------------------- matching


def test_full_name_and_birth_date_is_confirmed(matcher: IdentityMatcher) -> None:
    record = make_proceeding(confidence=0.0)
    assessment = matcher.assess(subject_for(), record)
    assert match_level_for(assessment.confidence) is MatchLevel.CONFIRMED


def test_same_name_different_birth_date_is_rejected(matcher: IdentityMatcher) -> None:
    """The core safety case: two people who share a name are not one person."""
    record = make_proceeding(birth_date=date(1970, 1, 1), confidence=0.0)
    assessment = matcher.assess(subject_for(), record)
    assert match_level_for(assessment.confidence) is MatchLevel.WEAK
    assert "дата рождения не совпадает" in assessment.reasons


def test_name_only_never_reaches_confirmed(matcher: IdentityMatcher) -> None:
    subject = subject_for(birth_date=None)
    record = make_proceeding(birth_date=None, confidence=0.0)
    assessment = matcher.assess(subject, record)
    assert match_level_for(assessment.confidence) is MatchLevel.PROBABLE
    assert match_level_for(assessment.confidence) is not MatchLevel.CONFIRMED


def test_common_surname_without_discriminator_is_weak(matcher: IdentityMatcher) -> None:
    subject = subject_for("Иванов Иван Иванович", birth_date=None)
    record = make_proceeding(name="Иванов Иван Иванович", birth_date=None, confidence=0.0)
    assessment = matcher.assess(subject, record)
    assert match_level_for(assessment.confidence) is MatchLevel.WEAK


def test_missing_birth_date_on_record_stays_probable(matcher: IdentityMatcher) -> None:
    record = make_proceeding(birth_date=None, confidence=0.0)
    assessment = matcher.assess(subject_for(), record)
    assert match_level_for(assessment.confidence) is MatchLevel.PROBABLE


def test_different_name_scores_near_zero(matcher: IdentityMatcher) -> None:
    record = make_proceeding(name="Совсем Другой Человек", birth_date=None, confidence=0.0)
    assessment = matcher.assess(subject_for(birth_date=None), record)
    assert assessment.confidence < 0.3


def test_surname_and_first_name_only_match(matcher: IdentityMatcher) -> None:
    record = make_proceeding(name="Тестов Андрей", confidence=0.0)
    assessment = matcher.assess(subject_for(), record)
    assert match_level_for(assessment.confidence) is MatchLevel.PROBABLE
    assert "совпадают фамилия и имя" in assessment.reasons


def test_matching_inn_confirms(matcher: IdentityMatcher) -> None:
    subject = subject_for(birth_date=None, inn="770912345601")
    record = make_business(confidence=0.0)
    assessment = matcher.assess(subject, record)
    assert match_level_for(assessment.confidence) is MatchLevel.CONFIRMED


def test_conflicting_inn_penalizes(matcher: IdentityMatcher) -> None:
    subject = subject_for(birth_date=None, inn="000000000000")
    record = make_business(confidence=0.0)
    assessment = matcher.assess(subject, record)
    assert "ИНН не совпадает" in assessment.reasons
    assert match_level_for(assessment.confidence) is MatchLevel.WEAK


def test_company_name_is_not_matched_to_a_person(matcher: IdentityMatcher) -> None:
    """A legal entity's name is not its director's name."""
    from app.domain.enums import BusinessRole, BusinessStatus
    from app.domain.models import BusinessRelation

    record = BusinessRelation(
        name='ООО "Тестов и партнёры"',
        role=BusinessRole.DIRECTOR,
        status=BusinessStatus.ACTIVE,
    )
    assessment = matcher.assess(subject_for(), record)
    assert match_level_for(assessment.confidence) is not MatchLevel.CONFIRMED


def test_company_inn_is_not_matched_against_the_person(matcher: IdentityMatcher) -> None:
    """ИНН компании — десять цифр, ИНН должника — двенадцать.

    Сравнивать их бессмысленно, а штраф за «несовпадение» стирал бы из отчёта
    ровно ту компанию, ради которой источник и опрашивали: 0.25 − 0.05 − 0.35
    даёт ноль, слабое совпадение и фильтр блока БИЗНЕС.
    """
    from app.domain.enums import BusinessRole, BusinessStatus, EntityType
    from app.domain.models import BusinessRelation

    record = BusinessRelation(
        inn="9728012826",
        name='ООО "СТАЛЬНОЕ СЕРДЦЕ"',
        entity_type=EntityType.LEGAL_ENTITY,
        role=BusinessRole.DIRECTOR,
        status=BusinessStatus.ACTIVE,
        linked_by_identifier=True,
    )
    assessment = matcher.assess(subject_for(birth_date=None, inn="770600089967"), record)

    assert "ИНН не совпадает" not in assessment.reasons
    assert match_level_for(assessment.confidence) is MatchLevel.CONFIRMED


def test_annotate_applies_confidence_floor(matcher: IdentityMatcher) -> None:
    """A record found by contract number keeps its confidence even with no name."""
    record = make_proceeding(name=None, birth_date=None, confidence=0.0)
    matcher.annotate(subject_for(), [record], confidence_floor=0.95)
    assert record.match_confidence == 0.95
    assert "найдено по точному идентификатору" in record.match_reasons


def test_annotate_does_not_lower_a_strong_match(matcher: IdentityMatcher) -> None:
    record = make_proceeding(confidence=0.0)
    matcher.annotate(subject_for(), [record], confidence_floor=0.5)
    assert record.match_confidence > 0.5


def test_bankruptcy_matching_uses_the_debtor_name(matcher: IdentityMatcher) -> None:
    record = make_bankruptcy(confidence=0.0)
    assessment = matcher.assess(subject_for(birth_date=None), record)
    assert assessment.confidence > 0.0


@pytest.mark.parametrize("confidence", [0.0, 0.3, 0.55, 0.85, 1.0])
def test_confidence_is_always_bounded(matcher: IdentityMatcher, confidence: float) -> None:
    record = make_proceeding(confidence=confidence)
    assessment = matcher.assess(subject_for(), record)
    assert 0.0 <= assessment.confidence <= 1.0


# ---------------------------------------------------------------- normalizers


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+7 (999) 123-45-67", "+79991234567"),
        ("89991234567", "+79991234567"),
        ("9991234567", "+79991234567"),
        ("7 999 123 45 67", "+79991234567"),
        ("123", None),
        ("", None),
        ("+1 202 555 0100", None),
    ],
)
def test_phone_normalization(raw: str, expected: str | None) -> None:
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("XW8ZZZ61ZKG011111", "XW8ZZZ61ZKG011111"),
        ("xw8zzz61zkg011111", "XW8ZZZ61ZKG011111"),
        ("XW8ZZZ61ZKG01111", None),
        ("XW8ZZZ61ZKG0111111", None),
        ("IOQZZZ61ZKG011111", None),
        ("", None),
    ],
)
def test_vin_validation(raw: str, expected: str | None) -> None:
    assert normalize_vin(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("А123ВС77", "А123ВС77"),
        ("а123вс777", "А123ВС777"),
        ("A123BC77", "А123ВС77"),
        ("не номер", None),
        ("", None),
    ],
)
def test_plate_normalization(raw: str, expected: str | None) -> None:
    assert normalize_plate(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("770912345601", "770912345601"), ("7709123456", "7709123456"), ("123", None)],
)
def test_inn_normalization(raw: str, expected: str | None) -> None:
    assert normalize_inn(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("45 09 123456", "4509123456"), ("4509123456", "4509123456"), ("45091234", None)],
)
def test_passport_normalization(raw: str, expected: str | None) -> None:
    assert normalize_passport(raw) == expected


def test_person_name_short_form() -> None:
    name = PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич")
    assert name.normalized_short == "тестов андрей"
