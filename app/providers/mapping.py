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
from pathlib import Path
from typing import Any

from app.providers.base import ProviderError

RecordDict = dict[str, Any]


class FieldMapError(ProviderError):
    def __init__(self, message: str) -> None:
        super().__init__("invalid_field_map", message)


class FieldMap:
    """Maps a vendor payload onto the flat keys a provider expects.

    ``records_path`` locates the array of records inside *the payload this map is
    handed*, and ``fields`` maps each domain key to a dotted path within one
    record. What that payload is depends on the caller: the vendor adapters pass
    the whole response body, so the path is counted from the envelope, while
    :class:`app.providers.newdb.MethodMap` has already unwrapped the envelope and
    passes one row of ``data``, so the path names the array nested inside that
    row. Both are "the array of records inside what you gave me"; neither is
    "somewhere in the response".

    Missing paths yield ``None`` rather than raising: vendors omit fields, and a
    partially-populated record is still useful. Whether a record that came out
    entirely empty is a finding or a broken map is a question for the caller,
    which is the only one that knows what it asked for.
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

    def record_nodes(self, payload: Any) -> list[Any]:
        """The items of the record array *as they arrived*, nothing filtered out.

        Separate from :meth:`extract_records` because the two answer different
        questions: what can be read, and what was there to read. Only the caller
        that knows both can tell "the array was empty" from "the array held two
        things this map cannot describe".
        """
        node = dig(payload, self.records_path) if self.records_path else payload
        if node is None:
            return []
        if isinstance(node, Mapping):
            return [node]
        if not isinstance(node, list):
            return []
        return list(node)

    def read_records(self, payload: Any) -> tuple[list[RecordDict], int]:
        """Records the map could read, and how many items of the array it could not.

        The count is the whole point. ``{"fnp": ["УВ-1", "УВ-2"]}`` — a register
        answering with two notices as bare strings instead of objects — used to
        come out as zero records and nothing unreadable, which reads as "no
        pledges" and pays a bonus for it. Two notices found, two notices lost,
        and not one word about it anywhere.
        """
        nodes = self.record_nodes(payload)
        records = [dict(item) for item in nodes if isinstance(item, Mapping)]
        return records, len(nodes) - len(records)

    def extract_records(self, payload: Any) -> list[RecordDict]:
        return self.read_records(payload)[0]

    def apply(self, record: Mapping[str, Any]) -> RecordDict:
        out: RecordDict = {}
        for target, source_path in self.fields.items():
            value = dig(record, source_path)
            if value is not None and target in self.value_maps:
                value = self.value_maps[target].get(str(value).strip().lower(), value)
            out[target] = value
        return out

    def map_all(self, payload: Any) -> list[RecordDict]:
        return self.read_all(payload)[0]

    def read_all(self, payload: Any) -> tuple[list[RecordDict], int]:
        """Mapped records, and the count of array items that were not records.

        The count exists for the same reason it does in :meth:`read_records`:
        the caller has to be able to tell "the source sent nothing" from "the
        source sent something this map cannot read", and only a number can.
        """
        records, unreadable = self.read_records(payload)
        return [self.apply(record) for record in records], unreadable


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
