"""Masking and log hygiene.

Nothing sensitive may appear in full in a log record, an audit row or the search
history.
"""

from __future__ import annotations

import json

import pytest

from app.services.search import describe_subject, redact_subject
from app.utils.hashing import stable_hash
from app.utils.masking import (
    mask_inn,
    mask_name,
    mask_passport,
    mask_phone,
    mask_secret,
    mask_snils,
    mask_vin,
    redact_sensitive_json,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+79991234567", "+7 (999) ***-**-67"),
        ("89991234567", "+7 (999) ***-**-67"),
        (None, None),
        ("", None),
    ],
)
def test_phone_masking(raw: str | None, expected: str | None) -> None:
    assert mask_phone(raw) == expected


def test_masked_phone_never_contains_the_middle_digits() -> None:
    masked = mask_phone("+79991234567")
    assert masked is not None
    assert "1234567" not in masked
    assert "12345" not in masked


def test_passport_masking_keeps_only_the_region_prefix() -> None:
    masked = mask_passport("4509123456")
    assert masked == "45** ******"
    assert "123456" not in masked


def test_passport_masking_of_none() -> None:
    assert mask_passport(None) is None


def test_name_masking_reduces_to_initials() -> None:
    assert mask_name("Иванов Иван Иванович") == "Иванов И. И."


def test_inn_masking() -> None:
    masked = mask_inn("770912345601")
    assert masked is not None
    assert masked.startswith("77")
    assert masked.endswith("01")
    assert "0912345" not in masked


def test_vin_masking_keeps_the_confirmation_tail() -> None:
    masked = mask_vin("XW8ZZZ61ZKG011111")
    assert masked == "*************1111"
    assert "XW8ZZZ" not in masked


def test_secret_masking_never_reveals_the_value() -> None:
    assert mask_secret("super-secret-token") == "<set:18 chars>"
    assert mask_secret("") == "<unset>"
    assert "super" not in mask_secret("super-secret-token")


# ---------------------------------------------------------------- history


def test_history_label_masks_the_phone(person_subject) -> None:  # type: ignore[no-untyped-def]
    subject = person_subject.model_copy(update={"phone": "+79991234567"})
    label = describe_subject(subject)
    assert "+79991234567" not in label
    assert "Тестов А. С." in label


def test_history_label_masks_the_passport() -> None:
    from app.domain.enums import SearchType
    from app.domain.identity import SearchSubject

    subject = SearchSubject(search_type=SearchType.PASSPORT.value, passport="4509123456")
    label = describe_subject(subject)
    assert "4509123456" not in label
    assert label.startswith("45")


def test_stored_subject_drops_sensitive_identifiers(person_subject) -> None:  # type: ignore[no-untyped-def]
    subject = person_subject.model_copy(update={"phone": "+79991234567", "passport": "4509123456"})
    payload = redact_subject(subject, store_sensitive=False)
    assert "phone" not in payload
    assert "passport" not in payload
    assert "4509123456" not in str(payload)


def test_stored_subject_keeps_identifiers_when_opted_in(person_subject) -> None:  # type: ignore[no-untyped-def]
    subject = person_subject.model_copy(update={"phone": "+79991234567"})
    payload = redact_subject(subject, store_sensitive=True)
    assert payload["phone"] == "+79991234567"


def test_query_hash_is_one_way_and_stable() -> None:
    first = stable_hash("person", "иванов иван", "1985-03-12")
    second = stable_hash("person", "Иванов Иван", "1985-03-12")
    assert first == second
    assert "иванов" not in first
    assert len(first) == 32


def test_redaction_survives_a_round_trip(person_subject) -> None:  # type: ignore[no-untyped-def]
    import json

    from app.services.search import subject_from_json

    subject = person_subject.model_copy(update={"phone": "+79991234567"})
    payload = json.dumps(redact_subject(subject, store_sensitive=False))
    restored = subject_from_json(payload)

    assert restored is not None
    assert restored.name == subject.name
    assert restored.phone is None


# --------------------------------------------------- сырые ответы вендоров


def test_snils_masking_keeps_only_the_check_digits() -> None:
    masked = mask_snils("106-556-061 42")
    assert masked == "***-***-*** 42"
    assert "106" not in masked
    assert "556" not in masked


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_snils_masking_of_nothing(raw: str | None) -> None:
    assert mask_snils(raw) is None


def test_a_stored_vendor_body_carries_no_snils() -> None:
    """СНИЛС вырезается из сырого тела до всякого хранения.

    Живой ответ ``bankrot_person`` привозит его в ``commmon`` вместе с местом
    рождения и адресом проживания. Ни одно из трёх не нужно ни отчёту, ни
    скорингу, ни одному пути карты полей — а ``STORE_RAW_RESPONSES`` включают,
    чтобы сверить карту, а не чтобы собрать чужие персональные данные.
    """
    body = json.dumps(
        {
            "results": {
                "bankrot_person": {
                    "result": {
                        "data": [
                            {
                                "commmon": {
                                    "name_or_fio": "Пыжова Анна Петровна",
                                    "inn": "270311112222",
                                    "snils": "111-222-333 44",
                                    "birth_date": "14.03.1979",
                                    "birth_place": "гор. Приморск",
                                    "residential_address": "680000, г. Приморск",
                                },
                                "bankruptcy": [{"case_number": "А73-1111/2017"}],
                            }
                        ]
                    }
                }
            }
        },
        ensure_ascii=False,
    )

    redacted = redact_sensitive_json(body)

    assert redacted is not None
    assert "111-222-333 44" not in redacted
    assert "Приморск" not in redacted
    # Всё остальное сохраняется дословно: тело хранят ради сверки карты.
    assert "А73-1111/2017" in redacted
    assert "Пыжова Анна Петровна" in redacted
    common = json.loads(redacted)["results"]["bankrot_person"]["result"]["data"][0]["commmon"]
    # Дата рождения остаётся: её читает сопоставление личности.
    assert common["birth_date"] == "14.03.1979"
    # Ключа нет вовсе, а не ``null``: «вендор ничего не прислал» — другое
    # утверждение, и оно было бы неправдой.
    assert "snils" not in common
    assert common["_redacted_fields"] == ["birth_place", "residential_address", "snils"]


def test_several_bodies_on_separate_lines_are_all_redacted() -> None:
    """Провайдер залогов склеивает два ответа через перевод строки."""
    first = json.dumps({"commmon": {"snils": "111-222-333 44"}}, ensure_ascii=False)
    second = json.dumps({"commmon": {"snils": "555-666-777 88"}}, ensure_ascii=False)

    redacted = redact_sensitive_json(f"{first}\n{second}")

    assert redacted is not None
    assert "111-222-333 44" not in redacted
    assert "555-666-777 88" not in redacted
    assert len(redacted.split("\n")) == 2


def test_a_body_that_is_not_json_is_left_alone() -> None:
    """Полу-вырезанный блоб хуже честного: регексп по неизвестному формату — нет."""
    assert redact_sensitive_json("<html>503</html>") == "<html>503</html>"
    assert redact_sensitive_json(None) is None
