"""Conversation state machines.

One group per search flow. Each step stores only what it needs into the FSM
context, and :func:`/cancel` clears it.
"""

from __future__ import annotations

from aiogram.fsm.state import State, StatesGroup


class PersonSearch(StatesGroup):
    """Поиск по человеку: одна строка на входе и один вопрос при нужде.

    Пять состояний свернулись в два, и это не экономия кода, а суть правки. ФИО,
    дата рождения, телефон, регион и паспорт спрашивались подряд, четыре из пяти
    можно было пропустить — то есть бот требовал того, без чего прекрасно
    обходится. Теперь оператор пишет всё, что знает, одной строкой, а
    :mod:`app.bot.identifiers` разбирает её.

    ``waiting_field`` — единственный уточняющий шаг, и живёт он в двух режимах.
    До запуска в данных лежит ``raw`` (исходная строка, которую перечитают
    заново с поправкой), после отчёта — ``token`` субъекта из
    :class:`~app.services.subject_store.SubjectStore`. Поле ``field`` говорит,
    что именно ждут. Паспорт в данные не кладётся никогда: он живёт ровно до
    ``SearchSubject``.
    """

    waiting_query = State()
    waiting_field = State()


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
