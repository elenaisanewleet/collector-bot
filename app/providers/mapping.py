"""Configuration-driven mapping of vendor JSON onto domain models.

Several of the sources this tool needs (ЕФРСБ, ЕГРЮЛ/ЕГРИП) are reachable only
through commercial API vendors, and each vendor uses its own field names. Rather
than inventing a schema and hard-coding it — which would produce a plausible but
fictional integration — the adapters read a field map supplied by the
deployment. Until a real map is provided, those adapters stay unconfigured.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.providers.base import ProviderError

RecordDict = dict[str, Any]


class FieldMapError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_field_map", message)


class FieldMap:
    """Maps a vendor payload onto the flat keys a provider expects.

    ``records_path`` locates the array of records inside the response envelope,
    and ``fields`` maps each domain key to a dotted path within one record.
    Missing paths yield ``None`` rather than raising: vendors omit fields, and a
    partially-populated record is still useful.
    """

    def __init__(
        self,
        *,
        records_path: str = "",
        fields: Mapping[str, str] | None = None,
        value_maps: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        self.records_path = records_path
        self.fields = dict(fields or {})
        self.value_maps = {key: dict(value) for key, value in (value_maps or {}).items()}

    @classmethod
    def from_file(cls, path: Path) -> FieldMap:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise FieldMapError(f"cannot read field map {path}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise FieldMapError(f"field map {path} is not valid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise FieldMapError(f"field map {path} must be a JSON object")
        return cls(
            records_path=str(payload.get("records_path", "")),
            fields=payload.get("fields", {}),
            value_maps=payload.get("value_maps", {}),
        )

    def extract_records(self, payload: Any) -> list[RecordDict]:
        node = dig(payload, self.records_path) if self.records_path else payload
        if node is None:
            return []
        if isinstance(node, dict):
            node = [node]
        if not isinstance(node, list):
            return []
        return [item for item in node if isinstance(item, dict)]

    def apply(self, record: Mapping[str, Any]) -> RecordDict:
        out: RecordDict = {}
        for target, source_path in self.fields.items():
            value = dig(record, source_path)
            if value is not None and target in self.value_maps:
                value = self.value_maps[target].get(str(value).strip().lower(), value)
            out[target] = value
        return out

    def map_all(self, payload: Any) -> list[RecordDict]:
        return [self.apply(record) for record in self.extract_records(payload)]


@dataclass(frozen=True, slots=True)
class MappedRows:
    """Rows the map could read, plus a count of the ones it could not.

    Keeping the two apart is the whole point: "the source answered with
    nothing" and "the map read nothing in the answer" both come out as zero
    records, and they mean opposite things.
    """

    records: list[RecordDict]
    unreadable: int


def has_any_value(record: Mapping[str, Any]) -> bool:
    """Did the map fill in anything at all?

    An all-``None`` record is not a finding, it is a set of paths that missed.
    Passing it on would turn a wrong map into a bankruptcy with no case number
    and a pledge with no subject — findings about people nobody parsed.
    """
    return any(value is not None for value in record.values())


def dig(payload: Any, path: str) -> Any:
    """Follow a dotted path, tolerating missing keys and list indices."""
    if not path:
        return payload
    node = payload
    for segment in path.split("."):
        if node is None:
            return None
        if isinstance(node, Mapping):
            node = node.get(segment)
        elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            try:
                node = node[int(segment)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def first_present(record: Mapping[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def as_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
