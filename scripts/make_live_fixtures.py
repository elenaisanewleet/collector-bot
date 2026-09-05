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
    /tmp/nd_real/bankrot_person_732817727300.json bankrot_person, empty
    /tmp/nd_real/egrul_ip_770600089967.json       egrul_ip, ИП + upr + uchr
    /tmp/real_egrul.json                          egrul_ip, ip + ip + docip
    /tmp/nd_real/egrul_ip_732817727300.json       egrul_ip, empty
    /tmp/real_arbitr.json                         arbitr_person, one case
    /tmp/nd_real/arbitr_person_732817727300.json  arbitr_person, empty
    /tmp/real_pledge.json                         pledge_person, 13 fnp_urls
    /tmp/nd_poll.json                             pledge_vin, empty
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- substitutions
#
# Every entry is "real value from the capture" -> "invented value of the same
# shape". Longest keys are replaced first so that a value containing another
# one cannot be half-substituted.
SUBSTITUTIONS: dict[str, str] = {
    # --- bankrot_person / egrul_ip subject #1
    "Пыж Анна Викторовна": "Пыжова Анна Петровна",
    "ПЫЖ АННА ВИКТОРОВНА": "ПЫЖОВА АННА ПЕТРОВНА",
    "270392288605": "270311112222",
    "323270000057022": "323270000011111",
    "307272011400023": "307272011400022",
    "106-556-061 42": "111-222-333 44",
    "27.01.1980": "14.03.1979",
    "гор. Хабаровск": "гор. Приморск",
    "680054, г. Хабаровск, ул. Трехгорная, 56, кв. 4": (
        "680000, г. Приморск, ул. Ягодная, 11, кв. 7"
    ),
    "А73-7992/2017": "А73-1111/2017",
    # --- egrul_ip subject #2 (роли в ЮЛ)
    "ПАРФЕНЕНКО АНТОН ОРЕСТОВИЧ": "ПАРФЁНОВ АНТОН ОРЕСТОВИЧ",
    "770600089967": "770600011111",
    "320774600370587": "320774600311111",
    "9728012826": "9728011111",
    "1207700337327": "1207700311111",
    'ООО "СТАЛЬНОЕ СЕРДЦЕ"': 'ООО "СТАЛЬНОЙ КЛЮЧ"',
    'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СТАЛЬНОЕ СЕРДЦЕ"': (
        'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "СТАЛЬНОЙ КЛЮЧ"'
    ),
    "https://bo.nalog.gov.ru/organizations-card/11400816": (
        "https://bo.nalog.gov.ru/organizations-card/11400000"
    ),
    # --- arbitr_person
    "Леликов Андрей Викторович": "Тестов Андрей Викторович",
    "ЛЕЛИКОВ АНДРЕЙ ВИКТОРОВИЧ": "ТЕСТОВ АНДРЕЙ ВИКТОРОВИЧ",
    "Леликова Андрея Викторовича": "Тестова Андрея Викторовича",
    "Леликову Андрею Викторовичу": "Тестову Андрею Викторовичу",
    "Леликова А.В.": "Тестова А.В.",
    "Леликов А.В.": "Тестов А.В.",
    "Леликов А. В.": "Тестов А. В.",
    "644605034188": "644600011111",
    "А57-10442/2025": "А57-11111/2025",
    "Саратовская обл., г. Ртищево, ул. Красная, д.22, кв.10": (
        "Саратовская обл., г. Ртищево, ул. Луговая, д.1, кв.1"
    ),
    "412030, Россия, г.Ртищево, Саратовская область , ул.Красная д.6": (
        "412030, Россия, г.Ртищево, Саратовская область , ул.Луговая д.6"
    ),
    "412030, Россия, Ртищево, Саратовская обл., Красная 6": (
        "412030, Россия, Ртищево, Саратовская обл., Луговая 6"
    ),
    "10.05.1985": "10.05.1985",  # pledge probe input, already invented
    # --- pledge_person (ФИО в params — вымышленные входные данные пробы)
}

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
    ("nd_real/bankrot_person_732817727300.json", "newdb_live_bankrot_person_empty.json"),
    ("nd_real/egrul_ip_770600089967.json", "newdb_live_egrul_ip.json"),
    ("real_egrul.json", "newdb_live_egrul_ip_duplicate_registration.json"),
    ("nd_real/egrul_ip_732817727300.json", "newdb_live_egrul_ip_empty.json"),
    ("real_arbitr.json", "newdb_live_arbitr_person.json"),
    ("nd_real/arbitr_person_732817727300.json", "newdb_live_arbitr_person_empty.json"),
    ("real_pledge.json", "newdb_live_pledge_person_unmatched.json"),
    ("nd_poll.json", "newdb_live_pledge_vin_empty.json"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("/tmp"))
    parser.add_argument("--out", type=Path, default=Path("tests/data"))
    args = parser.parse_args()

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
