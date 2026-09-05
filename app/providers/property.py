"""ЕГРН — объект недвижимости по адресу или кадастровому номеру.

**Что этот источник закрывает.** Есть ли по известному нам адресу
зарегистрированный объект, какой у него кадастровый номер, сколько он стоит по
кадастру, обременён ли он и на сколько долей поделён. Кадастровый номер — то,
что физически вписывается в ходатайство приставу об обращении взыскания; без
него ходатайство писать не о чем.

**Чего он не закрывает и закрыть не может.** Кому объект принадлежит. В живом
ответе — четыре записи о правах («Общая долевая собственность», доли 1/5, 2/5,
1/5, 1/5) и ни одного ФИО. Это не скупость вендора: сведения о правах
конкретного лица ЕГРН отдаёт самому лицу, суду и приставу. Поэтому раздела
«Недвижимость должника» в отчёте нет и не будет, а :attr:`PropertyRecord.
owner_confirmed` этот провайдер никогда не выставляет в ``True``.

**По ФИО он не ищет вовсе.** Вход — адрес из нашей же карточки (адрес
регистрации или адрес по договору, не адрес собственности) либо кадастровый
номер. Адрес до дома, без квартиры, живьём отвечает ``500`` с пустой ``data`` и
не доходит до ``complete``: вызов оплачен, результата нет. Такой адрес поэтому
не отправляется — это «не хватило данных», а не «объект не найден».

Разбор здесь в коде, а не в ``NEWDB_FIELD_MAP``: живой ответ прочитан, а права
и обременения в нём — массивы объектов, из которых плоская карта достаёт только
скаляры. Гейтом служит настройка ``ROSREESTR_ENABLED``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.domain.enums import ProviderName, ProviderStatus, SearchType
from app.domain.identity import SearchSubject
from app.domain.models import PropertyRecord, ProviderResult
from app.providers.base import NO_CONTEXT, FetchContext
from app.providers.mapping import as_text, dig
from app.providers.newdb import COUNTRY_RU, NewDBMethodProvider
from app.utils.dates import parse_date, utcnow
from app.utils.money import parse_amount

NEWDB_METHOD = "rosreestr"
MAX_RECORDS = 20

# 64:47:040605:229 — округ:район:квартал:объект.
CADASTRAL_NUMBER = re.compile(r"^\d{2}:\d{2}:\d{6,7}:\d+$")
# Адрес годится, только если он доходит до помещения. Проверено живьём: адрес до
# дома возвращает 500 и пустую data, и вызов всё равно оплачен.
_PREMISES_MARKERS = ("кв", "квартира", "помещ", "пом.", "оф")
_PREMISES_WITH_NUMBER = re.compile(
    r"(?:кв|квартира|помещ\w*|пом\.?|оф(?:ис)?)\.?\s*№?\s*\d", re.IGNORECASE
)

HOUSE_LEVEL_REFUSAL = (
    "Для запроса в Росреестр нужен адрес с квартирой или кадастровый номер: "
    "по адресу до дома источник отвечает ошибкой"
)
NO_ADDRESS_REFUSAL = "Для запроса в Росреестр нужен адрес или кадастровый номер"
BATCH_DISABLED = (
    "Проверка объекта по адресу в массовой проверке выключена настройкой ROSREESTR_IN_BATCH"
)
DISABLED = "Проверка объекта по адресу выключена настройкой ROSREESTR_ENABLED"

# Поиск по адресу оператор выбирает руками — за него платить он согласился.
_OPERATOR_CHOSEN = frozenset({SearchType.ADDRESS.value})


class NewDBPropertyProvider(NewDBMethodProvider):
    """Объект по адресу через метод NewDB ``rosreestr``."""

    name = ProviderName.PROPERTY
    title = "Объект по адресу (ЕГРН)"
    methods = (NEWDB_METHOD,)

    @property
    def is_configured(self) -> bool:
        return self._settings.rosreestr_configured

    async def _dispatch(self, subject: SearchSubject, context: FetchContext) -> ProviderResult:
        if not self._allowed_here(subject, context):
            return self.not_configured(BATCH_DISABLED)
        return await self._fetch(subject)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        query = _query_for(subject)
        if query is None:
            return self.insufficient_query(
                HOUSE_LEVEL_REFUSAL if subject.address else NO_ADDRESS_REFUSAL
            )

        rows, raw = await self.raw_rows_for(NEWDB_METHOD, query)
        records = [
            record for row in rows[:MAX_RECORDS] if (record := _to_property(row)) is not None
        ]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
            raw_response=self.raw_for(raw),
        )

    def planned_calls(self, subject: SearchSubject, context: FetchContext = NO_CONTEXT) -> int:
        if not self.is_configured or not self._allowed_here(subject, context):
            return 0
        return 1 if _query_for(subject) is not None else 0

    def _allowed_here(self, subject: SearchSubject, context: FetchContext) -> bool:
        """Поиск по адресу оператор выбрал сам; массовый прогон — под настройкой.

        Прогон на восемьсот должников с адресом в карточке — это восемьсот
        платных обращений к ЕГРН, и включаться они должны осознанно.
        """
        if subject.search_type in _OPERATOR_CHOSEN:
            return True
        return self._settings.rosreestr_in_batch or not context.batch


def _query_for(subject: SearchSubject) -> dict[str, Any] | None:
    """Параметры запроса, если субъект вообще годится для ЕГРН."""
    address = as_text(subject.address)
    if address is None:
        return None
    if CADASTRAL_NUMBER.match(address):
        return {"country": COUNTRY_RU, "cadastral_number": address}
    if _has_premises(address):
        return {"country": COUNTRY_RU, "address": address}
    return None


def _has_premises(address: str) -> bool:
    lowered = address.lower()
    if not any(marker in lowered for marker in _PREMISES_MARKERS):
        return False
    return bool(_PREMISES_WITH_NUMBER.search(lowered))


def _to_property(row: Any) -> PropertyRecord | None:
    """Одна строка ``data`` — один объект ЕГРН.

    Пути проверены на живом ответе. Строка без кадастрового номера не является
    объектом, который можно показать или проверить.
    """
    if not isinstance(row, Mapping):
        return None
    cadastral_number = as_text(row.get("cadNumber"))
    if cadastral_number is None:
        return None
    rights = _objects(row.get("rights"))
    return PropertyRecord(
        cadastral_number=cadastral_number,
        address=as_text(dig(row, "address.readableAddress")),
        property_type=_property_type(row),
        area=as_text(row.get("area")),
        cadastral_cost=_cost(row.get("cadCost")),
        cost_date=parse_date(as_text(row.get("cadCostDeterminationDate"))),
        registered_at=parse_date(as_text(row.get("regDate"))),
        cancelled_at=parse_date(as_text(row.get("cancelDate"))),
        rights_count=len(rights),
        shares=tuple(text for item in rights if (text := as_text(item.get("part")))),
        right_types=tuple(
            dict.fromkeys(text for item in rights if (text := as_text(item.get("rightTypeDesc"))))
        ),
        encumbrances=_encumbrances(row.get("encumbrances")),
        # Пустой массив в ответе — это проверенное отсутствие обременений;
        # отсутствие массива — непроверенное. Флаг различает их.
        encumbrances_checked=isinstance(row.get("encumbrances"), list),
        # Никогда не True: правообладателя источник не называет.
        owner_confirmed=False,
        fetched_at=utcnow(),
    )


def _property_type(row: Mapping[str, Any]) -> str | None:
    parts = [as_text(row.get("objType_text")), as_text(row.get("purpose_text"))]
    label = ", ".join(part for part in parts if part)
    return label or None


def _cost(raw: Any) -> Decimal | None:
    """``cadCost`` приходит строкой."""
    return parse_amount(as_text(raw))


def _objects(node: Any) -> list[Mapping[str, Any]]:
    if not isinstance(node, Sequence) or isinstance(node, (str, bytes)):
        return []
    return [item for item in node if isinstance(item, Mapping)]


def _encumbrances(node: Any) -> tuple[str, ...]:
    labels: list[str] = []
    for item in _objects(node):
        label = as_text(item.get("encumbranceTypeDesc")) or as_text(item.get("type"))
        number = as_text(item.get("number")) or as_text(item.get("regNumber"))
        holder = as_text(item.get("owner")) or as_text(item.get("rightholder"))
        text = " — ".join(part for part in (label or "обременение", number, holder) if part)
        labels.append(text)
    return tuple(labels)


__all__ = ["NEWDB_METHOD", "NewDBPropertyProvider"]
