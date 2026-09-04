"""Masking of personal identifiers.

Nothing in this module is reversible: masked values are what reaches logs, the
audit trail and the search history. Raw identifiers stay in memory for the
duration of a single search and are only persisted when the operator has
explicitly opted in via ``STORE_SENSITIVE_IDENTIFIERS``.
"""

from __future__ import annotations

import re

_DIGITS = re.compile(r"\D")
PHONE_VISIBLE_TAIL = 2
PASSPORT_SERIES_VISIBLE = 2


def mask_phone(phone: str | None) -> str | None:
    """``+79991234567`` -> ``+7 (999) ***-**-67``."""
    if not phone:
        return None
    digits = _DIGITS.sub("", phone)
    if len(digits) < 4:
        return "*" * len(digits) if digits else None
    tail = digits[-PHONE_VISIBLE_TAIL:]
    if len(digits) == 11 and digits[0] in {"7", "8"}:
        return f"+7 ({digits[1:4]}) ***-**-{tail}"
    return f"{'*' * (len(digits) - PHONE_VISIBLE_TAIL)}{tail}"


def mask_passport(passport: str | None) -> str | None:
    """``4509123456`` -> ``45** ******`` — only the region prefix survives."""
    if not passport:
        return None
    digits = _DIGITS.sub("", passport)
    if not digits:
        return None
    head = digits[:PASSPORT_SERIES_VISIBLE]
    series_stars = "*" * max(len(digits[:4]) - len(head), 0)
    number_stars = "*" * len(digits[4:])
    return f"{head}{series_stars} {number_stars}".strip()


def mask_name(full_name: str | None) -> str | None:
    """``Иванов Иван Иванович`` -> ``Иванов И. И.``."""
    if not full_name:
        return None
    parts = [part for part in full_name.split() if part]
    if not parts:
        return None
    head, *rest = parts
    initials = " ".join(f"{part[0].upper()}." for part in rest if part)
    return f"{head} {initials}".strip()


def mask_inn(inn: str | None) -> str | None:
    if not inn:
        return None
    digits = _DIGITS.sub("", inn)
    if len(digits) <= 4:
        return "*" * len(digits)
    return f"{digits[:2]}{'*' * (len(digits) - 4)}{digits[-2:]}"


def mask_vin(vin: str | None) -> str | None:
    """VIN keeps its last 4 characters, the part used for human confirmation."""
    if not vin:
        return None
    cleaned = vin.strip().upper()
    if len(cleaned) <= 4:
        return "*" * len(cleaned)
    return f"{'*' * (len(cleaned) - 4)}{cleaned[-4:]}"


def mask_secret(secret: str | None) -> str:
    """Used for tokens and API keys — never reveals more than a length hint."""
    if not secret:
        return "<unset>"
    return f"<set:{len(secret)} chars>"
