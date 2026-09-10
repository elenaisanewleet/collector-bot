"""Small text helpers shared by the report renderer and the bot handlers."""

from __future__ import annotations

import re
from collections.abc import Sequence

TELEGRAM_MESSAGE_LIMIT = 4096
#: Российский номер в нормализованном виде: код страны плюс десять цифр.
RU_PHONE_DIGITS = 11
_DIGITS = re.compile(r"\D")
_SAFE_CHUNK_LIMIT = 3900


def pluralize_ru(count: int, one: str, few: str, many: str) -> str:
    """Russian plural agreement: ``1 производство`` / ``2 производства`` / ``5 производств``."""
    abs_count = abs(count)
    if abs_count % 10 == 1 and abs_count % 100 != 11:
        return one
    if 2 <= abs_count % 10 <= 4 and not 12 <= abs_count % 100 <= 14:
        return few
    return many


def group_digits(value: int) -> str:
    """``3340`` → ``«3 340»``.

    Считанные штуки — обращения к источникам, строки выгрузки — читаются глазом
    так же плохо, как деньги, и группируются так же (:func:`money.format_amount`).
    Четырёхзначное число обращений стоит четырёхзначных денег, и «до 3340»
    рядом с «до 10 020 ₽» выглядит числом другого порядка, чем есть.
    """
    return f"{value:,}".replace(",", " ")


def signed(value: int) -> str:
    return f"+{value}" if value > 0 else str(value)


def percent(value: float) -> str:
    return f"{round(value * 100)}%"


def split_message(text: str, limit: int = _SAFE_CHUNK_LIMIT) -> list[str]:
    """Split a long report into Telegram-sized chunks on paragraph boundaries.

    Falls back to line and then hard splits so a pathological input still gets
    delivered rather than rejected by the API.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    for block in _split_on(text, "\n\n", limit):
        if len(block) <= limit:
            chunks.append(block)
            continue
        for line_block in _split_on(block, "\n", limit):
            if len(line_block) <= limit:
                chunks.append(line_block)
            else:
                chunks.extend(line_block[i : i + limit] for i in range(0, len(line_block), limit))
    return [chunk for chunk in chunks if chunk.strip()]


def _split_on(text: str, separator: str, limit: int) -> list[str]:
    pieces: Sequence[str] = text.split(separator)
    out: list[str] = []
    buffer = ""
    for piece in pieces:
        candidate = f"{buffer}{separator}{piece}" if buffer else piece
        if len(candidate) <= limit:
            buffer = candidate
        else:
            if buffer:
                out.append(buffer)
            buffer = piece
    if buffer:
        out.append(buffer)
    return out


def truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def format_phone(phone: str | None) -> str | None:
    """``+79991234567`` → ``+7 (999) 123-45-67``.

    Зеркало :func:`app.utils.masking.mask_phone`, и появилось оно ровно тогда,
    когда маску убрали из интерфейса. Хранится и ищется номер цифрами
    (``+79991234567``) — так его нормализует :func:`normalize_phone`, так он
    сравнивается и хэшируется. Показывать его в этом виде значило бы поменять
    аккуратную маску на строку, которую глазом не прочитать и вслух не
    продиктовать, — то есть сделать интерфейс ХУЖЕ снятием ограничения.

    Форма разбирается только знакомая: одиннадцать цифр, начинающихся с 7 или 8.
    Всё остальное возвращается как пришло — выдумывать разбивку для номера,
    которого не понимаешь, значит соврать о его структуре.
    """
    if not phone:
        return None
    digits = _DIGITS.sub("", phone)
    if len(digits) == RU_PHONE_DIGITS and digits[0] in {"7", "8"}:
        return f"+7 ({digits[1:4]}) {digits[4:7]}-{digits[7:9]}-{digits[9:]}"
    return phone
