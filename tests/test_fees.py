"""Госпошлина по ст. 333.19 НК РФ.

Ставки заданы законом, поэтому тесты проверяют границы ступеней: именно там
ошибка в таблице даёт неверную цифру, на которую человек посмотрит и примет
решение.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.fees import MAX_FEE, claim_fee, court_order_fee


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        # первая ступень — фиксированные 4 000 ₽
        ("1", "4000"),
        ("100000", "4000"),
        # 4 000 + 3 % свыше 100 000
        ("100001", "4000"),
        ("154200", "5626"),
        ("300000", "10000"),
        # 10 000 + 2,5 % свыше 300 000
        ("400000", "12500"),
        ("500000", "15000"),
        # 15 000 + 2 % свыше 500 000
        ("612750.50", "17255"),
        ("1000000", "25000"),
        # 25 000 + 1 % свыше 1 000 000
        ("3000000", "45000"),
        # 45 000 + 0,7 % свыше 3 000 000
        ("8000000", "80000"),
        # 80 000 + 0,35 % свыше 8 000 000
        ("24000000", "136000"),
        # 136 000 + 0,3 % свыше 24 000 000
        ("50000000", "214000"),
        # 214 000 + 0,2 % свыше 50 000 000
        ("100000000", "314000"),
    ],
)
def test_claim_fee_brackets(amount: str, expected: str) -> None:
    assert claim_fee(Decimal(amount)) == Decimal(expected)


def test_fee_is_capped() -> None:
    """Верхняя ступень ограничена 900 000 ₽."""
    assert claim_fee(Decimal("100000000000")) == MAX_FEE


def test_fee_is_monotonic_across_bracket_edges() -> None:
    """На стыке ступеней пошлина не должна падать."""
    edges = [100_000, 300_000, 500_000, 1_000_000, 3_000_000, 8_000_000, 24_000_000]
    for edge in edges:
        below = claim_fee(Decimal(edge))
        above = claim_fee(Decimal(edge + 1))
        assert above >= below, f"пошлина падает на границе {edge}"


def test_zero_and_negative_cost_nothing() -> None:
    assert claim_fee(Decimal("0")) == Decimal("0")
    assert claim_fee(Decimal("-100")) == Decimal("0")


def test_court_order_is_half_of_the_claim_fee() -> None:
    assert court_order_fee(Decimal("38400")) == Decimal("2000")
    assert court_order_fee(Decimal("154200")) == Decimal("2813")


def test_fee_is_whole_rubles() -> None:
    """Сбор исчисляется в полных рублях (п. 6 ст. 52 НК РФ)."""
    for amount in ("154200", "612750.50", "233333.33"):
        assert claim_fee(Decimal(amount)) % 1 == 0
        assert court_order_fee(Decimal(amount)) % 1 == 0
