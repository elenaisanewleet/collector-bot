"""Structured logging.

Log records carry a fixed set of keys so they can be shipped somewhere and
queried. Secrets and raw personal data never reach this pipeline — callers pass
already-masked values.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping, MutableMapping
from typing import Any

import structlog

_SENSITIVE_KEYS = frozenset(
    {
        "token",
        "telegram_bot_token",
        "api_key",
        "newdb_api_key",
        "x-api-key",
        "password",
        "fedresurs_password",
        "authorization",
        "secret",
        "phone",
        "passport",
        # Приезжают в ответе о банкротстве (блок ``commmon``), не читаются ни
        # одним полем домена и вырезаются из сохраняемого тела. Здесь — на
        # случай, если что-то из этого попадёт в лог отладочной строкой.
        "snils",
        "birth_place",
        "residential_address",
        # Паспортные поля так, как их зовёт NewDB, плюс контейнеры, в которых они
        # приезжают целиком. Один `logger.info(..., params=payload)` вывалил бы
        # серию и номер мимо всякой маскировки.
        "seria",
        "number",
        "passport_series",
        "passport_number",
        "params",
        "payload",
        "json_body",
        "body",
        "raw",
        "raw_response",
        "dob",
        "birth_date",
        "lastname",
        "firstname",
        "secondname",
    }
)
_REDACTED = "<redacted>"
# Глубже трёх уровней логи не носят ничего осмысленного, а неограниченная
# рекурсия по чужой структуре — это способ уронить логгер на цикле.
_MAX_REDACT_DEPTH = 3


def _redact_sensitive(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Last-resort guard: redact anything that slipped through unmasked.

    Рекурсивно: чувствительное значение чаще приезжает вложенным (``params``
    внутри ``payload``), чем отдельным ключом верхнего уровня.
    """
    return _redact_mapping(event_dict, depth=0)


def _redact_mapping(
    event_dict: MutableMapping[str, Any], *, depth: int
) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if str(key).lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
            continue
        value = event_dict[key]
        if isinstance(value, Mapping) and depth < _MAX_REDACT_DEPTH:
            event_dict[key] = _redact_mapping(dict(value), depth=depth + 1)
    return event_dict


def configure_logging(level: str = "INFO", *, json_output: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )
    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact_sensitive,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    return logger
