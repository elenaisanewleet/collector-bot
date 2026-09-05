"""ФССП — исполнительные производства через агрегатор NewDB.

The former direct integration with ``api-ip.fssp.gov.ru`` is gone: that service
is retired and answers ``HTTP 410 Gone``. Enforcement proceedings now come from
the NewDB ``fssp_person`` method.

The envelope, the polling and the ``failed``-means-error rule live in
:mod:`app.providers.newdb`, shared with every other NewDB method. What remains
here is what is specific to this one: the parameters ФССП requires, the regions
to search, and the row shape ``fssp_person`` returns —
``Debtor``, ``EnforcementProceeding``, ``SubjectAndDebtAmount``,
``BailiffDepartment`` and ``CompletionDateOrReason``.

That row shape is hard-coded rather than mapped, unlike the other methods,
because it is the one that has been read against a real response.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.domain.enums import (
    REGION_FSSP_CODES,
    MissingInput,
    ProceedingStatus,
    ProviderName,
    ProviderStatus,
    Region,
)
from app.domain.identity import SearchSubject
from app.domain.models import EnforcementProceeding, ProviderResult
from app.logging_setup import get_logger
from app.providers.base import BaseProvider, ProviderUnavailableError
from app.providers.mapping import as_text
from app.providers.newdb import NewDBClient, person_params
from app.utils.dates import parse_date, utcnow
from app.utils.masking import redact_sensitive_json
from app.utils.money import parse_amount

logger = get_logger(__name__)

NEWDB_METHOD = "fssp_person"

# NewDB's own dictionary: 100 searches every ФССП region at once. Used when the
# operator picked a region we hold no code for, so the search widens instead of
# silently targeting the wrong region.
ALL_REGIONS_CODE = 100

MAX_PROCEEDINGS = 100

# Row keys, exactly as the specification names them.
FIELD_DEBTOR = "Debtor"
FIELD_PROCEEDING = "EnforcementProceeding"
FIELD_SUBJECT = "SubjectAndDebtAmount"
FIELD_DEPARTMENT = "BailiffDepartment"
FIELD_COMPLETION = "CompletionDateOrReason"

# "ИВАНОВ ИВАН ИВАНОВИЧ 01.01.1990 Г. МОСКВА" -> name, date, place.
_BIRTH_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")
# "... Сумма долга: 30000.00 руб. Остаток долга по исполнительному документу: 30000.00 руб."
_REMAINING_DEBT_RE = re.compile(r"Остаток долга[^:]*:\s*([\d\s.,]+)")
_TOTAL_DEBT_RE = re.compile(r"Сумма долга\s*:\s*([\d\s.,]+)")
_DEBT_HEAD_RE = re.compile(r"\s*Сумма долга\s*:")
# "88442/25/66049-ИП от 09.09.2025" -> "88442/25/66049-ИП"
_PROCEEDING_TAIL_RE = re.compile(r"\s+от\s+\d{2}\.\d{2}\.\d{4}.*$")


class FSSPProvider(BaseProvider):
    """Enforcement proceedings for an individual, via NewDB."""

    name = ProviderName.FSSP
    title = "ФССП"

    def __init__(self, settings: Settings, client: NewDBClient | None = None) -> None:
        self._settings = settings
        self._client = client or NewDBClient(settings)

    @property
    def is_configured(self) -> bool:
        return self._settings.fssp_configured

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if subject.name is None:
            return self.insufficient_query(
                "Для поиска в ФССП нужно ФИО", missing=(MissingInput.NAME,)
            )
        if subject.birth_date is None:
            # NewDB requires dob. Saying so is honest; querying without it and
            # reporting the rejection as "ничего не найдено" would not be.
            return self.insufficient_query(
                "Для поиска в ФССП нужна дата рождения — источник требует её обязательно",
                missing=(MissingInput.BIRTH_DATE,),
            )

        base = person_params(
            last_name=subject.name.last_name,
            first_name=subject.name.first_name,
            middle_name=subject.name.middle_name,
            birth_date=subject.birth_date.strftime("%Y-%m-%d"),
        )
        param_sets = [{**base, "regioncode": code} for code in _region_codes(subject.regions)]

        response = await self._client.call(NEWDB_METHOD, *param_sets)
        records, unreadable = _parse_proceedings(response.rows)
        if unreadable:
            # Тот же счёт потерь, что и у методов с картой полей, и по той же
            # причине. Ключи здесь захардкожены, потому что их читали с живого
            # ответа, — но «прочитано однажды» не значит «не изменится», а строка
            # ответа, которую не удалось разобрать, молча уходила в пропасть:
            # ноль производств у самого вероятного включённого источника — это
            # +5 к взыскиваемости и «активных производств не найдено» в отчёте.
            logger.warning("fssp.unreadable_rows", unreadable=unreadable, parsed=len(records))
            raise ProviderUnavailableError(
                "unexpected_schema",
                f"Не удалось разобрать {unreadable} из {unreadable + len(records)} "
                "строк ответа ФССП",
            )
        unique = _dedupe(records)
        notes: tuple[str, ...] = ()
        if len(unique) > MAX_PROCEEDINGS:
            # Обрезка списка — тоже неполнота, и признаваться в ней обязан сам
            # ответ: иначе сотое производство отличалось бы от их отсутствия
            # только тем, что о нём никто не узнал.
            notes = (
                f"Показаны первые {MAX_PROCEEDINGS} производств из {len(unique)}, "
                "полученных от источника",
            )
            unique = unique[:MAX_PROCEEDINGS]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if unique else ProviderStatus.NO_RESULTS,
            records=list(unique),
            is_partial=bool(notes),
            notes=notes,
            # Тот же фильтр персданных, что у остальных методов NewDB: сырое
            # тело хранится без чужих СНИЛСов и адресов проживания, каким бы ни
            # был флаг. См. app/utils/masking.py.
            raw_response=(
                redact_sensitive_json(response.raw) if self._settings.store_raw_responses else None
            ),
        )


# ---------------------------------------------------------------- rows


def _parse_proceedings(rows: list[Any]) -> tuple[list[EnforcementProceeding], int]:
    """Производства и число строк, которые прочитать не удалось.

    Второе значение и есть смысл функции. Строка не-объект и строка без номера
    производства раньше просто пропускались: список получался короче ответа, и
    ничто ниже по течению не могло об этом узнать — отчёт печатал «активных
    исполнительных производств не найдено», а скоринг добавлял за это плюс.
    Номер производства — единственное, чем производство можно показать и
    проверить, поэтому строка без него не запись, а потеря.
    """
    fetched_at = utcnow()
    proceedings: list[EnforcementProceeding] = []
    unreadable = 0
    for row in rows:
        if not isinstance(row, Mapping):
            unreadable += 1
            continue
        record = _to_proceeding(row, fetched_at)
        if record is None:
            unreadable += 1
            continue
        proceedings.append(record)
    return proceedings, unreadable


def _to_proceeding(row: Mapping[str, Any], fetched_at: datetime) -> EnforcementProceeding | None:
    number = _proceeding_number(as_text(row.get(FIELD_PROCEEDING)))
    if number is None:
        return None

    debtor_name, debtor_birth_date = _split_debtor(as_text(row.get(FIELD_DEBTOR)))
    subject_text = as_text(row.get(FIELD_SUBJECT))
    completion = as_text(row.get(FIELD_COMPLETION))

    return EnforcementProceeding(
        proceeding_number=number,
        debtor_name=debtor_name,
        debtor_birth_date=debtor_birth_date,
        amount=_debt_amount(subject_text),
        # The source states a completion date or reason only for closed
        # proceedings, so its presence is the signal.
        status=ProceedingStatus.CLOSED if completion else ProceedingStatus.ACTIVE,
        status_text=completion,
        subject=_subject_text(subject_text),
        department=as_text(row.get(FIELD_DEPARTMENT)),
        fetched_at=fetched_at,
    )


def _proceeding_number(raw: str | None) -> str | None:
    """``88442/25/66049-ИП от 09.09.2025`` -> ``88442/25/66049-ИП``.

    The trailing date is dropped so the same proceeding returned for two regions
    deduplicates on one key.
    """
    if not raw:
        return None
    number = _PROCEEDING_TAIL_RE.sub("", raw).strip()
    return number or None


def _split_debtor(raw: str | None) -> tuple[str | None, date | None]:
    """``ИВАНОВ ИВАН ИВАНОВИЧ 01.01.1990 Г. МОСКВА`` -> name, date of birth.

    The place of birth is discarded: it is free text with no domain field, and
    the date is the part that drives identity matching.
    """
    if not raw:
        return None, None
    match = _BIRTH_DATE_RE.search(raw)
    if match is None:
        return raw.strip() or None, None
    name = raw[: match.start()].strip()
    return name or None, parse_date(match.group(1))


def _debt_amount(raw: str | None) -> Decimal | None:
    """Extract what the debtor still owes on this proceeding.

    The source states both the original sum and the outstanding balance. The
    balance is what matters for recovery — it is the claim still competing with
    ours — so it wins, with the original sum as the fallback when the source
    omits a remainder.
    """
    if not raw:
        return None
    for pattern in (_REMAINING_DEBT_RE, _TOTAL_DEBT_RE):
        match = pattern.search(raw)
        if match is None:
            continue
        amount = parse_amount(match.group(1))
        if amount is not None:
            return amount
    return None


def _subject_text(raw: str | None) -> str | None:
    """The purpose of the proceeding, without the amounts appended to it."""
    if not raw:
        return None
    head = _DEBT_HEAD_RE.split(raw, maxsplit=1)[0].strip()
    return head or raw.strip() or None


# ---------------------------------------------------------------- regions


def _region_codes(regions: tuple[str, ...]) -> list[int]:
    """Translate domain regions into NewDB region codes.

    A region we hold no code for falls back to "all regions" rather than to an
    arbitrary one: a broader search is a defensible default, a wrong region is
    not.
    """
    codes: list[int] = []
    for value in regions:
        try:
            region = Region(value)
        except ValueError:
            continue
        code = REGION_FSSP_CODES.get(region)
        if code is not None and code not in codes:
            codes.append(code)
    return codes or [ALL_REGIONS_CODE]


def _dedupe(records: list[EnforcementProceeding]) -> list[EnforcementProceeding]:
    """Multi-region searches legitimately return the same proceeding twice."""
    seen: set[str] = set()
    unique: list[EnforcementProceeding] = []
    for record in records:
        key = record.proceeding_number.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def build_fssp_provider(settings: Settings) -> FSSPProvider:
    provider = FSSPProvider(settings)
    if not provider.is_configured:
        logger.info("fssp.not_configured", reason="NEWDB_API_KEY/NEWDB_BASE_URL missing")
    return provider


__all__ = ["FSSPProvider", "build_fssp_provider"]
