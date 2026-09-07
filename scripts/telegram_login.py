"""Однократный вход в Telegram для моста «телефон → ФИО».

    python scripts/telegram_login.py

Спрашивает номер и код подтверждения, создаёт файл сессии по пути
``TELEGRAM_LOOKUP_SESSION`` и на этом заканчивается. В рантайме бота
интерактивного входа не происходит никогда: без готовой сессии мост отвечает
«не подключено», а не пытается спросить код у несуществующего человека посреди
обработки сообщения.

ПРО АККАУНТ. Файл сессии равен полному доступу к тому аккаунту, под которым
выполнен вход, — ко всей переписке, а не только к нужному боту. Поэтому:

*   заводите под эту работу ОТДЕЛЬНЫЙ аккаунт, не рабочий и не личный;
*   держите файл вне репозитория и вне резервных копий, режимом 600;
*   помните, что автоматизация пользовательских аккаунтов против правил
    Telegram: аккаунт могут ограничить, и вместе с ним пропадёт доступ к самому
    сервису. Массовый прогон через чат по этой причине запрещён в коде.

``TELEGRAM_LOOKUP_API_ID`` и ``TELEGRAM_LOOKUP_API_HASH`` берутся на
my.telegram.org → API development tools.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Runnable straight from a checkout: executing a file puts *its* directory on
# sys.path, not the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings


async def main() -> int:
    settings = get_settings()
    missing = [
        name
        for name, value in (
            ("TELEGRAM_LOOKUP_API_ID", settings.telegram_lookup_api_id),
            ("TELEGRAM_LOOKUP_API_HASH", settings.telegram_lookup_api_hash),
            ("TELEGRAM_LOOKUP_SESSION", settings.telegram_lookup_session),
            ("TELEGRAM_LOOKUP_BOT", settings.telegram_lookup_bot),
        )
        if not value
    ]
    if missing:
        print("Не заполнено в .env: " + ", ".join(missing))
        return 1

    try:
        from telethon import TelegramClient  # type: ignore[import-not-found]
    except ImportError:
        print("Нет библиотеки. Установите: pip install -e '.[telegram-lookup]'")
        return 1

    session = settings.telegram_lookup_session
    assert session is not None
    session.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(
        str(session), settings.telegram_lookup_api_id, settings.telegram_lookup_api_hash
    )
    async with client:
        await client.start()  # спросит номер и код, если сессии ещё нет
        me = await client.get_me()
        print(f"Вход выполнен: {getattr(me, 'username', None) or getattr(me, 'id', '?')}")

    # Сессия — это доступ к аккаунту целиком. Права сужаются сразу, а не
    # «когда-нибудь потом»: файл создаёт библиотека, и по умолчанию он читаем.
    session.chmod(0o600)
    print(f"Файл сессии: {session} (режим 600)")
    print(f"Бот для запросов: {settings.telegram_lookup_bot}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
