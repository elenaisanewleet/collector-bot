"""Государственная пошлина.

Ставки по п. 1 ст. 333.19 НК РФ в редакции Федерального закона от 08.08.2024
№ 259-ФЗ — она применяется к делам, возбуждённым после 08.09.2024.

Таблица вынесена в отдельный модуль намеренно: ставки меняются законом, и
правка одного списка не должна задевать логику принятия решения. При изменении
редакции НК проверьте и таблицу, и порог судебного приказа в настройках.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Final

# (верхняя граница цены иска включительно | None, базовая часть, ставка, вычет)
# Пошлина = base + rate * (цена иска - over).
FEE_BRACKETS: Final[tuple[tuple[Decimal | None, Decimal, Decimal, Decimal], ...]] = (
    (Decimal("100000"), Decimal("4000"), Decimal("0"), Decimal("0")),
    (Decimal("300000"), Decimal("4000"), Decimal("0.03"), Decimal("100000")),
    (Decimal("500000"), Decimal("10000"), Decimal("0.025"), Decimal("300000")),
    (Decimal("1000000"), Decimal("15000"), Decimal("0.02"), Decimal("500000")),
    (Decimal("3000000"), Decimal("25000"), Decimal("0.01"), Decimal("1000000")),
    (Decimal("8000000"), Decimal("45000"), Decimal("0.007"), Decimal("3000000")),
    (Decimal("24000000"), Decimal("80000"), Decimal("0.0035"), Decimal("8000000")),
    (Decimal("50000000"), Decimal("136000"), Decimal("0.003"), Decimal("24000000")),
    (Decimal("100000000"), Decimal("214000"), Decimal("0.002"), Decimal("50000000")),
    (None, Decimal("314000"), Decimal("0.0015"), Decimal("100000000")),
)

# Потолок для верхней ступени.
MAX_FEE: Final = Decimal("900000")

# Заявление о вынесении судебного приказа — 50 % от исковой пошлины
# (пп. 2 п. 1 ст. 333.19 НК РФ).
COURT_ORDER_FEE_RATIO: Final = Decimal("0.5")


def claim_fee(amount: Decimal) -> Decimal:
    """Пошлина по имущественному иску, подлежащему оценке.

    Округляется до полного рубля: сумма сбора исчисляется в полных рублях
    (п. 6 ст. 52 НК РФ).
    """
    if amount <= 0:
        return Decimal("0")

    for upper, base, rate, over in FEE_BRACKETS:
        if upper is None or amount <= upper:
            fee = base + rate * (amount - over)
            return _to_whole_rubles(min(fee, MAX_FEE))

    # Недостижимо: последняя ступень открыта сверху.
    raise AssertionError("fee brackets must end with an open bracket")


def court_order_fee(amount: Decimal) -> Decimal:
    """Пошлина за заявление о вынесении судебного приказа."""
    return _to_whole_rubles(claim_fee(amount) * COURT_ORDER_FEE_RATIO)


def _to_whole_rubles(value: Decimal) -> Decimal:
    return value.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
