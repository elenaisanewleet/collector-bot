"""Находки по номеру телефона: журнал того, кого владелец пробил.

Зачем журнал отдельно от карточки. Карточка — черновик одного должника: её
перезаписывает следующий номер и стирает кнопка «Очистить». Владелец же
спрашивает не про одного человека, а про всех сразу — «кого я пробил и кого из
них в базе нет». Это список, и место списка в вебе, а не в переписке.

Отметка «новый клиент» считается здесь, в момент проверки, а не при показе
страницы: она про то, что было известно тогда. Считается по фамилии — см.
:meth:`~app.db.repository.DebtorRepository.count_by_surname`, где написано,
почему именно по ней, а не по ФИО целиком.

Хранение — по правилу ``debtors``: маска всегда, сам документ только при
поднятом ``STORE_SENSITIVE_IDENTIFIERS``. Флаг читается здесь и один раз;
модель про него не знает.
"""

from __future__ import annotations

from datetime import date

from app.config import Settings
from app.db.models import PhoneLookup
from app.db.repository import DebtorRepository, PhoneLookupRepository
from app.db.session import Database
from app.domain.identity import PersonName
from app.logging_setup import get_logger
from app.utils.masking import mask_passport, mask_phone, mask_snils

logger = get_logger(__name__)

__all__ = ["PhoneLookupService"]


class PhoneLookupService:
    def __init__(self, settings: Settings, database: Database) -> None:
        self._settings = settings
        self._database = database

    async def record(
        self,
        *,
        telegram_user_id: int,
        phone: str | None,
        name: PersonName,
        birth_date: date | None = None,
        inn: str | None = None,
        passport: str | None = None,
        snils: str | None = None,
    ) -> PhoneLookup:
        """Записать одну находку и посчитать, знаем ли мы такую фамилию."""
        keep = self._settings.store_sensitive_identifiers
        async with self._database.session() as session:
            matches = await DebtorRepository(session).count_by_surname(name.last_name)
            lookup = await PhoneLookupRepository(session).record(
                PhoneLookup(
                    telegram_user_id=telegram_user_id,
                    phone_masked=mask_phone(phone),
                    last_name=name.last_name,
                    first_name=name.first_name,
                    middle_name=name.middle_name,
                    birth_date=birth_date,
                    inn=inn,
                    passport=passport if keep else None,
                    passport_masked=mask_passport(passport),
                    snils=snils if keep else None,
                    snils_masked=mask_snils(snils),
                    base_matches=matches,
                )
            )
            # Отсоединять от сессии здесь нечего: вызывающему нужны поля, а не
            # ленивые связи, а связей у таблицы нет ни одной.
            session.expunge(lookup)
        logger.info(
            "phone_lookup.recorded",
            user_id=telegram_user_id,
            base_matches=matches,
            has_passport=bool(passport),
            has_snils=bool(snils),
        )
        return lookup

    async def recent(self, *, limit: int) -> list[PhoneLookup]:
        async with self._database.session() as session:
            return await PhoneLookupRepository(session).recent(limit=limit)

    async def count(self) -> int:
        async with self._database.session() as session:
            return await PhoneLookupRepository(session).count()
