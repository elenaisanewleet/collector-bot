"""Declarative base and portable column types.

The MVP runs on SQLite while the target deployment is PostgreSQL, so the two
types that behave differently across those backends — money and timestamps — are
wrapped rather than left to the driver.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import Dialect, String, TypeDecorator
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Shared declarative base for every table."""


class UtcDateTime(TypeDecorator[datetime]):
    """Timestamps that stay timezone-aware in UTC on every backend.

    SQLite has no timezone-aware type, so a naive value read back would compare
    incorrectly against ``datetime.now(tz=utc)`` — the exact bug that makes cache
    expiry silently wrong. Storing ISO-8601 text sidesteps it.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=UTC)
        try:
            parsed = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class Money(TypeDecorator[Decimal]):
    """Exact decimal amounts, stored as text.

    SQLite would round-trip ``Numeric`` through a float and lose kopecks.
    """

    impl = String(32)
    cache_ok = True

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> str | None:
        if value is None:
            return None
        return str(Decimal(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
