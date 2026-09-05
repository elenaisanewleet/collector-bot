"""Conversation state machines.

One group per search flow. Each step stores only what it needs into the FSM
context, and :func:`/cancel` clears it.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup

# Группы ``PersonSearch`` здесь больше нет, и это решение, а не уборка. Поиск
# по человеку ведёт накопительная карточка (:mod:`app.bot.handlers.query_card`),
# а она сознательно не состояние: состояние забрало бы себе весь свободный
# текст и сняло бы ``StateFilter(None)`` с последнего роутера — тот самый
# фильтр, на котором держится ввод госномера, VIN, адреса и договора. Что
# именно карточка ждёт, помнит колонка ``query_cards.awaiting_field``.
#
# Следствие: ``/cancel`` карточку не сбрасывает. Так и надо — карточка не
# диалог, её очищает только своя кнопка.


class ContractSearch(StatesGroup):
    waiting_query = State()


class PlateSearch(StatesGroup):
    waiting_plate = State()


class VinSearch(StatesGroup):
    waiting_vin = State()


class VehicleSearch(StatesGroup):
    waiting_make_model = State()
    waiting_identifier = State()
    waiting_fio = State()


class AddressSearch(StatesGroup):
    waiting_address = State()
    waiting_fio = State()


class PassportSearch(StatesGroup):
    waiting_passport = State()


class CsvImport(StatesGroup):
    waiting_document = State()


class BatchCheck(StatesGroup):
    waiting_confirm = State()
