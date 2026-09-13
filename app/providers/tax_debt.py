"""Налоговая задолженность — метод NewDB ``nalog_debt``.

Не «ещё один долг должника», а ещё один **взыскатель**, и в этом вся разница для
перспективы. Налоговая взыскивает бесспорно: ей не нужно ни решение суда, ни
исполнительный лист — она списывает со счёта и обращается к приставу напрямую.
В очереди она стоит впереди нашего заказчика независимо от того, кто первым
обратился, и деньги, которых хватило бы на один долг, уйдут не ему.

**Ноль — это ответ, а не пустота.** Источник возвращает ``debt.total``, и ноль там
значит «проверено, задолженности нет». Отсутствие суммы значит другое: источник
её не назвал. Первое улучшает картину, второе не говорит ничего, и различает их
:class:`~app.domain.models.TaxDebtRecord` через ``amount is None``.

**Чего здесь нет.** ``debt.items[]`` источник документирует массивом, форму
которого в спецификации не описал. Поэтому берётся счётчик позиций и не берётся
их разбор: показывать структуру, которой мы не читали, — тот же вымысел, что и
карта полей по догадке.

Ищет по ИНН физлица, как блокировки, банкротство, статус ИП и арбитраж. Описание
метода у поставщика говорит «задолженность физического лица», хотя пример в
спецификации даёт десятизначный ИНН; отправляется двенадцатизначный, и если
поставщик его отвергнет — это придёт как ``bad_request`` и будет видно в отчёте
как «не проверено», а не как «долгов нет».
"""

from __future__ import annotations

from decimal import Decimal

from app.domain.enums import MissingInput, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import ProviderResult, TaxDebtRecord
from app.providers.mapping import RecordDict, as_text
from app.providers.newdb import COUNTRY_RU, NewDBMethodProvider, individual_inn
from app.utils.money import parse_amount

NEWDB_METHOD = "nalog_debt"

NEEDS_INN = "Для проверки налоговой задолженности нужен ИНН физлица (12 цифр)"


class NewDBTaxDebtProvider(NewDBMethodProvider):
    """Задолженность физлица перед налоговой по ИНН."""

    name = ProviderName.TAX_DEBT
    title = "Налоговая задолженность"
    methods = (NEWDB_METHOD,)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        inn = individual_inn(subject)
        if inn is None:
            return self.insufficient_query(NEEDS_INN, missing=(MissingInput.INN,))

        mapped, raw = await self.mapped_for(NEWDB_METHOD, _params(inn))
        records = [record for row in mapped.records if (record := to_tax_debt(row)) is not None]
        # Запись с нулевой суммой — это НАХОДКА в смысле «источник ответил», но
        # не находка в смысле «есть что взыскивать». Статусом различать их не
        # нужно: и то и другое «проверено», а разницу читает раздел отчёта.
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
            raw_response=self.raw_for(raw),
        )


def _params(inn: str) -> dict[str, str]:
    """Параметры запроса.

    Ключ ``inn``, а не ``innfiz``: так назван параметр в спецификации именно
    этого метода. Соседние методы называют то же самое иначе
    (``fns_block_person`` ждёт ``innfiz``), и единого правила у поставщика нет —
    поэтому имя берётся из контракта метода, а не из общего помощника.
    """
    return {"country": COUNTRY_RU, "inn": inn}


def to_tax_debt(record: RecordDict) -> TaxDebtRecord | None:
    """Строка ответа в запись о долге. ``None`` — источник не сказал ничего.

    Ноль сохраняется, а отсутствие суммы — нет, и это главное различие в
    функции. «Задолженности нет» — полноценный ответ, за который отчёт вправе
    начислить плюс; «сумму не назвали» — не ответ, и плюс за него был бы
    выдуман.
    """
    amount = parse_amount(as_text(record.get("amount")))
    count = _count(record.get("items_count"))
    if amount is None and count is None:
        return None
    return TaxDebtRecord(amount=amount, items_count=count)


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def total_debt(records: list[TaxDebtRecord]) -> Decimal | None:
    """Сумма названных долгов. ``None`` — ни одной суммы источник не назвал."""
    amounts = [record.amount for record in records if record.amount is not None]
    return sum(amounts, Decimal(0)) if amounts else None


__all__ = ["NEEDS_INN", "NEWDB_METHOD", "NewDBTaxDebtProvider", "to_tax_debt", "total_debt"]
