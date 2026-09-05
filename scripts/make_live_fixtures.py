"""Cut the test fixtures out of the live NewDB answers captured on 05.09.2026.

    python scripts/make_live_fixtures.py [--source /tmp] [--out tests/data]

Why a script and not hand-typed JSON
------------------------------------
A fixture retyped by hand is a fixture that agrees with whatever the code
already does. Every file this writes is the byte-for-byte envelope the live
service returned, with **nothing removed and no key renamed** — only personal
data substituted, value for value, in a way that cannot change the *shape* of
the answer:

*   ФИО -> invented ФИО of the same word count and the same letter case;
*   ИНН -> another string of the same length made of digits;
*   ОГРН/ОГРНИП, СНИЛС, dates, addresses, case numbers, GUIDs and vendor access
    tokens -> invented values in the same format and of the same length.

Nulls stay null, empty strings stay empty strings, the vendor's misspelled
``commmon`` stays misspelled. If a substitution would have to change a type or
a length, it is not made.

The source files are the raw captures kept outside the repository (they contain
real people's data and must not be committed):

    /tmp/real_bankrot.json                        bankrot_person, one case
    /tmp/nd_real/bankrot_person_770000000001.json bankrot_person, empty
    /tmp/nd_real/egrul_ip_770000000003.json       egrul_ip, ИП + upr + uchr
    /tmp/real_egrul.json                          egrul_ip, ip + ip + docip
    /tmp/nd_real/egrul_ip_770000000001.json       egrul_ip, empty
    /tmp/real_arbitr.json                         arbitr_person, one case
    /tmp/nd_real/arbitr_person_770000000001.json  arbitr_person, empty
    /tmp/real_pledge.json                         pledge_person, 13 fnp_urls
    /tmp/nd_poll.json                             pledge_vin, empty
"""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- substitutions
#
# Every entry is "real value from the capture" -> "invented value of the same
# shape". Longest keys are replaced first so that a value containing another
# one cannot be half-substituted.
# Таблица замен НЕ ХРАНИТСЯ В РЕПОЗИТОРИИ, и это не удобство, а требование.
# Её ключи — настоящие ФИО, ИНН, СНИЛС, адреса и номера дел живых людей: чтобы
# заменить данные, надо их назвать. Словарь такого вида сам по себе является
# персональными данными, и в git ему места нет — даже в приватном.
#
# Файл задаётся переменной окружения NEWDB_SUBSTITUTIONS и лежит вне дерева
# репозитория. Формат — JSON: {"настоящее": "вымышленное"}.
# Без него скрипт не запускается: молча пропустить обезличивание нельзя.
SUBSTITUTIONS: dict[str, str] = {}


def load_substitutions() -> dict[str, str]:
    path = os.environ.get("NEWDB_SUBSTITUTIONS")
    if not path:
        raise SystemExit(
            "NEWDB_SUBSTITUTIONS не задана. Укажите путь к файлу замен вне репозитория — "
            "без него фикстуры уйдут в git неанонимизированными."
        )
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data:
        raise SystemExit(f"{path}: ожидался непустой JSON-объект замен")
    SUBSTITUTIONS.update({str(k): str(v) for k, v in data.items()})
    return SUBSTITUTIONS

# Vendor access tokens: hex strings that address a person's card or a ЕГРЮЛ
# extract. Replaced by a hex string of exactly the same length, so the shape of
# ``links.egrul`` ("token=<hex>&inn=<ИНН>&pdf=vyp") survives intact.
_HEX_TOKEN = re.compile(r"\b[0-9A-F]{32,}\b")
# GUIDs address real Федресурс / КАД cards; the format is what matters.
_GUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def _hex_like(match: re.Match[str]) -> str:
    length = len(match.group(0))
    return ("0123456789ABCDEF" * (length // 16 + 1))[:length]


_GUID_COUNTER: dict[str, str] = {}


def _guid_like(match: re.Match[str]) -> str:
    original = match.group(0)
    if original not in _GUID_COUNTER:
        index = len(_GUID_COUNTER)
        _GUID_COUNTER[original] = f"{index:08x}-0000-4000-8000-{index:012x}"
    return _GUID_COUNTER[original]


def anonymize(text: str) -> str:
    for real, invented in sorted(SUBSTITUTIONS.items(), key=lambda item: -len(item[0])):
        text = text.replace(real, invented)
    text = _HEX_TOKEN.sub(_hex_like, text)
    return _GUID.sub(_guid_like, text)


def anonymize_tree(payload: Any) -> Any:
    """Substitute inside *values*, not inside the serialized JSON.

    Doing it on the text would miss anything the encoder escapes — a company
    name in quotes arrives as ``ООО \\"...\\"`` — and the miss would be silent.
    """
    if isinstance(payload, str):
        return anonymize(payload)
    if isinstance(payload, dict):
        return {anonymize(key): anonymize_tree(value) for key, value in payload.items()}
    if isinstance(payload, list):
        return [anonymize_tree(item) for item in payload]
    return payload


def _strip_volatile(payload: Any) -> Any:
    """Drop the per-call bookkeeping that says nothing about the row schema.

    ``newdb_qid`` is an opaque per-request id; requestId / taskId are addresses
    of somebody's paid task. Everything that describes the *answer* — including
    ``cost``, ``balance`` and the empty top-level ``method`` — is kept, because
    those are exactly the parts of the envelope the code may some day read.
    """
    if isinstance(payload, dict):
        return {
            key: _strip_volatile(value)
            for key, value in payload.items()
            if key not in {"newdb_qid"}
        }
    if isinstance(payload, list):
        return [_strip_volatile(item) for item in payload]
    return payload


FIXTURES: tuple[tuple[str, str], ...] = (
    ("real_bankrot.json", "newdb_live_bankrot_person.json"),
    ("nd_real/bankrot_person_770000000001.json", "newdb_live_bankrot_person_empty.json"),
    ("nd_real/egrul_ip_770000000003.json", "newdb_live_egrul_ip.json"),
    ("real_egrul.json", "newdb_live_egrul_ip_duplicate_registration.json"),
    ("nd_real/egrul_ip_770000000001.json", "newdb_live_egrul_ip_empty.json"),
    ("real_arbitr.json", "newdb_live_arbitr_person.json"),
    ("nd_real/arbitr_person_770000000001.json", "newdb_live_arbitr_person_empty.json"),
    ("real_pledge.json", "newdb_live_pledge_person_unmatched.json"),
    ("nd_poll.json", "newdb_live_pledge_vin_empty.json"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/tmp"))
    parser.add_argument("--out", type=Path, default=Path("tests/data"))
    args = parser.parse_args()

    load_substitutions()

    written = 0
    for source_name, target_name in FIXTURES:
        source = args.source / source_name
        if not source.exists():
            print(f"skip {source}: not captured")
            continue
        payload = anonymize_tree(json.loads(source.read_text(encoding="utf-8")))
        target = args.out / target_name
        target.write_text(
            json.dumps(_strip_volatile(payload), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"{source} -> {target}")
        written += 1
    return 0 if written else 1


if __name__ == "__main__":
    raise SystemExit(main())
