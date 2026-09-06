"""``/import`` — loading a debtor export.

Validates the upload before reading it (type, size), then parses defensively:
malformed rows are counted and reported, never fatal.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Document, Message

from app.bot.common import answer_callback, callback_message
from app.bot.keyboards import MENU_PREFIX, cancel_keyboard, main_menu
from app.bot.states import CsvImport
from app.container import Container
from app.logging_setup import get_logger
from app.providers.internal.csv_schema import CANONICAL_COLUMNS, CsvFormatError
from app.services.import_service import ImportReport

logger = get_logger(__name__)

ALLOWED_EXTENSIONS = (".csv", ".txt", ".xlsx", ".xlsm", ".xls")
ALLOWED_MIME_PREFIXES = (
    "text/",
    "application/csv",
    "application/vnd.ms-excel",
    # xlsx: длинное имя типа целиком, Telegram отдаёт его как есть.
    "application/vnd.openxmlformats-officedocument",
    # Некоторые клиенты присылают книгу без типа вовсе.
    "application/octet-stream",
)

ASK_DOCUMENT = (
    "Отправьте выгрузку должников — файл Excel или CSV.\n\n"
    f"Ожидаемые колонки:\n{', '.join(CANONICAL_COLUMNS)}\n\n"
    "Обязательна хотя бы одна из: fio или contract_number.\n"
    "Подойдёт выгрузка из 1С как есть: лишние колонки не мешают, "
    "названия распознаются по-русски, шапка может быть не первой строкой.\n"
    "CSV — в кодировке UTF-8 или Windows-1251."
)
NOT_A_DOCUMENT = "Нужно отправить файл документом (не фото и не текстом)."
WRONG_TYPE = "Похоже, это не выгрузка. Ожидается файл Excel (.xlsx) или CSV."
DOWNLOAD_FAILED = "Не удалось скачать файл. Попробуйте ещё раз."


def _is_export(document: Document) -> bool:
    name = (document.file_name or "").lower()
    if name.endswith(ALLOWED_EXTENSIONS):
        return True
    mime = (document.mime_type or "").lower()
    return mime.startswith(ALLOWED_MIME_PREFIXES)


async def _download(message: Message, document: Document) -> bytes | None:
    bot = message.bot
    if bot is None:  # pragma: no cover - always present in a live update
        return None
    try:
        buffer = await bot.download(document.file_id)
    except Exception:
        logger.warning("import.download_failed", file_id=document.file_id)
        return None
    if buffer is None:
        return None
    return buffer.read()


def render_import_report(report: ImportReport) -> str:
    lines = [
        "Импорт завершён.",
        "",
        f"Всего строк: {report.total_rows}",
        f"Импортировано: {report.imported}",
        f"Пропущено: {report.skipped}",
        f"Ошибки: {report.failed}",
    ]
    if report.created or report.updated:
        lines.append("")
        lines.append(f"Новых записей: {report.created}, обновлено: {report.updated}")
    if report.errors:
        lines.append("")
        lines.append("Ошибочные строки:")
        lines.extend(f"• {item}" for item in report.errors)
    if report.warnings:
        lines.append("")
        lines.append("Предупреждения:")
        lines.extend(f"• {item}" for item in report.warnings)
    return "\n".join(lines)


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="import_csv")

    @router.message(Command("import"))
    async def handle_import_command(message: Message, state: FSMContext) -> None:
        await state.set_state(CsvImport.waiting_document)
        await message.answer(ASK_DOCUMENT, reply_markup=cancel_keyboard())

    @router.callback_query(F.data == f"{MENU_PREFIX}:import")
    async def handle_import_callback(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(CsvImport.waiting_document)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_DOCUMENT, reply_markup=cancel_keyboard())
        await answer_callback(callback)

    @router.message(CsvImport.waiting_document, F.document)
    async def receive_document(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        document = message.document
        if document is None:  # pragma: no cover - guarded by the F.document filter
            await message.answer(NOT_A_DOCUMENT, reply_markup=cancel_keyboard())
            return

        if not _is_export(document):
            await message.answer(WRONG_TYPE, reply_markup=cancel_keyboard())
            return

        limit = container.settings.max_import_file_bytes
        if document.file_size and document.file_size > limit:
            await message.answer(
                f"Файл слишком большой: {document.file_size // 1024} КБ, "
                f"допустимо до {limit // 1024} КБ.",
                reply_markup=cancel_keyboard(),
            )
            return

        payload = await _download(message, document)
        if payload is None:
            await message.answer(DOWNLOAD_FAILED, reply_markup=cancel_keyboard())
            return

        await state.clear()
        try:
            report = await container.import_service.import_bytes(payload, telegram_user_id=user_id)
        except CsvFormatError as exc:
            await message.answer(f"Импорт не выполнен.\n\n{exc}", reply_markup=main_menu())
            return

        await message.answer(render_import_report(report), reply_markup=main_menu())

    @router.message(CsvImport.waiting_document)
    async def reject_non_document(message: Message) -> None:
        await message.answer(NOT_A_DOCUMENT, reply_markup=cancel_keyboard())

    return router
