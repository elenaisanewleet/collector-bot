"""ФИО по телефону через переписку с ботом в Telegram.

Второй адаптер к слоту :mod:`app.providers.phone_bridge`. Первый ходит по HTTP;
этот разговаривает с ботом от имени пользовательского аккаунта, потому что у
сервиса заказчицы другого интерфейса нет: телеграм-бот адреса наружу не имеет, и
боты не могут писать друг другу.

Живёт отдельным файлом и отдельной зависимостью намеренно. ``telethon`` в
основные зависимости не входит: без него весь остальной продукт собирается и
работает, а мост честно отвечает «не подключено».

ЧТО ЗДЕСЬ СДЕЛАНО ПРОТИВ ТРЁХ ИЗВЕСТНЫХ БЕД

**Массовый прогон сюда не ходит.** Восемьсот должников — восемьсот сообщений в
чат; Telegram отвечает на это ``FloodWait``, а потом ограничением аккаунта.
Прогон и не нуждается в мосте: ФИО и госномера у взыскателя уже есть в выгрузке,
номера телефона в ней нет вовсе. Поэтому в ``batch`` мост отвечает отказом до
единого обращения — см. :meth:`_dispatch`.

**Частота ограничена.** Между обращениями выдерживается пауза, и она не
украшение: без неё интерактивная работа оператора точно так же упирается в
``FloodWait``, только позже и неожиданнее.

**Ответ не угадывается.** Бот отвечает свободным текстом, и текст меняется без
предупреждения. Разбор строгий: не нашли в ответе ФИО в форме «Фамилия Имя
Отчество» — говорим «имя не определено». Собрать имя «примерно» здесь дороже
всего: им будет поднята не та строка выгрузки, и отчёт уедет про другого
человека, а разницы никто не заметит.

ЧЕГО ЭТОТ МОСТ НЕ МЕНЯЕТ

Найденное имя остаётся ключом поиска, а не фактом отчёта — ровно как у
HTTP-адаптера. В отчёт едут данные взыскателя и ответы официальных реестров.

ЧТО НУЖНО, ЧТОБЫ ВКЛЮЧИТЬ

``TELEGRAM_LOOKUP_API_ID`` и ``TELEGRAM_LOOKUP_API_HASH`` — с my.telegram.org.
``TELEGRAM_LOOKUP_SESSION`` — путь к файлу сессии ВНЕ репозитория, режим 600.
``TELEGRAM_LOOKUP_BOT`` — username бота, которому писать.

Файл сессии равен полному доступу к тому аккаунту, под которым выполнен вход,
поэтому для этой работы заводится отдельный аккаунт, а не рабочий. Однократный
вход делает ``scripts/telegram_login.py``; в рантайме бота интерактивного входа
не происходит никогда — без готовой сессии мост отвечает «не подключено».
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from app.config import Settings
from app.domain.enums import ProviderStatus
from app.domain.identity import NameParseError, PersonName, SearchSubject, parse_fio
from app.domain.models import ProviderResult
from app.providers.base import FetchContext, ProviderUnavailableError
from app.providers.phone_bridge import PhoneNameProvider, PhoneNameResult
from app.utils.dates import parse_date
from app.utils.masking import mask_phone

__all__ = ["TelegramPhoneNameProvider"]

#: ФИО в свободном тексте ответа. Три слова с большой буквы подряд, кириллица,
#: допускается дефис и тюркская частица отчества. Ищется по всему сообщению:
#: подпись поля («ФИО:», «Имя:») у каждого бота своя, а форма имени — нет.
_NAME_IN_TEXT = re.compile(
    r"\b([А-ЯЁ][а-яё]+(?:-[А-ЯЁ][а-яё]+)?)\s+"
    r"([А-ЯЁ][а-яё]+)\s+"
    r"([А-ЯЁ][а-яё]+(?:\s+(?:оглы|кызы|угли|уулу))?)\b"
)

#: Дата рождения в свободном тексте: 24.11.1994 или 24/11/1994.
_BIRTH_IN_TEXT = re.compile(r"\b(\d{2}[./]\d{2}[./]\d{4})\b")


class TelegramPhoneNameProvider(PhoneNameProvider):
    """Спрашивает ФИО у бота в Telegram от имени пользовательского аккаунта."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        super().__init__(settings)
        self._tg = client
        self._lock = asyncio.Lock()
        self._last_call = 0.0

    @property
    def is_configured(self) -> bool:
        if self._tg is not None:
            return True
        return self._settings.telegram_lookup_configured

    async def _dispatch(self, subject: SearchSubject, context: FetchContext) -> ProviderResult:
        if context.batch:
            # Не «не нашли», а «не спрашивали»: разница обязана быть видна.
            # Массовый прогон через чат — это FloodWait и ограничение аккаунта,
            # а нужды в нём нет: в выгрузке есть ФИО и госномера, телефона нет.
            return self.not_configured("В массовом прогоне определение по номеру не выполняется")
        return await self._fetch(subject)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        missing = self.missing_input_for(subject)
        if missing or not subject.phone:
            return self.insufficient_query("Нужен номер телефона", missing=missing)

        async with self._lock:
            await self._respect_rate_limit()
            text = await self._ask(subject.phone)

        return _read_reply(text, phone=subject.phone, provider=self)

    async def _respect_rate_limit(self) -> None:
        """Пауза между обращениями. Считается от последнего, а не от первого."""
        gap = self._settings.telegram_lookup_min_interval_seconds
        waited = time.monotonic() - self._last_call
        if self._last_call and waited < gap:
            await asyncio.sleep(gap - waited)
        self._last_call = time.monotonic()

    async def _ask(self, phone: str) -> str:
        """Отправить номер боту и дождаться ответа. Пустая строка — ответа нет."""
        client = await self._connected()
        target = self._settings.telegram_lookup_bot
        timeout = self._settings.telegram_lookup_reply_timeout_seconds
        try:
            async with client.conversation(target, timeout=timeout) as talk:
                await talk.send_message(phone)
                reply = await talk.get_response()
        except TimeoutError:
            # Молчание — не «имени нет». Источник не ответил, и отчёт обязан
            # сказать именно это.
            raise ProviderUnavailableError("Сервис не ответил на запрос") from None
        except Exception as exc:  # pragma: no cover - зависит от сети и сессии
            raise ProviderUnavailableError(
                f"Не удалось спросить сервис: {type(exc).__name__}"
            ) from exc
        return str(getattr(reply, "text", "") or "")

    async def _connected(self) -> Any:
        if self._tg is None:  # pragma: no cover - требует настоящей сессии
            self._tg = _build_client(self._settings)
        if not self._tg.is_connected():  # pragma: no cover - то же
            await self._tg.connect()
            if not await self._tg.is_user_authorized():
                raise ProviderUnavailableError(
                    "Вход в Telegram не выполнен: запустите scripts/telegram_login.py"
                )
        return self._tg


def _build_client(settings: Settings) -> Any:  # pragma: no cover - требует telethon
    from telethon import TelegramClient  # type: ignore[import-not-found]

    return TelegramClient(
        str(settings.telegram_lookup_session),
        settings.telegram_lookup_api_id,
        settings.telegram_lookup_api_hash,
    )


def _read_reply(text: str, *, phone: str, provider: TelegramPhoneNameProvider) -> ProviderResult:
    """Достать ФИО из свободного текста ответа.

    Строго: имя, которого нет в форме «Фамилия Имя Отчество», не собирается по
    кускам. Разобранное неверно, оно поднимет не ту строку выгрузки, и отчёт
    уедет про другого человека — молча.
    """
    name = _find_name(text)
    if name is None:
        return PhoneNameResult(
            provider=provider.name,
            status=ProviderStatus.NO_RESULTS,
            records=(),
            name=None,
            note=f"По номеру {mask_phone(phone)} имя не определено",
        )
    birth_match = _BIRTH_IN_TEXT.search(text)
    return PhoneNameResult(
        provider=provider.name,
        status=ProviderStatus.SUCCESS,
        records=(),
        name=name,
        birth_date=parse_date(birth_match.group(1)) if birth_match else None,
        note=f"ФИО определено по номеру {mask_phone(phone)}",
    )


def _find_name(text: str) -> PersonName | None:
    for match in _NAME_IN_TEXT.finditer(text or ""):
        try:
            return parse_fio(" ".join(match.groups()))
        except NameParseError:
            continue
    return None
