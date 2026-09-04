"""Conversation state machines.

One group per search flow. Each step stores only what it needs into the FSM
context, and :func:`/cancel` clears it.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PersonSearch(StatesGroup):
    waiting_fio = State()
    waiting_birth_date = State()
    waiting_phone = State()
    waiting_region = State()


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
