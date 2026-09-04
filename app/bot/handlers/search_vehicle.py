"""Plate, VIN and free-form vehicle searches.

The validation here is real and useful: it catches typos before a search runs
and it feeds matching against our own contracts, where the plate and VIN came
from us. Resolving a plate to an owner is not something this tool does — no
lawful provider is connected, and the report says so plainly rather than
returning an empty result that reads like "clean".
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.common import answer_callback, callback_message, run_and_send_report
from app.bot.keyboards import (
    MENU_PREFIX,
    SKIP_CALLBACK,
    cancel_keyboard,
    skip_keyboard,
)
from app.bot.states import PlateSearch, VehicleSearch, VinSearch
from app.container import Container
from app.domain.enums import SearchType
from app.domain.identity import (
    VIN_LENGTH,
    NameParseError,
    SearchSubject,
    VehicleDescriptor,
    normalize_plate,
    normalize_vin,
    parse_fio,
)

ASK_PLATE = "Введите госномер.\n\nПример: А123ВС77"
ASK_VIN = f"Введите VIN ({VIN_LENGTH} символов).\n\nПример: XW8ZZZ61ZKG011111"
ASK_MAKE_MODEL = "Введите марку и модель.\n\nПример: Skoda Octavia"
ASK_VEHICLE_IDENTIFIER = (
    "Госномер или VIN, если известны. Можно пропустить — но без них "
    "определить конкретный автомобиль невозможно."
)
ASK_VEHICLE_FIO = "ФИО владельца, если известно. Можно пропустить."
BAD_PLATE = "Не похоже на российский госномер. Пример: А123ВС77.\nПопробуйте ещё раз."
BAD_VIN = (
    f"VIN должен состоять ровно из {VIN_LENGTH} символов "
    "(латиница без I, O, Q — и цифры).\nПопробуйте ещё раз."
)
MAKE_MODEL_ONLY_NOTE = (
    "ℹ️ Марка и модель описывают тип автомобиля, а не конкретный. "
    "Поиск выполнен только по нашим данным."
)


# ---------------------------------------------------------------- plate


# ---------------------------------------------------------------- vin


# ---------------------------------------------------------------- vehicle


async def _finish_vehicle_search(
    message: Message,
    container: Container,
    data: dict[str, object],
    *,
    name_full: str | None,
    user_id: int,
) -> None:
    vehicle = VehicleDescriptor(
        make=_optional(data.get("make")),
        model=_optional(data.get("model")),
        plate=_optional(data.get("plate")),
        vin=_optional(data.get("vin")),
    )
    if not vehicle.has_unique_identifier and not name_full:
        # Refusing to guess: a make and model alone identify no one.
        await message.answer(MAKE_MODEL_ONLY_NOTE)

    name = None
    if name_full:
        try:
            name = parse_fio(name_full)
        except NameParseError:
            name = None

    subject = SearchSubject(
        search_type=SearchType.VEHICLE.value,
        name=name,
        vehicle=vehicle,
    )
    await run_and_send_report(message, container, subject, user_id=user_id)


def _optional(value: object) -> str | None:
    return str(value) if value not in (None, "") else None


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="search_vehicle")

    @router.callback_query(F.data == f"{MENU_PREFIX}:{SearchType.VEHICLE_PLATE.value}")
    async def start_plate_search(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(PlateSearch.waiting_plate)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_PLATE, reply_markup=cancel_keyboard())
        await answer_callback(callback)

    @router.message(PlateSearch.waiting_plate)
    async def receive_plate(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        plate = normalize_plate(message.text or "")
        if plate is None:
            await message.answer(BAD_PLATE, reply_markup=cancel_keyboard())
            return
        await state.clear()
        subject = SearchSubject(
            search_type=SearchType.VEHICLE_PLATE.value,
            vehicle=VehicleDescriptor(plate=plate),
        )
        await run_and_send_report(message, container, subject, user_id=user_id)

    @router.callback_query(F.data == f"{MENU_PREFIX}:{SearchType.VIN.value}")
    async def start_vin_search(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(VinSearch.waiting_vin)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_VIN, reply_markup=cancel_keyboard())
        await answer_callback(callback)

    @router.message(VinSearch.waiting_vin)
    async def receive_vin(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        vin = normalize_vin(message.text or "")
        if vin is None:
            await message.answer(BAD_VIN, reply_markup=cancel_keyboard())
            return
        await state.clear()
        subject = SearchSubject(
            search_type=SearchType.VIN.value, vehicle=VehicleDescriptor(vin=vin)
        )
        await run_and_send_report(message, container, subject, user_id=user_id)

    @router.callback_query(F.data == f"{MENU_PREFIX}:{SearchType.VEHICLE.value}")
    async def start_vehicle_search(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(VehicleSearch.waiting_make_model)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_MAKE_MODEL, reply_markup=cancel_keyboard())
        await answer_callback(callback)

    @router.message(VehicleSearch.waiting_make_model)
    async def receive_make_model(message: Message, state: FSMContext) -> None:
        parts = (message.text or "").split()
        if not parts:
            await message.answer(ASK_MAKE_MODEL, reply_markup=cancel_keyboard())
            return
        await state.update_data(make=parts[0], model=" ".join(parts[1:]) or None)
        await state.set_state(VehicleSearch.waiting_identifier)
        await message.answer(ASK_VEHICLE_IDENTIFIER, reply_markup=skip_keyboard())

    @router.message(VehicleSearch.waiting_identifier)
    async def receive_vehicle_identifier(message: Message, state: FSMContext) -> None:
        raw = (message.text or "").strip()
        plate = normalize_plate(raw)
        vin = normalize_vin(raw)
        if plate is None and vin is None:
            await message.answer(
                "Не распознан ни госномер, ни VIN. Попробуйте ещё раз или пропустите.",
                reply_markup=skip_keyboard(),
            )
            return
        await state.update_data(plate=plate, vin=vin)
        await state.set_state(VehicleSearch.waiting_fio)
        await message.answer(ASK_VEHICLE_FIO, reply_markup=skip_keyboard())

    @router.callback_query(VehicleSearch.waiting_identifier, F.data == SKIP_CALLBACK)
    async def skip_vehicle_identifier(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(VehicleSearch.waiting_fio)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_VEHICLE_FIO, reply_markup=skip_keyboard())
        await answer_callback(callback)

    @router.message(VehicleSearch.waiting_fio)
    async def receive_vehicle_fio(
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
        await _finish_vehicle_search(message, container, data, name_full=name.full, user_id=user_id)

    @router.callback_query(VehicleSearch.waiting_fio, F.data == SKIP_CALLBACK)
    async def skip_vehicle_fio(
        callback: CallbackQuery, state: FSMContext, container: Container, user_id: int
    ) -> None:
        data = await state.get_data()
        await state.clear()
        await answer_callback(callback)
        message = callback_message(callback)
        if message:
            await _finish_vehicle_search(message, container, data, name_full=None, user_id=user_id)

    return router
