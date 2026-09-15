"""Что поставщик вернул по номеру — и что из этого увидела наша карта полей.

Зачем нужен отдельный скрипт, когда есть лог. Лог печатает только ИМЕНА полей и
только те, что прошли через карту полей: `records_path` задаёт поддерево, и всё,
что лежит вне него, разбор не видит вовсе. То есть лог отвечает на вопрос «читаем
ли мы дату рождения», но не на вопрос «есть ли она в ответе». Владелец спросил
именно второе: «покажи, что depsearch возвращает по номеру — я проверю, есть ли в
ответе дата рождения».

Печатаются ДВЕ вещи рядом, и смысл в их сравнении:

*   тело ответа целиком, как пришло;
*   записи, которые из него достала карта полей.

Дата рождения в первом и её отсутствие во втором значит, что чинить надо карту.
Отсутствие в обоих значит, что поставщик её не знает, и чинить нечего.

    python -m scripts.probe_phone_bridge +79991234567

ВЫВОД СОДЕРЖИТ ПЕРСОНАЛЬНЫЕ ДАННЫЕ ТРЕТЬЕГО ЛИЦА. Он для чтения глазами на своём
сервере: не пересылать, не вставлять в переписку, не сохранять в репозиторий.
Скрипт поэтому и печатает в stdout, а не в файл.

Один запуск — одно платное обращение к поставщику.
"""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from app.config import get_settings
from app.domain.identity import normalize_phone
from app.providers.phone_bridge import PhoneNameProvider

WARNING = (
    "В выводе — персональные данные третьего лица. Только для чтения на этом "
    "сервере: не пересылать и не вставлять в переписку."
)


async def probe(phone: str) -> int:
    settings = get_settings()
    provider = PhoneNameProvider(settings)
    if not provider.is_configured:
        print("Мост по телефону не настроен: нет PHONE_BRIDGE_BASE_URL или карты полей.")
        return 1

    normalized = normalize_phone(phone)
    if normalized is None:
        print(f"Не разобрал номер: {phone!r}. Ожидается российский номер, 11 цифр.")
        return 1

    print(WARNING)
    print(f"\nНомер: {normalized}\n")

    # Тот же вызов, что делает сам мост, — не копия параметров, а он же: иначе
    # проверяли бы не то, что работает на проде.
    records, raw = await provider._vendor_client().fetch_records({"phone": normalized})

    print("=" * 72)
    print("ОТВЕТ ПОСТАВЩИКА, КАК ПРИШЁЛ")
    print("=" * 72)
    print(_pretty(raw))

    print()
    print("=" * 72)
    print(f"ЧТО УВИДЕЛА КАРТА ПОЛЕЙ — записей: {len(records)}")
    print("=" * 72)
    print(json.dumps(records, ensure_ascii=False, indent=2, default=str))

    print()
    print("=" * 72)
    print("КЛЮЧИ, КОТОРЫЕ ДОШЛИ ДО РАЗБОРА")
    print("=" * 72)
    keys = sorted({key for record in records for key in record})
    print(", ".join(keys) if keys else "ни одного: карта полей не достала ничего")
    print(
        "\nЕсли дата рождения видна в теле ответа выше, но её нет в этом списке —\n"
        "дело в карте полей (PHONE_BRIDGE_FIELD_MAP), а не в поставщике."
    )
    return 0


def _pretty(raw: str) -> str:
    """Тело ответа с отступами, а если это не JSON — как есть.

    Отступы здесь не косметика: ответ этого поставщика — свалка из десятков
    блоков, и глазами в одну строку её не прочитать.
    """
    try:
        parsed: Any = json.loads(raw)
    except ValueError:
        return raw
    return json.dumps(parsed, ensure_ascii=False, indent=2)


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    return asyncio.run(probe(sys.argv[1]))


if __name__ == "__main__":
    raise SystemExit(main())
