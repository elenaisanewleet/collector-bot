"""Identity matching and input normalization.

The behaviour under test is a safety property: a shared name must never be
enough to declare two records the same person.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.enums import MatchLevel, SearchType
from app.domain.identity import (
    NameMatch,
    NameParseError,
    PersonName,
    SearchSubject,
    VehicleDescriptor,
    compare_names,
    normalize_inn,
    normalize_passport,
    normalize_phone,
    normalize_plate,
    normalize_vin,
    parse_fio,
)
from app.domain.models import PledgeRecord, match_level_for
from app.services.identity import IdentityMatcher
from tests.conftest import make_bankruptcy, make_business, make_pledge, make_proceeding


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


# ---------------------------------------------------------- порядок слов в ФИО


@pytest.mark.parametrize(
    "raw",
    [
        "Тестов Андрей Сергеевич",
        # Порядок ФНП: «ИМЯ ОТЧЕСТВО ФАМИЛИЯ».
        "Андрей Сергеевич Тестов",
        "АНДРЕЙ СЕРГЕЕВИЧ ТЕСТОВ",
        "  тестов   сергеевич   андрей  ",
    ],
)
def test_word_order_does_not_decide_who_a_person_is(raw: str) -> None:
    name = PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич")
    assert compare_names(name, raw) is NameMatch.FULL


@pytest.mark.parametrize(
    "raw",
    [
        # Тот же набор слов минус отчество, в любом порядке.
        "Тестов Андрей",
        "Андрей Тестов",
        # Отчество есть, но чужое: сравнить его не с чем — только фамилия и имя.
        "Андрей Петрович Тестов",
    ],
)
def test_a_name_without_a_comparable_patronymic_is_only_a_short_match(raw: str) -> None:
    name = PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич")
    assert compare_names(name, raw) is NameMatch.SHORT


@pytest.mark.parametrize(
    "raw",
    [
        # Однофамилец: фамилия та же, имя другое.
        "Тестов Пётр Сергеевич",
        "Сергеевич Пётр Тестов",
        # Тёзка по имени и отчеству, но не по фамилии.
        "Андрей Сергеевич Петров",
        # Фамилия и отчество без имени — общих слов два, но не те два.
        "Тестов Сергеевич",
        # Родительный падеж: после нормализации это просто другие слова.
        "Тестова Андрея Сергеевича",
        "",
    ],
)
def test_a_shared_word_is_not_a_shared_identity(raw: str) -> None:
    """Мультимножество — это ТО ЖЕ множество слов, а не пересечение."""
    name = PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич")
    assert compare_names(name, raw) is NameMatch.NONE


def test_a_reordered_name_matches_through_the_matcher(matcher: IdentityMatcher) -> None:
    """Тот же инвариант там, где он решает судьбу записи.

    Дефект был именно здесь: ФНП присылает «ИМЯ ОТЧЕСТВО ФАМИЛИЯ», позиционное
    сравнение давало 0.0 за ФИО, и найденная запись становилась слабым
    совпадением, то есть исчезала из отчёта.
    """
    record = make_proceeding(name="Андрей Сергеевич Тестов", confidence=0.0)
    assessment = matcher.assess(subject_for(), record)

    assert "полное совпадение ФИО" in assessment.reasons
    assert match_level_for(assessment.confidence) is MatchLevel.CONFIRMED


def test_a_namesake_without_a_discriminator_is_still_not_the_debtor(
    matcher: IdentityMatcher,
) -> None:
    """Свобода порядка слов не должна становиться свободой совпадений."""
    record = make_proceeding(name="Сергеевич Андрей Тестова", birth_date=None, confidence=0.0)
    assessment = matcher.assess(subject_for(birth_date=None), record)

    assert "ФИО не совпадает" in assessment.reasons
    assert match_level_for(assessment.confidence) is MatchLevel.WEAK


# ------------------------------------------------------------------ инициалы


@pytest.mark.parametrize(
    "raw",
    [
        "Тестов А.С.",
        "Тестов А. С.",
        "А.С. Тестов",
        # Организационная приставка — не часть имени.
        "ИП Тестов А.С.",
        "Индивидуальный предприниматель Тестов А.С.",
        # Отчества у второй стороны нет — сравнивать его не с чем.
        "Тестов А.",
    ],
)
def test_a_surname_with_initials_is_recognized_as_such(raw: str) -> None:
    """КАД сокращает участников, и это его форма записи, а не другой человек."""
    name = PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич")
    assert compare_names(name, raw) is NameMatch.INITIALS


@pytest.mark.parametrize(
    "raw",
    [
        # Инициалы чужие.
        "Тестов П.С.",
        # Совпало только отчество: «Тестов Ю.» — это скорее другой Тестов.
        "Тестов С.",
        # Фамилия чужая.
        "Петров А.С.",
        # Одна фамилия — это не человек.
        "Тестов",
        # Инициалов больше, чем частей имени.
        "Тестов А.С.П.",
        # Не инициалы, а сокращённая организация.
        "ООО ТАС",
    ],
)
def test_initials_that_do_not_line_up_are_not_a_match(raw: str) -> None:
    name = PersonName(last_name="Тестов", first_name="Андрей", middle_name="Сергеевич")
    assert compare_names(name, raw) is NameMatch.NONE


def test_initials_alone_never_reach_a_usable_match(matcher: IdentityMatcher) -> None:
    """Фамилия с инициалами — доказательство слабее имени, и стоит меньше.

    Без даты рождения и ИНН такая запись остаётся слабым совпадением: «Тестов
    А.С.» — это в том числе каждый однофамилец с теми же двумя буквами.
    """
    record = make_proceeding(name="Тестов А.С.", birth_date=None, confidence=0.0)
    assessment = matcher.assess(subject_for(birth_date=None), record)

    assert "фамилия совпадает, имя — по инициалам" in assessment.reasons
    assert match_level_for(assessment.confidence) is MatchLevel.WEAK


def test_initials_with_a_matching_birth_date_are_not_thrown_away(
    matcher: IdentityMatcher,
) -> None:
    """Зато вместе с датой рождения запись обязана дойти до отчёта.

    Пока сокращение читалось как «ФИО не совпадает», производство с той же датой
    рождения набирало 0.30 и выпадало из отчёта — источник назвал должника
    короче, чем мы ожидали, и это стоило нам находки.
    """
    record = make_proceeding(name="Тестов А.С.", confidence=0.0)
    assessment = matcher.assess(subject_for(), record)

    assert match_level_for(assessment.confidence) is MatchLevel.PROBABLE
    # И всё же не подтверждённое: инициалы отождествления не дают.
    assert match_level_for(assessment.confidence) is not MatchLevel.CONFIRMED


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


# ------------------------------------------------------------- VIN из запроса


def vin_subject(
    vin: str = "XTA1234567890ABCD",
    *,
    name: PersonName | None = None,
    birth_date: date | None = None,
    inn: str | None = None,
) -> SearchSubject:
    return SearchSubject(
        search_type=SearchType.VIN.value,
        vehicle=VehicleDescriptor(vin=vin),
        name=name,
        birth_date=birth_date,
        inn=inn,
    )


def test_a_vin_from_the_query_carries_a_record_with_no_other_identifier(
    matcher: IdentityMatcher,
) -> None:
    """Реестр залогов по VIN не отдаёт ни ИНН, ни даты рождения.

    Без этого правила уведомление о залоге ровно той машины, про которую
    спросили, оставалось слабым совпадением и не попадало в отчёт — при том что
    в запросе стоял точный семнадцатизначный идентификатор.
    """
    record = PledgeRecord(
        registration_number="2015-000-291842-833",
        pledgor_name="Игорь Юрьевич Семенов",
        vin="XTA1234567890ABCD",
    )
    assessment = matcher.assess(vin_subject(), record)

    assert match_level_for(assessment.confidence) is MatchLevel.CONFIRMED
    assert "совпадает VIN, по которому шёл поиск" in assessment.reasons


def test_a_record_about_another_vehicle_is_not_carried(matcher: IdentityMatcher) -> None:
    record = PledgeRecord(pledgor_name="Игорь Юрьевич Семенов", vin="XW8ZZZ61ZKG011111")
    assessment = matcher.assess(vin_subject(), record)

    assert match_level_for(assessment.confidence) is MatchLevel.WEAK


def test_a_vin_only_the_record_knows_proves_nothing(matcher: IdentityMatcher) -> None:
    """Пол уверенности даёт запрос, а не запись.

    Иначе любой источник, приложивший к записи VIN, поднимал бы её сам себе.
    """
    assessment = matcher.assess(subject_for(birth_date=None, name="Совсем Другой"), make_pledge())

    assert match_level_for(assessment.confidence) is MatchLevel.WEAK
    assert "совпадает VIN, по которому шёл поиск" not in assessment.reasons


def test_a_conflicting_birth_date_outranks_a_matching_vin(matcher: IdentityMatcher) -> None:
    """Машина та, человек другой — запись не выдаётся за должника."""
    subject = vin_subject(name=parse_fio("Тестов Андрей Сергеевич"), birth_date=date(1985, 3, 12))
    record = make_pledge()
    record.pledgor_birth_date = date(1970, 1, 1)

    assessment = matcher.assess(subject, record)

    assert "дата рождения не совпадает" in assessment.reasons
    assert match_level_for(assessment.confidence) is MatchLevel.WEAK


def test_a_conflicting_inn_outranks_a_matching_vin(matcher: IdentityMatcher) -> None:
    subject = vin_subject(inn="000000000000")
    record = PledgeRecord(pledgor_inn="770912345601", vin="XTA1234567890ABCD")

    assessment = matcher.assess(subject, record)

    assert "ИНН не совпадает" in assessment.reasons
    assert match_level_for(assessment.confidence) is MatchLevel.WEAK


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
