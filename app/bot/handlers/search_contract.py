"""Contract / claim / internal-id lookup.

Answers from our own records first and shows the card. External sources are
queried only when the operator asks for it — one button press away, and the
subject is carried over so nothing has to be retyped.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.bot.common import answer_callback, callback_message, run_and_send_report
from app.bot.handlers.start import menu_markup
from app.bot.keyboards import (
    EXTERNAL_CHECK_PREFIX,
    MENU_PREFIX,
    cancel_keyboard,
    external_check_keyboard,
)
from app.bot.states import ContractSearch
from app.container import Container
from app.domain.enums import SearchType
from app.domain.identity import NameParseError, SearchSubject, VehicleDescriptor, parse_fio
from app.domain.models import InternalDebtorRecord
from app.services.reporting import render_internal_card
from app.utils.formatting import split_message

ASK_QUERY = "Введите номер договора, номер заявки или внутренний ID должника.\n\nПример: EV-20481"
NOT_FOUND = (
    "Во внутренней базе ничего не найдено.\n\n"
    "Проверьте номер или загрузите актуальную выгрузку через /import."
)
MAX_CARDS = 5


def _subject_from_record(record: InternalDebtorRecord, *, fallback: SearchSubject) -> SearchSubject:
    """Promote an internal record into a full subject for external checking.

    This is what makes the contract flow useful: the operator types a contract
    number and the external search runs against the name and date of birth we
    already hold.
    """
    name = None
    if record.full_name:
        try:
            name = parse_fio(record.full_name)
        except NameParseError:
            name = None

    vehicle = None
    if record.vehicle_plate or record.vin:
        vehicle = VehicleDescriptor(plate=record.vehicle_plate, vin=record.vin)

    return SearchSubject(
        search_type=SearchType.PERSON.value if name else fallback.search_type,
        name=name,
        birth_date=record.birth_date,
        phone=record.phone,
        address=record.address,
        contract_number=record.contract_number or fallback.contract_number,
        claim_number=record.claim_number,
        debtor_id=record.debtor_id,
        vehicle=vehicle,
    )


def build_router() -> Router:
    """Build this module's router.

    A factory rather than a module-level singleton: a Router can only be
    attached to one parent, so a shared instance would make a second
    Dispatcher — in tests, or in any future multi-bot setup — impossible.
    """
    router = Router(name="search_contract")

    @router.callback_query(F.data == f"{MENU_PREFIX}:{SearchType.CONTRACT.value}")
    async def start_contract_search(callback: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ContractSearch.waiting_query)
        message = callback_message(callback)
        if message:
            await message.answer(ASK_QUERY, reply_markup=cancel_keyboard())
        await answer_callback(callback)

    @router.message(ContractSearch.waiting_query)
    async def receive_query(
        message: Message, state: FSMContext, container: Container, user_id: int
    ) -> None:
        query = (message.text or "").strip()
        if not query:
            await message.answer(ASK_QUERY, reply_markup=cancel_keyboard())
            return
        await state.clear()

        subject = SearchSubject(
            search_type=SearchType.CONTRACT.value,
            contract_number=query,
            claim_number=query,
            debtor_id=query,
        )
        records = await container.search_service.lookup_internal(subject)
        if not records:
            await message.answer(
                NOT_FOUND,
                reply_markup=await menu_markup(container, user_id),
            )
            return

        for record in records[:MAX_CARDS]:
            chunks = split_message(render_internal_card(record))
            token = container.subject_store.put(_subject_from_record(record, fallback=subject))
            for index, chunk in enumerate(chunks):
                is_last = index == len(chunks) - 1
                await message.answer(
                    chunk,
                    reply_markup=external_check_keyboard(token) if is_last else None,
                )

        hidden = len(records) - MAX_CARDS
        if hidden > 0:
            await message.answer(f"Найдено ещё {hidden} совпадений — уточните запрос.")

    @router.callback_query(F.data.startswith(f"{EXTERNAL_CHECK_PREFIX}:"))
    async def run_external_check(
        callback: CallbackQuery, container: Container, user_id: int
    ) -> None:
        token = (callback.data or "").split(":", maxsplit=1)[-1]
        subject = container.subject_store.get(token)
        message = callback_message(callback)

        if subject is None:
            await answer_callback(callback, "Данные устарели, повторите поиск.")
            return
        await answer_callback(callback)
        if message is None:
            return
        await run_and_send_report(message, container, subject, user_id=user_id)

    return router
