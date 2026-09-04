"""Load the demo CSV into the local database.

    python scripts/seed_demo.py [path/to/export.csv]

Creates the schema if it is missing, so a fresh checkout needs nothing else.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from app.config import get_settings
from app.container import build_container
from app.logging_setup import configure_logging


async def seed(csv_path: Path | None = None) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, json_output=settings.log_json)

    path = csv_path or settings.internal_csv_path
    if not path.is_file():
        print(f"Файл не найден: {path}")
        return 1

    container = build_container(settings)
    try:
        await container.database.create_all()
        report = await container.import_service.import_file(path)
    finally:
        await container.dispose()

    print(
        f"Импорт из {path}\n"
        f"  всего строк:   {report.total_rows}\n"
        f"  импортировано: {report.imported} (новых {report.created}, "
        f"обновлено {report.updated})\n"
        f"  пропущено:     {report.skipped}\n"
        f"  ошибок:        {report.failed}"
    )
    for error in report.errors:
        print(f"  ! {error}")
    for warning in report.warnings:
        print(f"  ~ {warning}")
    return 0


def main() -> None:
    argument = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    raise SystemExit(asyncio.run(seed(argument)))


if __name__ == "__main__":
    main()
