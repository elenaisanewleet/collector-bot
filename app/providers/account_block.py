"""Блокировки счетов ФНС — метод NewDB ``fns_block_person``.

**Единственный законный ответ на вопрос «счета в банках».** Заказчик назвал счета
прямым текстом в своём сценарии, и до сих пор отчёт отвечал на это чистым
отказом: остатки и обороты — банковская тайна (ст. 26 ФЗ «О банках»), источника
нет и не будет. Отказ остаётся верным: остатков этот метод не показывает.

Но реестр решений о приостановлении операций ФНС публикует открыто, и в решении
стоит **БИК банка**. Это и есть та половина вопроса, которую можно получить
законно, и практически она же самая нужная: заявление приставу подают с указанием
банка, и до сих пор банк искали перебором.

**Новость двусторонняя, и раздел обязан читаться именно так.** Счёт есть — значит
есть куда обращать взыскание. Но ФНС уже наложила на него руку и стоит в очереди
впереди нас. Одна и та же запись улучшает и ухудшает перспективу, и оценка
считает обе стороны отдельными факторами, а не одним усреднённым.

Ищет по ИНН физлица, то есть зависит от цепочки мостов ровно так же, как
банкротство, статус ИП и арбитраж. Без ИНН — ``insufficient_query``, а не пустой
ответ: «блокировок не найдено» у непроверенного человека читалось бы как «счета
чистые».
"""

from __future__ import annotations

from app.domain.enums import MissingInput, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import AccountBlockRecord, ProviderResult
from app.providers.mapping import RecordDict, as_text
from app.providers.newdb import NewDBMethodProvider, individual_inn, inn_params
from app.utils.dates import parse_date

NEWDB_METHOD = "fns_block_person"
MAX_RECORDS = 30

NEEDS_INN = "Для проверки блокировок счетов нужен ИНН физлица (12 цифр)"


class NewDBAccountBlockProvider(NewDBMethodProvider):
    """Решения ФНС о приостановлении операций по счетам физлица."""

    name = ProviderName.ACCOUNT_BLOCK
    title = "Блокировки счетов (ФНС)"
    methods = (NEWDB_METHOD,)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        inn = individual_inn(subject)
        if inn is None:
            return self.insufficient_query(NEEDS_INN, missing=(MissingInput.INN,))

        mapped, raw = await self.mapped_for(NEWDB_METHOD, inn_params(inn))
        records = [
            record
            for row in mapped.records[:MAX_RECORDS]
            if (record := to_account_block(row)) is not None
        ]
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
            raw_response=self.raw_for(raw),
        )


def to_account_block(record: RecordDict) -> AccountBlockRecord | None:
    """Строка ответа в запись о блокировке. ``None`` — читать нечего.

    Запись без БИК и без номера решения бесполезна дважды: банк по ней не
    опознать, а в заявлении приставу на неё не сослаться. Показывать такую
    строку значило бы сообщить «счёт где-то есть» — утверждение, которое нельзя
    ни проверить, ни использовать.
    """
    bic = as_text(record.get("bank_bic"))
    number = as_text(record.get("decision_number"))
    if not bic and not number:
        return None
    return AccountBlockRecord(
        bank_bic=bic,
        decision_number=number,
        decision_date=parse_date(as_text(record.get("decision_date"))),
        started_at=parse_date(as_text(record.get("started_at"))),
        reason_code=as_text(record.get("reason_code")),
        tax_office=as_text(record.get("tax_office")),
    )


__all__ = ["NEEDS_INN", "NEWDB_METHOD", "NewDBAccountBlockProvider", "to_account_block"]
