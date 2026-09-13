"""Розыск МВД — метод NewDB ``mvd_wanted``.

Источник отвечает на вопрос, который дороже всех остальных в этом отчёте:
есть ли смысл вообще подавать. Человек в розыске — это не «сложный должник»,
это «повестку вручать некому», и узнать об этом надо ДО пошлины.

**Он работает по каждому должнику выгрузки, и это его второе достоинство.**
Банкротство, статус ИП и арбитраж ищут только по ИНН физлица, которого в
выгрузке заказчика нет ни у кого: до них добирается цепочка мостов, и каждое её
звено — платный вызов и точка отказа. Розыск ищет по ФИО и дате рождения, то
есть по тому, что в выгрузке есть у всех.

**Чего этот источник не говорит.** МВД ищет по строке имени, и полный тёзка
попадает в выдачу наравне с должником. Поэтому источник сам присылает
``birth_date_match`` — совпала ли дата рождения, — и здесь это значение
принимается как есть, а не пересчитывается: сверять нам не с чем, кроме той же
даты, которую мы и отправили. Запись без совпадения по дате в отчёт попадает, но
фактом о должнике не становится и в оценку не идёт.

**Капча.** Живьём источник ходит на публичную страницу МВД, и та иногда просит
капчу. Ответ приходит с ``captcha_error: true``, ``found: false`` и пустой
``data`` — то есть для читающего только строки выглядит как «в розыске не
значится». Это худшая инверсия, какая возможна в этом продукте: «не проверено»
превращается в «чисто» ровно в том разделе, где «чисто» значит «можно подавать».
Признак лежит рядом с ``data`` и читается из :attr:`NewDBResponse.results`.
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import MissingInput, ProviderName, ProviderStatus
from app.domain.identity import SearchSubject
from app.domain.models import ProviderResult, WantedRecord
from app.providers.mapping import RecordDict, as_text, dig
from app.providers.newdb import NewDBMethodProvider
from app.utils.dates import parse_date

NEWDB_METHOD = "mvd_wanted"
MAX_RECORDS = 20

CAPTCHA_REFUSAL = (
    "Реестр розыска не ответил: источник не прошёл проверку «я не робот». "
    "Это не значит, что в розыске никого нет"
)
NEEDS_PERSON = "Для проверки розыска нужны ФИО и дата рождения"
NEEDS_EMAIL = (
    "Проверка розыска не настроена: публичная форма МВД требует адрес почты, "
    "укажите NEWDB_WANTED_EMAIL"
)


class NewDBWantedProvider(NewDBMethodProvider):
    """Розыск МВД по ФИО и дате рождения."""

    name = ProviderName.WANTED
    title = "Розыск МВД"
    methods = (NEWDB_METHOD,)

    @property
    def is_configured(self) -> bool:
        """Карты полей мало: без адреса почты запрос не примут.

        Гейт добавлен по живому отказу: без ``email`` поставщик отвечает
        HTTP 400, и источник падал с ``http_error`` на каждом должнике. «Не
        подключено» честнее вдвойне — оно называет причину и чинится настройкой,
        а ошибка HTTP выглядит как наша поломка и приходит к нам.
        """
        return super().is_configured and bool(self._settings.newdb_wanted_email)

    @property
    def not_configured_reason(self) -> str:
        """Причина называется, только если она действительно в почте.

        Когда ключа NewDB нет или метод не описан в карте полей, причина другая,
        и подсовывать оператору почту значило бы отправить его чинить не то.
        """
        if self._settings.newdb_configured and not self._settings.newdb_wanted_email:
            return NEEDS_EMAIL
        return super().not_configured_reason

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        if not self._settings.newdb_wanted_email:  # pragma: no cover - закрыто is_configured
            return self.not_configured(NEEDS_EMAIL)
        if subject.name is None or subject.birth_date is None:
            return self.insufficient_query(NEEDS_PERSON, missing=_missing_for(subject))

        mapped, answer = await self.mapped_answer_for(
            NEWDB_METHOD, _params_for(subject, self._settings.newdb_wanted_email)
        )
        raw = self.raw_for(answer.raw)

        if captcha_failed(answer.results):
            # ДО разбора строк и до любого суждения о пустоте. Источник не
            # смотрел в реестр, и всё, что можно сказать, — это что он не
            # смотрел.
            return ProviderResult(
                provider=self.name,
                status=ProviderStatus.UNAVAILABLE,
                error_code="captcha",
                error_message=CAPTCHA_REFUSAL,
                raw_response=raw,
            )

        records = [
            record for row in mapped.records[:MAX_RECORDS] if (record := to_wanted(row)) is not None
        ]
        total = reported_total(answer.results)
        # Источник сам говорит, сколько нашёл. Прислал меньше — раздел неполный,
        # и сказать об этом обязаны мы: «нашли одного» вместо «нашли троих,
        # показали одного» здесь стоит дороже всего.
        incomplete = total is not None and total > len(records)
        return ProviderResult(
            provider=self.name,
            status=ProviderStatus.SUCCESS if records else ProviderStatus.NO_RESULTS,
            records=list(records),
            is_partial=incomplete,
            notes=(f"Источник сообщил о {total} записях, разобрано {len(records)}",)
            if incomplete
            else (),
            raw_response=raw,
        )


def _params_for(subject: SearchSubject, email: str) -> dict[str, str]:
    """Параметры ровно те, что объявлены в контракте метода.

    Без ``country``, и это не пропуск: в схеме ``mvd_wanted`` его нет вовсе, а
    обязательными названы ``lastname``, ``firstname``, ``dob`` и ``email``.
    Отчество передаётся, когда оно есть, — пустым его слать нельзя, поставщик
    такие поля отбивает.

    ``email`` источник использует для отправки формы МВД и обратно не
    возвращает.
    """
    assert subject.name is not None and subject.birth_date is not None
    params = {
        "lastname": subject.name.last_name,
        "firstname": subject.name.first_name,
        "dob": subject.birth_date.strftime("%d.%m.%Y"),
        "email": email,
    }
    if subject.name.middle_name:
        params["secondname"] = subject.name.middle_name
    return params


def _missing_for(subject: SearchSubject) -> tuple[MissingInput, ...]:
    missing: list[MissingInput] = []
    if subject.name is None:
        missing.append(MissingInput.NAME)
    if subject.birth_date is None:
        missing.append(MissingInput.BIRTH_DATE)
    return tuple(missing)


def to_wanted(record: RecordDict) -> WantedRecord | None:
    """Строка ответа в запись розыска. ``None`` — читать нечего.

    Пустая запись здесь опаснее, чем в других источниках: раздел «Розыск» с
    безымянной строкой читается как «кто-то нашёлся», а это самый тяжёлый по
    последствиям раздел отчёта.
    """
    full_name = as_text(record.get("full_name"))
    if not full_name:
        return None
    return WantedRecord(
        full_name=full_name,
        birth_date=parse_date(as_text(record.get("birth_date"))),
        birth_date_match=_flag(record.get("birth_date_match")),
        region=as_text(record.get("region")),
        reason=as_text(record.get("reason")),
        details=as_text(record.get("details")),
        source_url=_source_url(record.get("source")),
    )


def _source_url(value: Any) -> str | None:
    """Адрес источника, если он похож на адрес.

    Живой ответ присылает ссылку на страницу МВД; встречается и форма без
    схемы (``//static.mvd.ru/...``). Ссылка, которая не откроется, в отчёте
    хуже её отсутствия.
    """
    text = as_text(value)
    if not text:
        return None
    if text.startswith("//"):
        return f"https:{text}"
    return text if text.startswith(("http://", "https://")) else None


def _flag(value: Any) -> bool | None:
    """Булев признак источника. ``None`` — источник не сказал.

    «Не сказал» и «сказал нет» здесь разные ответы: на первом запись остаётся
    неопознанной, на втором — опознанной как ЧУЖАЯ.
    """
    if isinstance(value, bool):
        return value
    text = as_text(value)
    if text is None:
        return None
    lowered = text.strip().lower()
    if lowered in {"true", "1", "да"}:
        return True
    if lowered in {"false", "0", "нет"}:
        return False
    return None


def captcha_failed(results: list[Any]) -> bool:
    """Не взял ли источник капчу хоть в одном из ответов."""
    return any(_flag(dig(node, "captcha_error")) for node in results)


def reported_total(results: list[Any]) -> int | None:
    """Сколько записей источник СЧИТАЕТ найденными.

    Сравнивается с числом прочитанных: источник, нашедший три записи и
    приславший одну, обязан отметиться в отчёте как неполный, а не как «нашли
    одну».
    """
    total: int | None = None
    for node in results:
        value = dig(node, "total_found")
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            continue
        try:
            number = int(str(value).strip())
        except ValueError:
            continue
        total = number if total is None else total + number
    return total


__all__ = [
    "CAPTCHA_REFUSAL",
    "NEEDS_PERSON",
    "NEWDB_METHOD",
    "NewDBWantedProvider",
    "captcha_failed",
    "reported_total",
    "to_wanted",
]
