"""Картинка приветствия.

Баннер — украшение, а ``/start`` — главная команда бота, поэтому здесь всё
устроено так, чтобы отсутствие файла или отказ Telegram давали текстовое
приветствие, а не исключение в проде.

Путь считается от модуля, а не от рабочего каталога: файл лежит внутри пакета
``app``, значит едет и в wheel (``packages = ["app"]``), и в образ
(``COPY app ./app``) без единой правки сборки.
"""

from __future__ import annotations

from pathlib import Path

from aiogram.types import (
    FSInputFile,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardMarkup,
)

from app.logging_setup import get_logger

logger = get_logger(__name__)

BANNER_PATH = Path(__file__).resolve().parent.parent / "assets" / "welcome.jpg"

# Подпись под фото Telegram не обрезает, а отвергает сообщение целиком, поэтому
# длину проверяем сами и в крайнем случае отправляем текст отдельно.
CAPTION_LIMIT = 1024

# file_id первой удачной отправки. FSInputFile перезаливает файл на каждый
# /start; после первого раза Telegram уже хранит картинку у себя.
_file_id: str | None = None


def banner_available() -> bool:
    return BANNER_PATH.is_file()


async def send_welcome(
    message: Message,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | None = None,
    parse_mode: str | None = None,
) -> None:
    """Приветствие с баннером, а без баннера — то же приветствие текстом.

    ``parse_mode`` передаётся явно, потому что глобально он выключен
    (``app/main.py``): весь остальной бот пишет простым текстом, и это
    защищает от того, что чужая фамилия с «<» уронит отправку сообщения.
    Приветствие — единственное исключение, и только ради ссылки на базу:
    у сообщения с нижней клавиатурой инлайн-кнопки быть не может, а
    голый адрес с токеном на первом экране читать нечем.
    """
    photo = _photo()
    if photo is None:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return

    caption = text if len(text) <= CAPTION_LIMIT else None
    try:
        sent = await message.answer_photo(
            photo,
            caption=caption,
            reply_markup=reply_markup if caption is not None else None,
            parse_mode=parse_mode if caption is not None else None,
        )
    except Exception:
        # Что бы ни ответил Telegram — приветствие человек получить обязан.
        logger.warning("welcome.photo_failed", path=str(BANNER_PATH))
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
        return

    _remember(sent)
    if caption is None:
        await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)


def _photo() -> str | FSInputFile | None:
    if _file_id is not None:
        return _file_id
    if not BANNER_PATH.is_file():
        logger.warning("welcome.banner_missing", path=str(BANNER_PATH))
        return None
    return FSInputFile(BANNER_PATH)


def _remember(sent: Message) -> None:
    global _file_id
    if sent.photo:
        _file_id = sent.photo[-1].file_id


def forget_file_id() -> None:
    """Сбросить запомненный file_id. Нужно тестам, а не боту."""
    global _file_id
    _file_id = None


__all__ = [
    "BANNER_PATH",
    "CAPTION_LIMIT",
    "banner_available",
    "forget_file_id",
    "send_welcome",
]
