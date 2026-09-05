"""Инвентарь опубликованного OData 1С и сверка карты справочников.

    python scripts/onec_doctor.py

Читает ``ONEC_*`` из ``.env``, скачивает ``$metadata`` и печатает: какие
коллекции опубликованы, из каких реквизитов они состоят и что в написанной
карте не сходится с базой. Без доступа честно говорит, чего не хватает.

Печатает — и только. Ни ``$select``, ни фильтры, ни соответствие реквизитов
доменным полям отсюда не выводятся: метаданные знают имена, а не смысл, и
решение, какой справочник про должников, принимает человек.

Запускается вручную и вне рантайма бота.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Runnable straight from a checkout: executing a file puts *its* directory on
# sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings
from app.logging_setup import configure_logging
from app.providers.onec.doctor import run_doctor


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=False)
    exit_code, lines = await run_doctor(settings)
    print("\n".join(lines))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
