"""Самозанятость (НПД) — метод NewDB ``self_employed``.

Один из немногих источников в отчёте, который говорит, **что у должника есть**, а
не чего у него нет. Подтверждённый статус плательщика налога на профессиональный
доход — это легальный доход, на который обращают взыскание, и основание, с
которым идут к приставу.

**Карта полей написана по живому ответу, а не по документации.** Формы ответа
поставщик в спецификации не описал вовсе, поэтому источник сначала был выпущен
без карты и честно отвечал ``NOT_CONFIGURED``. 13.09.2026 сделан один живой
вызов, и карта собрана по нему: ``is_self_employed`` булевым, ``registry_status``
и ``message`` словами, ``source_url`` ссылкой на сервис ФНС.

**Даты постановки на учёт источник не возвращает.** В ответе есть
``request_date`` — это дата ЗАПРОСА, и подставить её как дату учёта значило бы
сочинить факт: «на учёте с сегодня» верно для любого ответа и неверно ни про
кого.
"""

from __future__ import annotations

from app.domain.enums import MissingInput, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import ProviderResult, SelfEmployedRecord
from app.providers.mapping import RecordDict, as_text
from app.providers.newdb import COUNTRY_RU, NewDBMethodProvider, individual_inn

NEWDB_METHOD = "self_employed"

NEEDS_INN = "Для проверки статуса самозанятого нужен ИНН физлица (12 цифр)"


class NewDBSelfEmployedProvider(NewDBMethodProvider):
    """Статус плательщика НПД по ИНН физлица."""

    name = ProviderName.SELF_EMPLOYED
    title = "Самозанятость (НПД)"
    methods = (NEWDB_METHOD,)
    needs_individual_inn = True

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        inn = individual_inn(subject)
        if inn is None:
            return self.insufficient_query(NEEDS_INN, missing=(MissingInput.INN,))

        mapped, raw = await self.mapped_for(NEWDB_METHOD, _params(inn))
        records = [
            record for row in mapped.records if (record := to_self_employed(row)) is not None
        ]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
            raw_response=self.raw_for(raw),
        )


def _params(inn: str) -> dict[str, str]:
    """Параметры запроса: ключ ``inn``, как в контракте этого метода."""
    return {"country": COUNTRY_RU, "inn": inn}


def to_self_employed(record: RecordDict) -> SelfEmployedRecord | None:
    """Строка ответа в запись о статусе. ``None`` — источник ничего не сказал.

    Статус — единственное обязательное поле: запись без него не утверждает
    ничего, а раздел с пустой строкой читался бы как «что-то нашли».
    """
    active = _flag(record.get("is_active"))
    if active is None:
        return None
    return SelfEmployedRecord(
        is_active=active,
        source_url=as_text(record.get("source_url")),
    )


def _flag(value: object) -> bool | None:
    """Булев признак. ``None`` — источник не сказал, а не «нет»."""
    if isinstance(value, bool):
        return value
    text = as_text(value)
    if text is None:
        return None
    lowered = text.strip().lower()
    if lowered in {"true", "1", "да", "действующий", "активен"}:
        return True
    if lowered in {"false", "0", "нет", "не является"}:
        return False
    return None


__all__ = [
    "NEEDS_INN",
    "NEWDB_METHOD",
    "NewDBSelfEmployedProvider",
    "to_self_employed",
]
