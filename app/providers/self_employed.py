"""Самозанятость (НПД) — метод NewDB ``self_employed``.

Один из немногих источников в отчёте, который говорит, **что у должника есть**, а
не чего у него нет. Подтверждённый статус плательщика налога на профессиональный
доход — это легальный доход, на который обращают взыскание, и основание, с
которым идут к приставу.

**Карты полей у этого метода пока нет, и это не забывчивость.** Поставщик
документирует запрос (двенадцатизначный ИНН физлица под ключом ``inn``) и не
документирует ответ: формы строк ``data`` в его спецификации нет вовсе. Написать
карту по догадке значило бы нарушить правило проекта — код кодирует только
проверенное — и купить за это худший из возможных исходов: неверная карта роняет
источник в ``unexpected_schema`` на КАЖДОМ ответе, то есть платный вызов уходит, а
отчёт пишет «не проверено».

Поэтому метод отсутствует в ``config/field_maps/example_newdb.json``, и по общему
правилу провайдер отвечает ``NOT_CONFIGURED`` со ссылкой на то, чего не хватает.
Это честное молчание: источник не притворяется, что смотрел.

Чтобы его включить, нужен один живой ответ. Владелец делает одну проверку с
``STORE_RAW_RESPONSES=true``, сохранённое тело показывает форму строк, и карта
дописывается по факту — как это было сделано для ``egrul_ip`` 05.09.2026.
"""

from __future__ import annotations

from app.domain.enums import MissingInput, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import ProviderResult, SelfEmployedRecord
from app.providers.mapping import RecordDict, as_text
from app.providers.newdb import COUNTRY_RU, NewDBMethodProvider, individual_inn
from app.utils.dates import parse_date

NEWDB_METHOD = "self_employed"

NEEDS_INN = "Для проверки статуса самозанятого нужен ИНН физлица (12 цифр)"


class NewDBSelfEmployedProvider(NewDBMethodProvider):
    """Статус плательщика НПД по ИНН физлица."""

    name = ProviderName.SELF_EMPLOYED
    title = "Самозанятость (НПД)"
    methods = (NEWDB_METHOD,)

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

    Имена ключей здесь — те, под которыми их положит карта полей, когда её
    напишут по живому ответу. Пока карты нет, функция не вызывается ни разу: до
    неё доходит только тот, у кого метод описан.
    """
    active = _flag(record.get("is_active"))
    registered = parse_date(as_text(record.get("registered_at")))
    if active is None and registered is None:
        return None
    return SelfEmployedRecord(is_active=active, registered_at=registered)


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
