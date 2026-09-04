"""Date parsing and formatting helpers.

All user-facing dates use the Russian ``DD.MM.YYYY`` convention.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

DISPLAY_DATE_FORMAT = "%d.%m.%Y"
DISPLAY_DATETIME_FORMAT = "%d.%m.%Y %H:%M"

_ACCEPTED_DATE_FORMATS = ("%d.%m.%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d")
_DIGITS_ONLY = re.compile(r"^\d{8}$")

MIN_PLAUSIBLE_YEAR = 1900
MAX_PLAUSIBLE_AGE_YEARS = 120


def utcnow() -> datetime:
    """Timezone-aware ``now`` in UTC.

    ``datetime.utcnow()`` is deprecated and returns a naive value, which then
    silently breaks comparisons against stored timezone-aware timestamps.
    """
    return datetime.now(tz=UTC)


def parse_date(raw: str | None) -> date | None:
    """Parse a user-supplied date, returning ``None`` when it is unusable.

    Accepts the display format plus a few common variants. Implausible values
    (far future, absurd years) are rejected rather than guessed at.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if _DIGITS_ONLY.match(text):
        text = f"{text[:2]}.{text[2:4]}.{text[4:]}"
    for fmt in _ACCEPTED_DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, fmt).date()
        except ValueError:
            continue
        if _is_plausible(parsed):
            return parsed
        return None
    return _parse_iso_datetime(text)


def _parse_iso_datetime(text: str) -> date | None:
    """ISO 8601 with a time part: ``2015-01-29T16:40:08``.

    Registries hand out timestamps where a date is meant — the ФНП pledge
    register dates its notices only as ``json_extra.registrationTime``. Dropping
    them left the registration date empty, and an undated notice loses its
    status with it: "зарегистрирован, не исключён" is exactly what makes a
    pledge count as active.
    """
    try:
        parsed = datetime.fromisoformat(text).date()
    except ValueError:
        return None
    return parsed if _is_plausible(parsed) else None


def _is_plausible(value: date) -> bool:
    today = utcnow().date()
    if value.year < MIN_PLAUSIBLE_YEAR:
        return False
    if value > today:
        return False
    return (today.year - value.year) <= MAX_PLAUSIBLE_AGE_YEARS


def format_date(value: date | None) -> str:
    return value.strftime(DISPLAY_DATE_FORMAT) if value else "—"


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.strftime(DISPLAY_DATETIME_FORMAT)


def iso_or_none(value: date | None) -> str | None:
    return value.isoformat() if value else None
