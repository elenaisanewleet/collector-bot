"""``onec-doctor`` — инвентарь опубликованного OData, глазами человека.

Что это делает: скачивает ``$metadata``, печатает список опубликованных
коллекций, состав их свойств с типами и скелет карты, который остаётся
заполнить. Плюс сверяет уже написанную карту: коллекции или реквизита нет в
метаданных — печатает предупреждение и похожие имена.

Чего это не делает, и это главное: **не принимает решений**. Ни ``$select``, ни
``$expand``, ни выбор синтаксиса литерала из метаданных не выводятся. Причина
ровно та же, по которой в этом проекте нет адаптера, написанного по догадкам:
парсер EDMX, сочинённый без единого настоящего образца и проверенный
собственноручно сочинённой фикстурой, доказывает только то, что мы умеем читать
свой же XML. Гипотеза в карте видна и правится текстовым редактором; гипотеза,
спрятанная в разборе метаданных, правится релизом.

И вторая причина: метаданные знают имена, а не смысл. Из ``$metadata`` не
следует, какой из справочников про должников и какой реквизит означает остаток
долга. На эти вопросы отвечает заказчик.

Запускается вручную (``make onec-doctor``), вне рантайма бота: на старте сеть
не трогается.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from xml.etree import ElementTree

from app.config import Settings
from app.providers.mapping import FieldMapError
from app.providers.onec.client import OneCODataClient
from app.providers.onec.lookup_map import LOOKUPS, OneCLookupMaps

# Локальные имена, а не namespace: у разных версий EDMX разные URI, и привязка
# к одному из них — способ не разобрать метаданные соседней версии платформы.
ENTITY_TYPE_TAG = "EntityType"
ENTITY_SET_TAG = "EntitySet"
PROPERTY_TAG = "Property"
NAVIGATION_TAG = "NavigationProperty"

SIMILAR_NAMES = 5


@dataclass(frozen=True, slots=True)
class EntitySet:
    """Одна опубликованная коллекция и то, что в ней видно."""

    name: str
    entity_type: str
    properties: tuple[tuple[str, str], ...] = ()
    navigation: tuple[str, ...] = ()


def parse_metadata(xml_text: str) -> list[EntitySet]:
    """Разобрать EDMX терпимо: непонятный документ — не повод падать."""
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError:
        return []

    types: dict[str, tuple[tuple[tuple[str, str], ...], tuple[str, ...]]] = {}
    for node in root.iter():
        if _local(node.tag) != ENTITY_TYPE_TAG:
            continue
        name = node.get("Name")
        if not name:
            continue
        properties = tuple(
            (child.get("Name", ""), child.get("Type", ""))
            for child in node
            if _local(child.tag) == PROPERTY_TAG and child.get("Name")
        )
        navigation = tuple(
            child.get("Name", "")
            for child in node
            if _local(child.tag) == NAVIGATION_TAG and child.get("Name")
        )
        types[name] = (properties, navigation)

    sets: list[EntitySet] = []
    for node in root.iter():
        if _local(node.tag) != ENTITY_SET_TAG:
            continue
        name = node.get("Name")
        if not name:
            continue
        entity_type = (node.get("EntityType") or "").rsplit(".", 1)[-1]
        properties, navigation = types.get(entity_type, ((), ()))
        sets.append(
            EntitySet(
                name=name,
                entity_type=entity_type,
                properties=properties,
                navigation=navigation,
            )
        )
    return sorted(sets, key=lambda item: item.name)


def render_inventory(sets: Sequence[EntitySet]) -> list[str]:
    lines = [f"Опубликовано коллекций: {len(sets)}", ""]
    for item in sets:
        lines.append(f"{item.name}")
        for prop, type_name in item.properties:
            lines.append(f"    {prop}: {type_name}")
        if item.navigation:
            lines.append(f"    → ссылочные: {', '.join(item.navigation)}")
        lines.append("")
    return lines


def render_map_skeleton(sets: Sequence[EntitySet]) -> list[str]:
    """Форма карты с пустыми шаблонами. Содержание вписывает человек."""
    lines = [
        "Скелет ONEC_FIELD_MAP. Имена коллекций подставлены из метаданных,",
        "предикаты и соответствие реквизитов доменным полям — нет: какой",
        "справочник про должников и какой реквизит означает остаток долга,",
        "метаданные не знают.",
        "",
        "{",
    ]
    candidates = ", ".join(item.name for item in sets[:SIMILAR_NAMES]) or "…"
    for lookup in sorted(LOOKUPS):
        lines.extend(
            [
                f'  "{lookup}": {{',
                f'    "collection": "",              // из списка выше: {candidates}',
                '    "filter_template": "",         // например: Реквизит eq \'{value}\'',
                '    "fields": {}                   // доменный ключ -> путь в записи',
                "  },",
            ]
        )
    lines.append("}")
    return lines


def render_map_check(sets: Sequence[EntitySet], maps: OneCLookupMaps) -> list[str]:
    """Сверка написанной карты с тем, что реально опубликовано."""
    if not maps.lookups:
        return ["ONEC_FIELD_MAP не задан или не описывает ни одного поиска."]

    by_name: Mapping[str, EntitySet] = {item.name: item for item in sets}
    lines = ["Сверка ONEC_FIELD_MAP с метаданными:", ""]
    for lookup in sorted(maps.lookups):
        mapping = maps.require(lookup)
        lines.append(f"{lookup} → {mapping.collection}")
        entity = by_name.get(mapping.collection)
        if entity is None:
            lines.append("    ✗ такой коллекции в метаданных нет")
            lines.extend(
                f"      похоже на: {name}" for name in _similar(mapping.collection, by_name)
            )
            continue
        known = {prop for prop, _type in entity.properties} | set(entity.navigation)
        for field_name in mapping.select:
            if field_name in known:
                continue
            lines.append(f"    ✗ реквизита {field_name} нет в {mapping.collection}")
            lines.extend(f"      похоже на: {name}" for name in _similar(field_name, known))
    lines.append("")
    return lines


async def run_doctor(settings: Settings) -> tuple[int, list[str]]:
    """Собрать отчёт доктора. Возвращает код выхода и строки для печати."""
    if not settings.onec_base_url:
        return 1, [
            "ONEC_BASE_URL не задан — спрашивать нечего.",
            "Адрес публикации выглядит так: https://host/база/odata/standard.odata",
        ]
    if not (settings.onec_username and settings.onec_password.get_secret_value()):
        return 1, ["ONEC_USERNAME/ONEC_PASSWORD не заданы — 1С ответит 401."]

    client = OneCODataClient(
        settings.onec_base_url,
        settings.onec_username,
        settings.onec_password.get_secret_value(),
        timeout_seconds=settings.request_timeout_seconds,
        verify=settings.onec_verify,
    )
    try:
        xml_text = await client.metadata_text()
    except Exception as exc:
        return 1, [f"$metadata получить не удалось: {type(exc).__name__}: {exc}"]

    sets = parse_metadata(xml_text)
    if not sets:
        return 1, [
            "Метаданные не разобраны: документ получен, но ни одной коллекции в нём",
            "не опознано. Это ответ про формат EDMX, а не про содержимое базы —",
            "сохраните ответ и посмотрите глазами.",
        ]

    lines = [*render_inventory(sets)]
    try:
        maps = OneCLookupMaps.load(settings.onec_field_map)
    except FieldMapError as exc:
        lines.extend(["ONEC_FIELD_MAP не читается:", str(exc.message), ""])
        maps = OneCLookupMaps()
    lines.extend(render_map_check(sets, maps))
    if not maps.lookups:
        lines.extend(render_map_skeleton(sets))
    return 0, lines


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _similar(name: str, pool: Iterable[str]) -> list[str]:
    return difflib.get_close_matches(name, list(pool), n=SIMILAR_NAMES, cutoff=0.5)


__all__ = [
    "EntitySet",
    "parse_metadata",
    "render_inventory",
    "render_map_check",
    "render_map_skeleton",
    "run_doctor",
]
