"""NewDB — общий транспорт агрегатора.

`fssp_person` was the first method wired up here, and the envelope it speaks is
the same for every method NewDB exposes. That envelope therefore lives in this
module, and each per-method adapter carries nothing but the parameters it sends
and the rows it reads back.

The split between what is hard-coded here and what is configuration is
deliberate, and it follows the same rule as the rest of this project: *code may
encode what has been verified; everything else is supplied by the deployment.*

**Verified, and therefore hard-coded** — the transport, as published at
https://newdb.net/docs/ and in the OpenAPI document at
https://newdb.net/swagger/openapi.json:

*   ``POST {NEWDB_BASE_URL}/v2`` with ``X-API-KEY``; the body is
    ``{"params": {...}, "requestId": "..."}`` and ``method`` lives *inside*
    ``params``.
*   The call is asynchronous. The envelope carries ``state``: ``queued``,
    ``in_progress`` and ``restart`` mean keep waiting; ``complete`` and
    ``failed`` are terminal. Polling is a repeat POST to the same endpoint with
    the same ``requestId``, which keeps the token out of query strings and
    therefore out of access logs.
*   Rows arrive at ``results.<method>.result.data``.
*   **A rejected or missing token comes back as HTTP 200 with
    ``state: "failed"``**, not as 401/403. Read naively that is an empty result
    — a clean report for a debtor nobody checked. Every terminal ``failed`` is
    mapped to an error status, and a ``complete`` envelope without the expected
    result path is ``unexpected_schema``, never ``NO_RESULTS``.

**Not verified, and therefore configuration** — the shape of the rows each
method returns. Only ``fssp_person`` has been read against a real response. The
rows of the other methods are described in ``config/field_maps/example_newdb.json``
from the vendor's own documentation, recovered from a web-archive snapshot of
07.02.2026 (the live docs site is gone); that is a good deal better than
invented names and still not the same thing as a response. So the row schema
comes from a field map (``NEWDB_FIELD_MAP``), the same mechanism the ЕФРСБ and
ФНС vendor adapters already use, and a method with no entry in that map stays
``NOT_CONFIGURED``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.domain.identity import INN_INDIVIDUAL_LENGTH, SearchSubject
from app.logging_setup import get_logger
from app.providers.base import BaseProvider, ProviderError, ProviderUnavailableError
from app.providers.http import RetryPolicy, build_client, request_json
from app.providers.mapping import (
    FieldMap,
    FieldMapError,
    MappedRows,
    RecordDict,
    as_text,
    dig,
    has_any_value,
)

logger = get_logger(__name__)

COUNTRY_RU = "ru"
# The parameter name ФССП uses for a date of birth. ``pledge_person`` documents
# a different one; see ``person_params``.
DOB_KEY = "dob"

STATE_COMPLETE = "complete"
STATE_FAILED = "failed"
PENDING_STATES = frozenset({"queued", "in_progress", "restart"})

# The service reports a bad key and an empty balance through one message; both
# are actionable by the operator and neither is retryable.
AUTH_ERROR_MARKERS = ("токен", "token", "баланс", "balance", "x-api-key")
ERROR_CODE_PAYMENT_REQUIRED = 402
ERROR_CODE_BAD_REQUEST = 400


def result_data_path(method: str) -> str:
    return f"results.{method}.result.data"


@dataclass(frozen=True, slots=True)
class NewDBResponse:
    """Rows from one or more calls of the same method, plus the raw bodies."""

    rows: list[Any]
    raw: str


class NewDBClient:
    """Speaks the NewDB envelope. Knows nothing about any particular method."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def is_configured(self) -> bool:
        return bool(self._settings.newdb_api_key and self._settings.newdb_base_url)

    async def call(self, method: str, *param_sets: Mapping[str, Any]) -> NewDBResponse:
        """Run ``method`` once per parameter set, on one shared connection.

        Several parameter sets exist for the one case that genuinely needs them:
        ФССП searches Moscow and the region separately. Rows are concatenated;
        deduplication is the caller's business, because only the caller knows
        what makes two rows the same record.
        """
        retry = RetryPolicy(
            max_retries=self._settings.provider_max_retries,
            backoff_seconds=self._settings.provider_retry_backoff_seconds,
        )
        rows: list[Any] = []
        raw_bodies: list[str] = []

        async with build_client(
            base_url=self._settings.newdb_base_url,
            timeout_seconds=self._settings.request_timeout_seconds,
            headers={"X-API-KEY": self._settings.newdb_api_key},
        ) as client:
            for params in param_sets:
                envelope, raw = await self._run(client, method, params, retry)
                raw_bodies.append(raw)
                rows.extend(_extract_rows(envelope, method))

        return NewDBResponse(rows=rows, raw="\n".join(raw_bodies))

    # ------------------------------------------------------------ envelope

    async def _run(
        self,
        client: httpx.AsyncClient,
        method: str,
        params: Mapping[str, Any],
        retry: RetryPolicy,
    ) -> tuple[Any, str]:
        payload = _build_payload(method, params)
        envelope, raw = await self._post(client, method, payload, retry)

        state = _state_of(envelope)
        if state == STATE_COMPLETE:
            return envelope, raw
        if state == STATE_FAILED:
            raise _failure_error(envelope)
        if state not in PENDING_STATES:
            # An envelope with no recognizable state is not an empty result.
            raise ProviderUnavailableError(
                "unexpected_schema", "Ответ NewDB не содержит поля state"
            )
        return await self._poll(client, method, payload, retry)

    async def _poll(
        self,
        client: httpx.AsyncClient,
        method: str,
        payload: Mapping[str, Any],
        retry: RetryPolicy,
    ) -> tuple[Any, str]:
        """Re-POST the same requestId until the task settles or the budget ends."""
        for _attempt in range(self._settings.newdb_poll_attempts):
            await asyncio.sleep(self._settings.newdb_poll_interval_seconds)
            envelope, raw = await self._post(client, method, payload, retry)
            state = _state_of(envelope)
            if state == STATE_COMPLETE:
                return envelope, raw
            if state == STATE_FAILED:
                raise _failure_error(envelope)

        logger.info(
            "newdb.poll_timeout", method=method, attempts=self._settings.newdb_poll_attempts
        )
        raise ProviderUnavailableError("poll_timeout", "NewDB не успела подготовить результат")

    async def _post(
        self,
        client: httpx.AsyncClient,
        method: str,
        payload: Mapping[str, Any],
        retry: RetryPolicy,
    ) -> tuple[Any, str]:
        return await request_json(
            client,
            "POST",
            self._settings.newdb_method_path,
            json_body=payload,
            retry=retry,
            provider=f"newdb:{method}",
        )


def _build_payload(method: str, params: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "params": {"method": method, **params},
        # Carried through polling: the same id addresses the same task.
        "requestId": str(uuid.uuid4()),
    }


def _state_of(envelope: Any) -> str:
    state = as_text(dig(envelope, "state"))
    return state.lower() if state else ""


def _errors_info(envelope: Any) -> list[Mapping[str, Any]]:
    node = dig(envelope, "errors_info")
    if not isinstance(node, list):
        return []
    return [item for item in node if isinstance(item, Mapping)]


def _failure_error(envelope: Any) -> ProviderError:
    """Translate a terminal ``failed`` envelope into a typed provider error.

    Never a ``NO_RESULTS``: the task did not run, so there is nothing to report
    as absent.
    """
    errors = _errors_info(envelope)
    messages = [text for item in errors if (text := as_text(item.get("error")))]
    message = "; ".join(messages) or as_text(dig(envelope, "error")) or "NewDB отклонила запрос"
    return ProviderError(_failure_code(errors, message), message)


def _failure_code(errors: list[Mapping[str, Any]], message: str) -> str:
    codes = {item.get("error_code") for item in errors}
    if ERROR_CODE_PAYMENT_REQUIRED in codes:
        return "payment_required"
    lowered = message.lower()
    if any(marker in lowered for marker in AUTH_ERROR_MARKERS):
        # A rejected key and an exhausted balance share one message here, and
        # both mean the same thing operationally: the source was not queried.
        return "unauthorized"
    if ERROR_CODE_BAD_REQUEST in codes:
        return "bad_request"
    return "request_failed"


def _extract_rows(envelope: Any, method: str) -> list[Any]:
    """Read the rows of a completed envelope.

    A missing result path on a ``complete`` envelope is a schema problem, not an
    empty result, and is reported as such.
    """
    path = result_data_path(method)
    rows = dig(envelope, path)
    if rows is None:
        raise ProviderUnavailableError("unexpected_schema", f"В ответе NewDB нет раздела {path}")
    if not isinstance(rows, list):
        raise ProviderUnavailableError("unexpected_schema", f"{path} не является списком")
    return rows


# ---------------------------------------------------------------- field maps


@dataclass(frozen=True, slots=True)
class MethodMap:
    """How to read one method's rows, and what to add to its request."""

    method: str
    field_map: FieldMap
    extra_params: Mapping[str, Any] = field(default_factory=dict)

    def apply(self, rows: Iterable[Any]) -> MappedRows:
        """Map every row of ``data``, unwrapping the nested array if one is named.

        Half the methods answer with a *container per subject* rather than with
        records: ``pledge_person`` keeps the notices in ``fnp``,
        ``bankrot_person`` the cases in ``bankruptcy``. ``records_path`` names
        that array **inside one row of ``data``**, so a response carrying two
        subjects loses neither — which an absolute path from ``data`` (``0.fnp``)
        would.
        """
        records: list[RecordDict] = []
        unreadable = 0
        for row in rows:
            if not isinstance(row, Mapping):
                unreadable += 1
                continue
            nested = self.field_map.extract_records(row)
            mapped = [
                record
                for record in (self.field_map.apply(item) for item in nested)
                if has_any_value(record)
            ]
            if mapped:
                records.extend(mapped)
                continue
            if nested or self._path_missing(row):
                # Либо строки внутри есть, но карта не нашла в них ни одного
                # поля, либо названного картой массива в строке нет вовсе. И то
                # и другое значит «не разобрано», а не «ничего не найдено».
                unreadable += 1
        return MappedRows(records=records, unreadable=unreadable)

    def _path_missing(self, row: Mapping[str, Any]) -> bool:
        """A named array that is absent, as opposed to present and empty.

        ``"fnp": []`` is an answer — no notices. A row with no ``fnp`` at all is
        a row shaped differently from what the map describes.
        """
        path = self.field_map.records_path
        return bool(path) and dig(row, path) is None


class NewDBFieldMaps:
    """Row maps for NewDB methods, keyed by method name.

    A method present in the file is a method the deployment has confirmed
    against its own NewDB contract. A method absent from it is a method this
    tool will not pretend to understand.
    """

    def __init__(self, maps: Mapping[str, MethodMap] | None = None) -> None:
        self._maps = dict(maps or {})

    @classmethod
    def load(cls, path: Path | None) -> NewDBFieldMaps:
        """Read the map file. No path configured means no methods enabled."""
        if path is None:
            return cls()
        payload = _read_json_object(path)
        maps: dict[str, MethodMap] = {}
        for method, entry in payload.items():
            if method.startswith("_"):  # comment keys
                continue
            maps[method] = _method_map(path, method, entry)
        return cls(maps)

    def __contains__(self, method: str) -> bool:
        return method in self._maps

    @property
    def methods(self) -> frozenset[str]:
        return frozenset(self._maps)

    def get(self, method: str) -> MethodMap | None:
        return self._maps.get(method)

    def require(self, method: str) -> MethodMap:
        mapping = self._maps.get(method)
        if mapping is None:
            raise FieldMapError(f"метод NewDB {method!r} не описан в NEWDB_FIELD_MAP")
        return mapping


def _read_json_object(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FieldMapError(f"cannot read field map {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FieldMapError(f"field map {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FieldMapError(f"field map {path} must be a JSON object keyed by NewDB method")
    return payload


def _method_map(path: Path, method: str, entry: Any) -> MethodMap:
    if not isinstance(entry, Mapping):
        raise FieldMapError(f"{path}: запись метода {method!r} должна быть объектом")
    fields = entry.get("fields")
    if not isinstance(fields, Mapping) or not fields:
        # An empty map would map every row onto an all-None record: not an
        # integration, just a source that always answers "nothing known".
        raise FieldMapError(f"{path}: у метода {method!r} нет непустого раздела fields")
    extra = entry.get("extra_params", {})
    if not isinstance(extra, Mapping):
        raise FieldMapError(f"{path}: extra_params метода {method!r} должны быть объектом")
    return MethodMap(
        method=method,
        field_map=FieldMap(
            # Rows of ``data`` are already extracted from the envelope by the
            # client, so ``records_path`` starts *inside one of them*: it names
            # the nested array a method wraps its records in (``fnp``,
            # ``bankruptcy``). Absent, the row itself is the record.
            records_path=str(entry.get("records_path", "")),
            fields={str(key): str(value) for key, value in fields.items()},
            value_maps=entry.get("value_maps", {}),
        ),
        extra_params=dict(extra),
    )


def person_params(
    *,
    last_name: str,
    first_name: str,
    middle_name: str | None,
    birth_date: str,
    birth_date_key: str = DOB_KEY,
    country: str = COUNTRY_RU,
) -> dict[str, Any]:
    """The person block NewDB's ``*_person`` methods take.

    Named after ``fssp_person``, whose parameters were read from the published
    contract. The sibling person methods take the same block; a deployment whose
    contract differs adds or overrides keys through ``extra_params`` in the field
    map rather than through a code change.

    The shape below was checked against the live endpoint: an unauthenticated
    ``POST /v2`` validates its parameters before it looks at the key, so the
    contract can be read off the rejections without spending a call.

    ``birth_date_key`` exists because the two person methods disagree about it:
    ``fssp_person`` documents ``dob``, ``pledge_person`` documents ``datebirth``
    in all four places it mentions the parameter. Which name the live service
    accepts for ``pledge_person`` has *not* been checked — the docs are the only
    source left — so the difference is spelled out here rather than hidden.
    """
    params: dict[str, Any] = {
        "country": country,
        "lastname": last_name,
        "firstname": first_name,
        birth_date_key: birth_date,
    }
    # A missing patronymic omits the key. Sending it empty is what the service
    # actually rejects — ``secondname must be non-empty`` — so the earlier
    # reading, that the key must always be present, had it backwards and would
    # have failed every request for a debtor without one.
    if middle_name:
        params["secondname"] = middle_name
    return params


def person_params_for(subject: SearchSubject, *, birth_date_key: str = DOB_KEY) -> dict[str, Any]:
    """The person block for a subject the caller has already checked.

    Each method is sent the smallest set that identifies the subject *for that
    method*, rather than everything known about them. Two reasons: a parameter
    the contract does not expect can be rejected outright, and ``extra_params``
    can add a key but never take one away.
    """
    assert subject.name is not None and subject.birth_date is not None
    return person_params(
        last_name=subject.name.last_name,
        first_name=subject.name.first_name,
        middle_name=subject.name.middle_name,
        birth_date=subject.birth_date.strftime("%Y-%m-%d"),
        birth_date_key=birth_date_key,
    )


def inn_params(inn: str) -> dict[str, Any]:
    """The single-parameter block for a natural person addressed by ИНН.

    ``innfiz``, not ``inn``: the latter is the legal-entity field and is
    validated as ten digits, so a person's twelve-digit ИНН sent under it is
    rejected outright (``innyur / inn is not valid``). Checked against the live
    endpoint, which validates the parameter before the key.
    """
    return {"country": COUNTRY_RU, "innfiz": inn}


def individual_inn(subject: SearchSubject) -> str | None:
    """The subject's ИНН, but only if it can be sent as ``innfiz``.

    ``normalize_inn`` accepts ten digits too, because a ten-digit ИНН is a valid
    identifier — of a legal entity. ``innfiz`` is validated as twelve, so a
    ten-digit value would buy a rejected (and, judging by ``cost: 1`` on the
    live endpoint, still billed) call instead of the honest answer that there is
    nothing to search by.
    """
    inn = subject.inn
    if inn and len(inn) == INN_INDIVIDUAL_LENGTH:
        return inn
    return None


class NewDBMethodProvider(BaseProvider):
    """Base for a source served by NewDB methods whose rows come from the map.

    ``fssp_person`` deliberately does not use this: its rows were read from a
    real response and are parsed by hard-coded keys. Everything else here is
    parsed by the deployment's own description of its contract, and a method
    with no description is a method that reports ``NOT_CONFIGURED``.
    """

    methods: tuple[str, ...] = ()

    def __init__(
        self,
        settings: Settings,
        field_maps: NewDBFieldMaps,
        client: NewDBClient | None = None,
    ) -> None:
        self._settings = settings
        self._field_maps = field_maps
        self._client = client or NewDBClient(settings)

    @property
    def mapped_methods(self) -> tuple[str, ...]:
        return tuple(method for method in self.methods if method in self._field_maps)

    @property
    def is_configured(self) -> bool:
        return self._settings.newdb_configured and bool(self.mapped_methods)

    async def rows_for(
        self, method: str, *param_sets: Mapping[str, Any]
    ) -> tuple[list[RecordDict], str]:
        """Run one mapped method and return its rows as flat domain-key dicts.

        ``extra_params`` from the map wins over the subject-derived parameters:
        it exists precisely for a deployment whose contract wants something
        different from what this code would send.
        """
        mapping = self._field_maps.require(method)
        merged = [{**params, **mapping.extra_params} for params in param_sets]
        response = await self._client.call(method, *merged)
        mapped = mapping.apply(response.rows)
        if mapped.unreadable and not mapped.records:
            # The source answered, and the map read nothing in the answer.
            # Reporting that as ``NO_RESULTS`` would tell the operator the
            # register is clean on the strength of a file of wrong paths.
            raise ProviderUnavailableError(
                "unexpected_schema",
                f"Карта полей не разобрала ни одной строки ответа NewDB ({method})",
            )
        if mapped.unreadable:
            logger.warning(
                "newdb.unreadable_rows",
                method=method,
                unreadable=mapped.unreadable,
                parsed=len(mapped.records),
            )
        return mapped.records, response.raw

    def raw_for(self, raw: str) -> str | None:
        return raw if self._settings.store_raw_responses else None


__all__ = [
    "COUNTRY_RU",
    "DOB_KEY",
    "MappedRows",
    "MethodMap",
    "NewDBClient",
    "NewDBFieldMaps",
    "NewDBMethodProvider",
    "NewDBResponse",
    "individual_inn",
    "inn_params",
    "person_params",
    "person_params_for",
    "result_data_path",
]
