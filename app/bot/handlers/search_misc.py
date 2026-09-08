"""Address and passport searches.

Both are internal-matching tools in this MVP.

*   Address: used to find our own records at that address. Resolving an address
    to its owner is not something this tool does — that requires lawful access
    to the property register, which is not connected.
*   Passport: used only against our own records. It is never sent to an external
    provider, and with ``STORE_SENSITIVE_IDENTIFIERS=false`` (the default) it is
    never written to disk in full.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.common import answer_callback, callback_message, run_and_send_report
from app.bot.keyboards import MENU_PREFIX, SKIP_CALLBACK, cancel_keyboard, skip_keyboard
from app.bot.states import AddressSearch, PassportSearch
from app.container import Container
from app.domain.enums import SearchType
from app.domain.identity import (
    NameParseError,
    SearchSubject,
    normalize_address,
    normalize_passport,
    parse_fio,
)

ASK_ADDRESS = "Введите адрес.\n\nПример: Москва, ул. Примерная, д. 1, кв. 2"
ASK_ADDRESS_FIO = "ФИО, если известно. Можно пропустить."
ASK_PASSPORT = (
    "Введите серию и номер паспорта (10 цифр).\n\n"
    "Паспорт используется только для сопоставления с нашей базой "
    "и не передаётся во внешние источники."
)
BAD_PASSPORT = "Нужно ровно 10 цифр (серия и номер). Попробуйте ещё раз."
ADDRESS_NOTE = (
    "ℹ️ Поиск по адресу выполняется по нашей базе. Определение собственника по адресу не подключено."
)


# ---------------------------------------------------------------- address


async def _finish_address(
    message: Message,
    container: Container,
    address: str,
    *,
    name_full: str | None,
    user_id: int,
) -> None:
    await message.answer(ADDRESS_NOTE)
    name = None
    if name_full:
        try:
            name = parse_fio(name_full)
        except NameParseError:
            name = None
    subject = SearchSubject(search_type=SearchType.ADDRESS.value, address=address, name=name)
    await run_and_send_report(message, container, subject, user_id=user_id)


# ---------------------------------------------------------------- passport


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="search_misc")

    @router.callback_query(F.data == f"{MENU_PREFIX}:{SearchType.ADDRESS.value}")
    async def start_address_search(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(AddressSearch.waiting_address)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_ADDRESS, reply_markup=cancel_keyboard())
        await answer_callback(callback)

    @router.message(AddressSearch.waiting_address)
    async def receive_address(message: Message, state: FSMContext) -> None:
        address = normalize_address(message.text or "")
        if not address:
            await message.answer(ASK_ADDRESS, reply_markup=cancel_keyboard())
            return
        await state.update_data(address=address)
        await state.set_state(AddressSearch.waiting_fio)
        await message.answer(ASK_ADDRESS_FIO, reply_markup=skip_keyboard())

    @router.message(AddressSearch.waiting_fio)
    async def receive_address_fio(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        try:
            name = parse_fio(message.text or "")
        except NameParseError as exc:
            await message.answer(
                f"{exc}\n\nПопробуйте ещё раз или пропустите.", reply_markup=skip_keyboard()
            )
            return
        data = await state.get_data()
        await state.clear()
        await _finish_address(
            message, container, str(data.get("address", "")), name_full=name.full, user_id=user_id
        )

    @router.callback_query(AddressSearch.waiting_fio, F.data == SKIP_CALLBACK)
    async def skip_address_fio(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        data = await state.get_data()
        await state.clear()
        await answer_callback(callback)
        message = callback_message(callback)
        if message:
            await _finish_address(
                message, container, str(data.get("address", "")), name_full=None, user_id=user_id
            )

    @router.callback_query(F.data == f"{MENU_PREFIX}:{SearchType.PASSPORT.value}")
    async def start_passport_search(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(PassportSearch.waiting_passport)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_PASSPORT, reply_markup=cancel_keyboard())
        await answer_callback(callback)

    @router.message(PassportSearch.waiting_passport)
    async def receive_passport(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        passport = normalize_passport(message.text or "")
        if passport is None:
            await message.answer(BAD_PASSPORT, reply_markup=cancel_keyboard())
            return
        await state.clear()

        # Сообщение оператора больше не удаляется, и номер печатается целиком.
        # Прежнее поведение держалось на обещании «номер не сохраняю, сообщение
        # удалю»; владелица его сняла — «нам надо наоборот сохранять эти
        # номера», — и карточка ведёт себя так же (``Card.shown``).
        await message.answer(f"Ищу по паспорту {passport}…")
        subject = SearchSubject(search_type=SearchType.PASSPORT.value, passport=passport)
        await run_and_send_report(message, container, subject, user_id=user_id)

    return router
