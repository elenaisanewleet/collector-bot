"""Individual (физлицо) search — the primary flow.

Collects a name, then optionally a date of birth and a phone, then a region.
Each optional step can be skipped; skipping costs match confidence rather than
blocking the search, and the report says so.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.common import answer_callback, callback_message, run_and_send_report
from app.bot.keyboards import (
    MENU_PREFIX,
    REGION_COMBINED,
    REGION_PREFIX,
    SKIP_CALLBACK,
    cancel_keyboard,
    region_keyboard,
    skip_keyboard,
)
from app.bot.states import PersonSearch
from app.container import Container
from app.domain.enums import Region, SearchType
from app.domain.identity import (
    NameParseError,
    PersonName,
    SearchSubject,
    normalize_phone,
    parse_fio,
)
from app.utils.dates import parse_date

ASK_FIO = "Введите ФИО должника.\n\nПример: Иванов Иван Иванович\nОтчество можно не указывать."
ASK_BIRTH_DATE = (
    "Дата рождения в формате ДД.ММ.ГГГГ.\n\n"
    "Можно пропустить — но без неё совпадения будут только предположительными."
)
ASK_PHONE = "Телефон должника (для сопоставления с нашей базой). Можно пропустить."
ASK_REGION = "Выберите регион проверки:"
BAD_DATE = "Не удалось разобрать дату. Формат: ДД.ММ.ГГГГ. Попробуйте ещё раз или пропустите."
BAD_PHONE = "Не похоже на российский номер. Попробуйте ещё раз или пропустите."


def _regions_for(choice: str) -> tuple[str, ...]:
    """``Москва + МО`` runs both regions and merges the results."""
    if choice == REGION_COMBINED:
        return (Region.MOSCOW.value, Region.MOSCOW_OBLAST.value)
    try:
        return (Region(choice).value,)
    except ValueError:
        return (Region.OTHER.value,)


def _build_subject(data: dict[str, object], regions: tuple[str, ...]) -> SearchSubject:
    name = PersonName(
        last_name=str(data["last_name"]),
        first_name=str(data["first_name"]),
        middle_name=_optional_str(data.get("middle_name")),
    )
    birth_date_raw = _optional_str(data.get("birth_date"))
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=name,
        birth_date=parse_date(birth_date_raw) if birth_date_raw else None,
        phone=_optional_str(data.get("phone")),
        regions=regions,
    )


def _optional_str(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="search_person")

    @router.callback_query(F.data == f"{MENU_PREFIX}:{SearchType.PERSON.value}")
    async def start_person_search(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(PersonSearch.waiting_fio)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_FIO, reply_markup=cancel_keyboard())
        await answer_callback(callback)

    @router.message(PersonSearch.waiting_fio)
    async def receive_fio(message: Message, state: FSMContext) -> None:
        try:
            name = parse_fio(message.text or "")
        except NameParseError as exc:
            # Never guess how to split a malformed name — ask again.
            await message.answer(f"{exc}\n\nПопробуйте ещё раз.", reply_markup=cancel_keyboard())
            return

        await state.update_data(
            last_name=name.last_name,
            first_name=name.first_name,
            middle_name=name.middle_name,
        )
        await state.set_state(PersonSearch.waiting_birth_date)
        await message.answer(ASK_BIRTH_DATE, reply_markup=skip_keyboard())

    @router.message(PersonSearch.waiting_birth_date)
    async def receive_birth_date(message: Message, state: FSMContext) -> None:
        parsed = parse_date(message.text or "")
        if parsed is None:
            await message.answer(BAD_DATE, reply_markup=skip_keyboard())
            return
        await state.update_data(birth_date=parsed.isoformat())
        await state.set_state(PersonSearch.waiting_phone)
        await message.answer(ASK_PHONE, reply_markup=skip_keyboard())

    @router.callback_query(PersonSearch.waiting_birth_date, F.data == SKIP_CALLBACK)
    async def skip_birth_date(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(PersonSearch.waiting_phone)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_PHONE, reply_markup=skip_keyboard())
        await answer_callback(callback)

    @router.message(PersonSearch.waiting_phone)
    async def receive_phone(message: Message, state: FSMContext) -> None:
        normalized = normalize_phone(message.text or "")
        if normalized is None:
            await message.answer(BAD_PHONE, reply_markup=skip_keyboard())
            return
        await state.update_data(phone=normalized)
        await state.set_state(PersonSearch.waiting_region)
        await message.answer(ASK_REGION, reply_markup=region_keyboard())

    @router.callback_query(PersonSearch.waiting_phone, F.data == SKIP_CALLBACK)
    async def skip_phone(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(PersonSearch.waiting_region)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_REGION, reply_markup=region_keyboard())
        await answer_callback(callback)

    @router.callback_query(PersonSearch.waiting_region, F.data.startswith(f"{REGION_PREFIX}:"))
    async def receive_region(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        choice = (callback.data or "").split(":", maxsplit=1)[-1]
        regions = _regions_for(choice)

        data = await state.get_data()
        await state.clear()
        await answer_callback(callback)

        message = callback_message(callback)
        if message is None:
            return

        subject = _build_subject(data, regions)
        await run_and_send_report(message, container, subject, user_id=user_id)

    return router
