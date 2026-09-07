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


#: Порог, с которого сумму стоит округлять до миллионов. Ниже него точная
#: запись ещё читается с одного взгляда: «980 400 ₽» — это семь знаков.
_MILLION = Decimal("1000000")
_BILLION = Decimal("1000000000")


def format_compact_amount(value: Decimal | None, *, currency: str = "₽") -> str:
    """Крупная сумма коротко: ``13,7 млн ₽``.

    Для итогов, а не для расчётов. «13 679 650 ₽» на первом экране — это
    восемь цифр, которые человек всё равно прочитает как «около четырнадцати
    миллионов»; короткая запись говорит то же самое и не спорит с соседними
    строками за ширину. Везде, где сумма участвует в решении — цена иска,
    пошлина, долг конкретного человека, — остаётся :func:`format_amount`:
    округлять деньги, по которым подают в суд, нельзя.
    """
    if value is None:
        return "—"
    magnitude = abs(value)
    if magnitude < _MILLION:
        return format_amount(value, currency=currency)
    unit, scale = ("млрд", _BILLION) if magnitude >= _BILLION else ("млн", _MILLION)
    scaled = (value / scale).quantize(Decimal("0.1"))
    # Целое печатается без хвоста: «14 млн», а не «14,0 млн».
    whole = scaled == scaled.to_integral_value()
    text = format(scaled.to_integral_value() if whole else scaled, "f")
    return f"{text.replace('.', ',')} {unit} {currency}".strip()


def _chunks(digits: str) -> list[str]:
    reversed_chunks = [digits[max(i - 3, 0) : i] for i in range(len(digits), 0, -3)]
    return list(reversed(reversed_chunks))
