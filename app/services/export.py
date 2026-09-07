"""Выгрузка очереди в CSV.

У оператора с восемьюстами должниками рабочий инструмент — таблица. Экспорт
даёт ту же очередь, что и бот, но пригодную для фильтров, разметки и передачи
юристу.

Кодировка — UTF-8 с BOM: без неё Excel открывает кириллицу как кракозябры,
а файл открывают именно в Excel.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Sequence

from app.db.models import BatchItem
from app.domain.verdict import FEE_BASIS_TITLES, VERDICT_TITLES, FeeBasis, Verdict
from app.utils.dates import format_date
from app.utils.masking import mask_name, mask_phone

QUEUE_COLUMNS = (
    "verdict",
    "вердикт",
    "обоснование",
    "debtor_id",
    "фио",
    "дата_рождения",
    "телефон",
    "договор",
    "долг",
    "пошлина",
    "основание_пошлины",
    "score",
    "уверенность_%",
    "госномер",
    "ошибка",
)


def queue_to_csv(
    items: Sequence[BatchItem], *, include_phone: bool = False, mask_personal: bool = False
) -> bytes:
    """Собрать CSV очереди.

    Телефон по умолчанию маскируется: файл уходит из системы, и полный номер в
    нём — это выгрузка персональных данных, которую никто не запрашивал.

    ``mask_personal`` — для файла, который отдаётся по ссылке, а не владельцу в
    личном чате. Страница очереди маскирует ФИО намеренно («полное ФИО по одной
    ссылке на восемьсот строк — это выгрузка базы»), а соседняя кнопка
    «Таблицей» отдавала ровно то, что страница прятала, и сверх того дату
    рождения и госномер. Под этим флагом файл повторяет страницу: маска ФИО, без
    даты рождения и без госномера. Опознать строку по-прежнему есть чем —
    остаются внутренний номер должника и номер договора.

    Флаг, а не смена умолчания: владельцу в бот уходит полный файл, и это
    осознанно — он идёт юристу, ради чего выгрузка и написана.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(QUEUE_COLUMNS)

    for item in items:
        debtor = item.debtor
        phone = (
            (debtor.phone if include_phone else None)
            or debtor.phone_masked
            or mask_phone(debtor.phone)
            if debtor
            else None
        )
        fio = (mask_name(debtor.fio) if mask_personal else debtor.fio) if debtor else ""
        birth = (
            ""
            if mask_personal or not debtor or not debtor.birth_date
            else format_date(debtor.birth_date)
        )
        plate = "" if mask_personal or not debtor else debtor.vehicle_plate
        writer.writerow(
            _safe(
                (
                    item.verdict,
                    _verdict_title(item.verdict),
                    item.headline,
                    debtor.external_debtor_id if debtor else "",
                    fio or "",
                    birth,
                    phone or "",
                    debtor.contract_number if debtor else "",
                    _amount(item.debt_amount),
                    _amount(item.state_fee),
                    _fee_basis_title(item.fee_basis),
                    item.score if item.score is not None else "",
                    item.confidence,
                    plate or "",
                    item.error or "",
                )
            )
        )

    return buffer.getvalue().encode("utf-8-sig")


#: Символы, с которых Excel и LibreOffice начинают читать ячейку как формулу.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _safe(row: Sequence[object]) -> list[object]:
    """Обезвредить ячейки, которые Excel примет за формулу.

    Фамилия «-Оглы», договор «=ЭВ-1», подставленный в 1С, или что угодно,
    начинающееся с ``=``, ``+``, ``-``, ``@``, выполняется при открытии файла.
    Это не гипотеза про злоумышленника: файл идёт юристу и открывается в Excel,
    а формула из чужой строки в лучшем случае покажет ошибку вместо фамилии.

    Апостроф впереди — способ, который понимают и Excel, и LibreOffice, и он
    сохраняет значение читаемым. Числа и пустые ячейки не трогаются: они не
    строки, и портить их незачем.
    """
    return [
        f"'{value}" if isinstance(value, str) and value.startswith(_FORMULA_LEAD) else value
        for value in row
    ]


def _verdict_title(value: str) -> str:
    try:
        return VERDICT_TITLES[Verdict(value)]
    except (ValueError, KeyError):
        return value


def _fee_basis_title(value: str) -> str:
    try:
        return FEE_BASIS_TITLES[FeeBasis(value)]
    except (ValueError, KeyError):
        return value


def _amount(value: object) -> str:
    """Десятичная запятая — иначе Excel в русской локали не считает столбец."""
    return str(value).replace(".", ",") if value is not None else ""
