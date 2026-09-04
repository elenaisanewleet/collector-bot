"""Monetary parsing and formatting.

Money is handled as :class:`~decimal.Decimal` end to end; floats are never used
for amounts.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

_THOUSAND_SEPARATORS = re.compile(r"[\s  ']")
_CURRENCY_NOISE = re.compile(r"(?i)(руб\.?|рублей|rub|₽)")
_NUMERIC = re.compile(r"^-?\d+(?:[.,]\d+)?$")

ZERO = Decimal("0")


def parse_amount(raw: str | float | int | Decimal | None) -> Decimal | None:
    """Parse an amount from arbitrary user or provider input.

    Returns ``None`` for anything that is not unambiguously a number, so callers
    can distinguish "no amount" from "zero".
    """
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))

    text = _CURRENCY_NOISE.sub("", raw)
    text = _THOUSAND_SEPARATORS.sub("", text).strip()
    if not text:
        return None
    # A comma is a decimal separator in Russian formatting.
    if text.count(",") == 1 and text.count(".") == 0:
        text = text.replace(",", ".")
    else:
        text = text.replace(",", "")
    if not _NUMERIC.match(text):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        return None


def format_amount(value: Decimal | None, *, currency: str = "₽") -> str:
    """Format an amount for display: ``38 400 ₽``."""
    if value is None:
        return "—"
    quantized = value.quantize(Decimal("1")) if value == value.to_integral_value() else value
    whole, _, frac = f"{quantized:f}".partition(".")
    sign = "-" if whole.startswith("-") else ""
    digits = whole.lstrip("-")
    grouped = " ".join(_chunks(digits))
    body = f"{sign}{grouped}"
    if frac:
        body = f"{body},{frac}"
    return f"{body} {currency}".strip()


def _chunks(digits: str) -> list[str]:
    reversed_chunks = [digits[max(i - 3, 0) : i] for i in range(len(digits), 0, -3)]
    return list(reversed(reversed_chunks))
