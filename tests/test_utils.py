"""Formatting, money, dates and hashing."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.utils.dates import format_date, format_datetime, parse_date, utcnow
from app.utils.formatting import (
    percent,
    pluralize_ru,
    signed,
    split_message,
    truncate,
)
from app.utils.hashing import normalize_token, stable_hash
from app.utils.money import format_amount, parse_amount


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.03.1985", date(1985, 3, 12)),
        ("12-03-1985", date(1985, 3, 12)),
        ("1985-03-12", date(1985, 3, 12)),
        ("12031985", date(1985, 3, 12)),
        # Реестры датируют записи временем: ФНП отдаёт дату регистрации
        # уведомления только как registrationTime.
        ("2015-01-29T16:40:08", date(2015, 1, 29)),
        ("2023-08-15T10:10:28.393", date(2023, 8, 15)),
        ("2025-12-09 20:21:09", date(2025, 12, 9)),
        ("31.02.1985", None),
        ("не дата", None),
        ("", None),
        (None, None),
    ],
)
def test_date_parsing(raw: str | None, expected: date | None) -> None:
    assert parse_date(raw) == expected


def test_future_dates_are_rejected() -> None:
    future = utcnow().date().replace(year=utcnow().year + 1)
    assert parse_date(future.strftime("%d.%m.%Y")) is None


def test_implausibly_old_dates_are_rejected() -> None:
    assert parse_date("01.01.1800") is None


def test_date_formatting() -> None:
    assert format_date(date(1985, 3, 12)) == "12.03.1985"
    assert format_date(None) == "—"


def test_datetime_formatting_assumes_utc_for_naive_values() -> None:
    naive = datetime(2026, 9, 4, 14, 32)
    aware = datetime(2026, 9, 4, 14, 32, tzinfo=UTC)
    assert format_datetime(naive) == format_datetime(aware) == "04.09.2026 14:32"


def test_utcnow_is_timezone_aware() -> None:
    assert utcnow().tzinfo is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("38400", Decimal("38400")),
        ("38 400", Decimal("38400")),
        ("38 400,50", Decimal("38400.50")),
        ("38400.50 руб.", Decimal("38400.50")),
        ("1 234 567 ₽", Decimal("1234567")),
        (38400, Decimal("38400")),
        ("не сумма", None),
        ("", None),
        (None, None),
    ],
)
def test_amount_parsing(raw: object, expected: Decimal | None) -> None:
    assert parse_amount(raw) == expected  # type: ignore[arg-type]


def test_zero_is_distinguishable_from_missing() -> None:
    assert parse_amount("0") == Decimal("0")
    assert parse_amount(None) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("38400"), "38 400 ₽"),
        (Decimal("1234567"), "1 234 567 ₽"),
        (Decimal("100"), "100 ₽"),
        (Decimal("612750.50"), "612 750,50 ₽"),
        (None, "—"),
    ],
)
def test_amount_formatting(value: Decimal | None, expected: str) -> None:
    assert format_amount(value) == expected


@pytest.mark.parametrize(
    ("count", "expected"),
    [
        (1, "производство"),
        (2, "производства"),
        (5, "производств"),
        (11, "производств"),
        (21, "производство"),
        (104, "производства"),
    ],
)
def test_russian_pluralization(count: int, expected: str) -> None:
    assert pluralize_ru(count, "производство", "производства", "производств") == expected


def test_signed_formatting() -> None:
    assert signed(10) == "+10"
    assert signed(-15) == "-15"
    assert signed(0) == "0"


def test_percent_formatting() -> None:
    assert percent(0.82) == "82%"
    assert percent(1.0) == "100%"


def test_truncate() -> None:
    assert truncate("abcdef", 4) == "abc…"
    assert truncate("abc", 10) == "abc"


def test_split_message_preserves_content() -> None:
    text = "\n\n".join(f"Блок {index}: {'x' * 300}" for index in range(40))
    chunks = split_message(text, limit=1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert "".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_split_message_handles_an_unbreakable_run() -> None:
    chunks = split_message("x" * 5000, limit=1000)
    assert all(len(chunk) <= 1000 for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) == 5000


def test_short_message_is_not_split() -> None:
    assert split_message("короткое сообщение") == ["короткое сообщение"]


def test_token_normalization_folds_case_and_yo() -> None:
    assert normalize_token("  Артём  ПЁТР ") == "артем петр"


def test_stable_hash_is_deterministic_across_calls() -> None:
    assert stable_hash("a", "b") == stable_hash("A", " b ")
    assert stable_hash("a", "b") != stable_hash("a", "c")
