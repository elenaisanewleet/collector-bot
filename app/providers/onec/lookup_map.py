"""Карта поисков 1С: всё, чего нет в протоколе и не может быть в коде.

Точная калька механизма ``NEWDB_FIELD_MAP`` (:mod:`app.providers.newdb`),
включая пропуск ключей на подчёркивание, требование непустого ``fields`` и
:class:`~app.providers.mapping.FieldMapError` на любой кривой записи.

Правило то же самое, что и у остальных источников проекта: *кодом описывается
проверенное, всё остальное приносит деплой.* Протокол OData опубликован фирмой
«1С» и одинаков везде — он в коде. Имена справочников, реквизитов и вид фильтра
разные в УТ, ERP, УНФ и в самописной конфигурации, ни одного образца базы
заказчика у нас нет — они здесь. Поиск, которого нет в файле, не выполняется и
докладывается как «не подключено»: это не то же самое, что «должник не найден».
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.providers.mapping import (
    FieldMap,
    FieldMapError,
    MappedRows,
    dig,
    has_any_value,
)
from app.providers.onec.client import DEFAULT_ORDERBY, quote_literal, select_from_paths

# Восемь поисков, которые умеет спрашивать SearchService. Ключи карты сверяются
# с этим набором, чтобы опечатка в имени не превращалась молча в «поиск не
# описан».
LOOKUPS: frozenset[str] = frozenset(
    {
        "by_debtor_id",
        "by_contract",
        "by_claim",
        "by_vin",
        "by_plate",
        "by_phone",
        "by_fio",
        "by_address",
    }
)

VALUE_PLACEHOLDER = "{value}"
BIRTH_DATE_PLACEHOLDER = "{birth_date}"


@dataclass(frozen=True, slots=True)
class LookupMap:
    """Как найти должника одним способом и как прочитать найденное.

    ``filter_template`` — единственное место, где живёт синтаксис предиката.
    Он в карте, а не в коде, потому что версии платформы расходятся даже в
    имени функции подстроки (``substringof`` против ``contains``), а тип
    реквизита решает, сравнимо ли поле вообще.
    """

    lookup: str
    collection: str
    filter_template: str
    field_map: FieldMap
    extra_filter: str | None = None
    # Для поиска по ФИО: дата рождения в 1С — ``datetime``, и сравнение на
    # равенство с полуночью попадает не всегда. Диапазон за сутки — тоже
    # гипотеза деплоя, поэтому шаблон, а не код.
    birth_date_template: str | None = None
    orderby: str = DEFAULT_ORDERBY
    expand: str | None = None
    select: tuple[str, ...] = field(default_factory=tuple)

    def filter_for(self, value: str, *, birth_date: date | None = None) -> str:
        """Готовый ``$filter``: шаблон карты, экранированное значение, довесок."""
        parts = [self.filter_template.replace(VALUE_PLACEHOLDER, quote_literal(value))]
        if birth_date is not None and self.birth_date_template:
            parts.append(_format_birth_date(self.birth_date_template, birth_date))
        if self.extra_filter:
            parts.append(self.extra_filter)
        if len(parts) == 1:
            return parts[0]
        return " and ".join(f"({part})" for part in parts)

    def apply(self, rows: Iterable[Mapping[str, Any]]) -> MappedRows:
        """Строки ответа → плоские записи в доменных ключах.

        Строка, в которой карта не нашла ни одного поля, не запись, а промах
        карты, и считается отдельно: «источник ответил пусто» и «мы не поняли
        ответ» дают один и тот же ноль и означают противоположное.
        """
        records = []
        unreadable = 0
        for row in rows:
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
                unreadable += 1
        return MappedRows(records=records, unreadable=unreadable)

    def _path_missing(self, row: Mapping[str, Any]) -> bool:
        """Названной картой табличной части в строке нет вовсе.

        ``"КонтактнаяИнформация": []`` — ответ: строк нет. Строка вообще без
        этого раздела — строка не той формы, что описана в карте.
        """
        path = self.field_map.records_path
        return bool(path) and dig(row, path) is None


class OneCLookupMaps:
    """Поиски, описанные деплоем, по имени поиска.

    Поиск в файле — это поиск, который заказчик сверил со своей конфигурацией.
    Поиска в файле нет — этот инструмент не станет притворяться, что понимает,
    где в чужой базе лежат должники.
    """

    def __init__(self, maps: Mapping[str, LookupMap] | None = None) -> None:
        self._maps = dict(maps or {})

    @classmethod
    def load(cls, path: Path | None) -> OneCLookupMaps:
        """Прочитать файл карты. Пути нет — ни один поиск не включён."""
        if path is None:
            return cls()
        payload = _read_json_object(path)
        maps: dict[str, LookupMap] = {}
        for lookup, entry in payload.items():
            if lookup.startswith("_"):  # comment keys
                continue
            if lookup not in LOOKUPS:
                raise FieldMapError(
                    f"{path}: неизвестный поиск {lookup!r}; допустимы {', '.join(sorted(LOOKUPS))}"
                )
            maps[lookup] = _lookup_map(path, lookup, entry)
        return cls(maps)

    def __contains__(self, lookup: str) -> bool:
        return lookup in self._maps

    @property
    def lookups(self) -> frozenset[str]:
        return frozenset(self._maps)

    def get(self, lookup: str) -> LookupMap | None:
        return self._maps.get(lookup)

    def require(self, lookup: str) -> LookupMap:
        mapping = self._maps.get(lookup)
        if mapping is None:
            raise FieldMapError(f"поиск 1С {lookup!r} не описан в ONEC_FIELD_MAP")
        return mapping


def _read_json_object(path: Path) -> Mapping[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FieldMapError(f"cannot read field map {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise FieldMapError(f"field map {path} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise FieldMapError(f"field map {path} must be a JSON object keyed by lookup name")
    return payload


def _lookup_map(path: Path, lookup: str, entry: Any) -> LookupMap:
    if not isinstance(entry, Mapping):
        raise FieldMapError(f"{path}: запись поиска {lookup!r} должна быть объектом")

    collection = str(entry.get("collection", "")).strip()
    if not collection:
        raise FieldMapError(f"{path}: у поиска {lookup!r} не задан collection")

    template = str(entry.get("filter_template", "")).strip()
    if VALUE_PLACEHOLDER not in template:
        raise FieldMapError(
            f"{path}: filter_template поиска {lookup!r} должен содержать {VALUE_PLACEHOLDER}"
        )

    fields = entry.get("fields")
    if not isinstance(fields, Mapping) or not fields:
        # Пустая карта разобрала бы любую строку в запись из одних None: это не
        # интеграция, а источник, который всегда отвечает «ничего не известно».
        raise FieldMapError(f"{path}: у поиска {lookup!r} нет непустого раздела fields")
    field_paths = {str(key): str(value) for key, value in fields.items()}

    birth_template = entry.get("birth_date_template")
    if birth_template is not None:
        birth_template = str(birth_template)
        if BIRTH_DATE_PLACEHOLDER not in birth_template:
            raise FieldMapError(
                f"{path}: birth_date_template поиска {lookup!r} должен содержать "
                f"{BIRTH_DATE_PLACEHOLDER}"
            )

    select = entry.get("select")
    if select is None:
        # $select обязателен всегда; если деплой его не перечислил, он выводится
        # из верхних сегментов путей fields — того, что мы и так собираемся
        # читать. Больше просить не за чем.
        select_fields = select_from_paths(field_paths.values())
    else:
        if not isinstance(select, list) or not all(isinstance(item, str) for item in select):
            raise FieldMapError(f"{path}: select поиска {lookup!r} должен быть списком строк")
        select_fields = tuple(str(item) for item in select)
    if not select_fields:
        raise FieldMapError(f"{path}: у поиска {lookup!r} не из чего построить $select")

    return LookupMap(
        lookup=lookup,
        collection=collection,
        filter_template=template,
        field_map=FieldMap(
            # Конверт OData (``value``) разбирает клиент — громко, потому что
            # отсутствие ``value`` это не пустой ответ. ``records_path`` здесь
            # значит другое: табличная часть ВНУТРИ строки, если карта читает
            # записи оттуда. Пусто — строка сама и есть запись.
            records_path=str(entry.get("records_path", "")),
            fields=field_paths,
            value_maps=entry.get("value_maps", {}),
        ),
        extra_filter=_optional_text(entry.get("extra_filter")),
        birth_date_template=birth_template,
        orderby=str(entry.get("orderby", DEFAULT_ORDERBY)) or DEFAULT_ORDERBY,
        expand=_optional_text(entry.get("expand")),
        select=select_fields,
    )


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _format_birth_date(template: str, birth_date: date) -> str:
    """Дата в литерал OData ``datetime'YYYY-MM-DDTHH:MM:SS'``.

    Подставляются и сама дата, и следующий день: шаблон деплоя обычно задаёт
    полуинтервал, потому что реквизит в 1С — момент времени, а не дата.
    """
    next_day = date.fromordinal(birth_date.toordinal() + 1)
    return template.replace(BIRTH_DATE_PLACEHOLDER, birth_date.isoformat()).replace(
        "{birth_date_next}", next_day.isoformat()
    )


__all__ = [
    "LOOKUPS",
    "LookupMap",
    "OneCLookupMaps",
]
