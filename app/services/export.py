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
from app.utils.masking import mask_phone

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


def queue_to_csv(items: Sequence[BatchItem], *, include_phone: bool = False) -> bytes:
    """Собрать CSV очереди.

    Телефон по умолчанию маскируется: файл уходит из системы, и полный номер в
    нём — это выгрузка персональных данных, которую никто не запрашивал.
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
        writer.writerow(
            (
                item.verdict,
                _verdict_title(item.verdict),
                item.headline,
                debtor.external_debtor_id if debtor else "",
                debtor.fio if debtor else "",
                format_date(debtor.birth_date) if debtor and debtor.birth_date else "",
                phone or "",
                debtor.contract_number if debtor else "",
                _amount(item.debt_amount),
                _amount(item.state_fee),
                _fee_basis_title(item.fee_basis),
                item.score if item.score is not None else "",
                item.confidence,
                debtor.vehicle_plate if debtor else "",
                item.error or "",
            )
        )

    return buffer.getvalue().encode("utf-8-sig")


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
