"""Masking of personal identifiers.

Nothing in this module is reversible: masked values are what reaches logs, the
audit trail and the search history. Raw identifiers stay in memory for the
duration of a single search and are only persisted when the operator has
explicitly opted in via ``STORE_SENSITIVE_IDENTIFIERS``.
"""

from __future__ import annotations

import json
import re
from typing import Any

_DIGITS = re.compile(r"\D")
PHONE_VISIBLE_TAIL = 2
PASSPORT_SERIES_VISIBLE = 2
SNILS_VISIBLE_TAIL = 2

REDACTED = "[удалено]"

#: Ключи чужих ответов, которые не должны оседать в базе ни при каком флаге.
#:
#: Список закрытый и назван по именам ключей вендора, потому что вырезание идёт
#: до всякого разбора: сырое тело сохраняется как есть (``search_results.
#: raw_response``), и карта полей на него не влияет. Что здесь лежит и откуда:
#:
#: ``snils``               ``bankrot_person`` -> ``commmon.snils``. Ни одно поле
#:                         домена его не читает, и читать не должно.
#: ``birth_place``,        там же. Место рождения и адрес проживания должника —
#: ``place_of_birth``,     сведения, которых взысканию не нужно; живой ответ
#: ``residential_address`` ``arbitr_person`` кладёт домашний адрес ещё и в
#:                         ``ai_interpretation.debtor_info.place_of_birth``.
#: ``passport``,           на случай методов по паспорту: если такой ответ когда-
#: ``passport_number``,    нибудь окажется в этом же хранилище, он не должен
#: ``passport_series``     попасть туда молча.
SENSITIVE_RAW_KEYS: frozenset[str] = frozenset(
    {
        "snils",
        "birth_place",
        "place_of_birth",
        "residential_address",
        "passport",
        "passport_number",
        "passport_series",
    }
)


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


def mask_snils(snils: str | None) -> str | None:
    """``106-556-061 42`` -> ``***-***-*** 42``.

    Exists so that a СНИЛС which somehow reaches a log line is unusable there.
    Nothing in this tool reads a СНИЛС as data: the live ``bankrot_person``
    answer carries one in ``commmon.snils``, no field map points at it, and
    :func:`redact_sensitive_json` removes it from the stored body. This is the
    last barrier, not the first one.
    """
    if not snils:
        return None
    digits = _DIGITS.sub("", snils)
    if not digits:
        return None
    tail = digits[-SNILS_VISIBLE_TAIL:] if len(digits) > SNILS_VISIBLE_TAIL else ""
    return f"***-***-*** {tail}".strip()


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


def redact_sensitive_json(raw: str | None) -> str | None:
    """Strip :data:`SENSITIVE_RAW_KEYS` out of a vendor body before it is stored.

    ``STORE_RAW_RESPONSES=true`` is the documented way to check a field map
    against the first real debtor, and the live ``bankrot_person`` answer turned
    out to carry a СНИЛС, a place of birth and a home address in ``commmon``.
    Storing them would be collecting data about somebody else's client that this
    tool has no use for and no map pointing at.

    The keys are removed, not blanked to ``null``: the point of a stored body is
    to show what the vendor sent, and a key with a plausible ``null`` in it
    reads as "the vendor sent nothing here". A marker key
    (``_redacted_fields``) says what was taken out instead.

    Deliberately unconditional — it is not behind ``STORE_SENSITIVE_IDENTIFIERS``.
    That flag governs *our own* debtors' identifiers, which the operator entered
    and may need back. Nobody entered these, and nothing reads them.

    A body that is not JSON is left alone: the alternative is a regex over an
    unknown format, and a half-redacted blob is worse than an honest one.
    """
    if not raw:
        return raw
    documents: list[Any] = []
    for line in raw.split("\n"):
        if not line.strip():
            documents.append(line)
            continue
        try:
            documents.append(_redact_node(json.loads(line)))
        except json.JSONDecodeError:
            return raw
    return "\n".join(
        item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)
        for item in documents
    )


def _redact_node(node: Any) -> Any:
    if isinstance(node, dict):
        removed = sorted(key for key in node if key.lower() in SENSITIVE_RAW_KEYS)
        cleaned: dict[str, Any] = {
            key: _redact_node(value) for key, value in node.items() if key not in removed
        }
        if removed:
            cleaned["_redacted_fields"] = removed
        return cleaned
    if isinstance(node, list):
        return [_redact_node(item) for item in node]
    return node
