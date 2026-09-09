"""Паспорт по ФИО и дате рождения — вторая половина цепочки к ИНН.

ЗАЧЕМ ОН ЕСТЬ

Три источника из семи — банкротство, статус ИП и арбитраж — ищут **только** по
двенадцатизначному ИНН физлица. ИНН добывается по паспорту (мост
:mod:`app.providers.identity_bridge`), а паспорт есть не у всех: в выгрузке
заказчика он заполнен не везде, телефона в ней нет вовсе ни у одного из 2052
должников. Для такого должника цепочка обрывалась на первом шаге, и три раздела
отчёта молчали навсегда — при том, что ФИО и дата рождения у нас были.

Этот мост закрывает разрыв: ФИО + дата рождения → паспорт → ФНС → ИНН → три
источника.

ГЛАВНОЕ ПРАВИЛО: ДАТА РОЖДЕНИЯ СОВПАДАЕТ ТОЧНО, ИНАЧЕ ЗАПИСЬ НЕ НАША

Поставщик ищет по строке, а не по полям, и на распространённое имя отвечает
десятками разных людей. Снято с живого ответа 09.09.2026:

*   «Иванов Иван Иванович 1985» — **254 записи**;
*   «Иванов Иван Иванович 15.03.1985» — **43 записи**.

Сорок три записи на одного человека — это по-прежнему не один человек, а
однофамильцы с той же датой и просто мусор из разных утечек. Поэтому берутся
только те, где дата рождения разбирается и совпадает с известной нам ТОЧНО, а
фамилия совпадает по нормализованной форме.

Цена ошибки здесь выше, чем у любого другого разбора в проекте, и её надо
понимать буквально. Взяли чужой паспорт — по нему ушёл платный запрос в ФНС,
пришёл чужой ИНН, по чужому ИНН проверились банкротство, ИП и арбитраж, и в
отчёте про вашего должника оказалась чужая жизнь. Ни одна строка отчёта при
этом не выглядит подозрительно: он ровно такой же, как настоящий. Проверить это
может только человек, который знает должника лично, — то есть никто.

Поэтому здесь нет ни одного «похоже»: либо точное совпадение даты, либо запись
не рассматривается вовсе.

ЧТО ОН НЕ ДЕЛАЕТ

Как и оба соседних моста: записей в отчёт не приносит, в ``configured_names`` не
входит, покрытие не увеличивает. Он делает возможной проверку тех, кто в
покрытие уже входит.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date
from typing import Any

from app.config import Settings
from app.domain.enums import PROVIDER_TITLES, MissingInput, ProviderName, ProviderStatus
from app.domain.identity import (
    INN_INDIVIDUAL_LENGTH,
    PersonName,
    SearchSubject,
    normalize_inn,
    normalize_passport,
    normalize_snils,
)
from app.domain.models import ProviderResult
from app.providers.base import BaseProvider
from app.providers.http import RetryPolicy
from app.providers.mapping import RecordDict
from app.providers.vendor_http import VendorConfig, VendorJsonClient
from app.utils.dates import parse_date
from app.utils.hashing import normalize_token

__all__ = ["PassportByNameProvider", "PassportByNameResult", "build_name_bridge"]


class PassportByNameResult(ProviderResult):
    """``ProviderResult`` моста плюс документы, которые он нашёл.

    Живут в памяти одного прогона, как у обоих соседних мостов: в
    ``search_results`` не пишутся, на кэш-хите берутся из сохранённого субъекта.
    """

    passport: str | None = None
    #: Дата выдачи, если поставщик её отдал. Отдельного поля у него нет — она
    #: лежит текстом внутри ``passport_info``.
    passport_issued: Any = None
    snils: str | None = None
    #: ИНН, если он оказался в тех же записях. Тогда мост ФНС не понадобится
    #: вовсе — это минус одно платное обращение.
    inn: str | None = None


class PassportByNameProvider(BaseProvider):
    """ФИО + дата рождения → паспорт через настраиваемый эндпоинт."""

    name = ProviderName.NAME_BRIDGE
    title = PROVIDER_TITLES[ProviderName.NAME_BRIDGE]

    def __init__(self, settings: Settings, client: VendorJsonClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def is_configured(self) -> bool:
        return self._settings.name_bridge_configured

    def _vendor_client(self) -> VendorJsonClient:
        if self._client is None:
            self._client = VendorJsonClient(
                VendorConfig(
                    base_url=self._settings.name_bridge_base_url,
                    path=self._settings.name_bridge_path,
                    auth_style=self._settings.name_bridge_auth_style,
                    auth_name=self._settings.name_bridge_auth_name,
                    api_key=self._settings.name_bridge_api_key,
                    field_map_path=self._settings.name_bridge_field_map,
                ),
                timeout_seconds=self._settings.request_timeout_seconds,
                retry=RetryPolicy(
                    max_retries=self._settings.provider_max_retries,
                    backoff_seconds=self._settings.provider_retry_backoff_seconds,
                ),
                provider_label=self.name.value,
            )
        return self._client

    def is_needed(self, subject: SearchSubject) -> bool:
        """Нужен ли мост этому субъекту.

        Не нужен дважды: когда паспорт уже есть (искать нечего) и когда уже есть
        ИНН физлица (паспорт нужен только ради него, и покупать его незачем).
        """
        if normalize_passport(subject.passport) is not None:
            return False
        inn = normalize_inn(subject.inn)
        return not (inn and len(inn) == INN_INDIVIDUAL_LENGTH)

    def missing_input_for(self, subject: SearchSubject) -> tuple[MissingInput, ...]:
        """Мост требует и ФИО, и дату рождения — по одному имени он не пойдёт.

        Дата обязательна не для формальности: без неё поставщик отвечает
        сотнями однофамильцев, из которых выбрать нужного нельзя ничем.
        """
        missing: list[MissingInput] = []
        if subject.name is None:
            missing.append(MissingInput.NAME)
        if subject.birth_date is None:
            missing.append(MissingInput.BIRTH_DATE)
        return tuple(missing)

    def will_query(self, subject: SearchSubject) -> bool:
        """Дойдёт ли дело до платного вызова. Нужно смете массового прогона."""
        return (
            self.is_configured and self.is_needed(subject) and not self.missing_input_for(subject)
        )

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        missing = self.missing_input_for(subject)
        if missing:
            return self.insufficient_query("Нужны ФИО и дата рождения", missing=missing)
        assert subject.name is not None and subject.birth_date is not None

        query = f"{subject.name.full} {subject.birth_date.strftime('%d.%m.%Y')}"
        records, _raw = await self._vendor_client().fetch_records({"query": query})
        return _read_rows(records, name=subject.name, birth_date=subject.birth_date, provider=self)


def _read_rows(
    rows: list[RecordDict],
    *,
    name: PersonName,
    birth_date: date,
    provider: PassportByNameProvider,
) -> ProviderResult:
    """Документы того человека, чью дату рождения мы знаем.

    Отбор идёт в два шага, и оба обязательны.

    **Дата рождения совпадает точно.** Записи без разбираемой даты и записи с
    другой датой не рассматриваются вовсе. Это единственный признак, по которому
    из сорока трёх ответов на одно имя выделяется один человек.

    **Фамилия совпадает по нормализованной форме.** Дата рождения не уникальна:
    у поставщика в ответе на «Иванов Иван Иванович» лежат и Ивановы, и посторонние
    записи, попавшие в выдачу по другим полям. Сравнение нормализованное —
    регистр и ``ё`` не различают людей, а вот другая фамилия различает.

    Отчество и имя намеренно НЕ проверяются. В утечках они сокращены до буквы,
    пропущены или записаны с ошибкой чаще, чем фамилия, и требовать их значило
    бы отбросить верные записи. Фамилия плюс точная дата — компромисс, который
    держится на том, что дату мы знаем из СВОЕЙ выгрузки, а не из ответа.
    """
    ours = [row for row in rows if _is_the_same_person(row, name=name, birth_date=birth_date)]
    if not ours:
        return PassportByNameResult(
            provider=provider.name,
            status=ProviderStatus.NO_RESULTS,
            records=(),
            note="По ФИО и дате рождения документов не нашлось",
        )

    passport = _pick(ours, _read_passport, "passport", "passport_number")
    return PassportByNameResult(
        provider=provider.name,
        status=ProviderStatus.SUCCESS if passport else ProviderStatus.NO_RESULTS,
        records=(),
        passport=passport,
        passport_issued=_issue_date(ours, passport),
        snils=_pick(ours, _read_snils, "snils"),
        inn=_pick(ours, _individual_inn, "inn", "innfiz"),
        note=(
            "Паспорт найден по ФИО и дате рождения"
            if passport
            else "Записи нашлись, но паспорта среди них нет"
        ),
    )


def _is_the_same_person(row: RecordDict, *, name: PersonName, birth_date: date) -> bool:
    """Про этого ли человека запись. Точная дата плюс совпавшая фамилия."""
    raw_date = _first(row, "birth_date", "dob", "bdate", "birthdate")
    if raw_date is None or parse_date(str(raw_date)) != birth_date:
        return False
    raw_name = _first(row, "fio", "full_name", "name")
    if raw_name is None:
        # Дата совпала, а имени в записи нет: такие блоки поставщик отдаёт
        # обрывками. Принимаем — фамилию они не опровергают, а поля в них
        # бывают те самые.
        return True
    theirs = normalize_token(str(raw_name)).split()
    return bool(theirs) and theirs[0] == normalize_token(name.last_name)


def _pick[T](rows: list[RecordDict], read: Callable[[object], T | None], *keys: str) -> T | None:
    """Первое значение, которое ``read`` признал годным, среди своих записей."""
    for row in rows:
        raw = _first(row, *keys)
        if raw is None:
            continue
        value = read(raw)
        if value is not None:
            return value
    return None


def _first(row: RecordDict, *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return None


#: Дата внутри свободного текста ``passport_info``: отдельного поля под дату
#: выдачи поставщик не отдаёт.
_DATE_IN_TEXT = re.compile(r"\b\d{2}[.\-/]\d{2}[.\-/]\d{4}\b|\b\d{4}-\d{2}-\d{2}\b")


def _issue_date(rows: list[RecordDict], passport: str | None) -> date | None:
    """Дата выдачи — только у ТОГО ЖЕ паспорта, который мы взяли.

    То же правило, что в телефонном мосте: в одной выдаче лежат несколько
    разных документов, и у каждого своя дата. Приписать дату одного к номеру
    другого — ошибка, которую в заявлении заметит только суд.
    """
    if not passport:
        return None
    for row in rows:
        if _read_passport(_first(row, "passport", "passport_number") or "") != passport:
            continue
        clean = _first(row, "passport_date", "passport_issued", "issue_date")
        if clean is not None and (parsed := parse_date(str(clean))) is not None:
            return parsed
        text = row.get("passport_info")
        found = _DATE_IN_TEXT.search(str(text)) if text else None
        if found is not None and (parsed := parse_date(found.group(0))) is not None:
            return parsed
    return None


def _read_passport(raw: object) -> str | None:
    """Паспорт РФ — десять цифр. Живой ответ несёт и семь, и четырнадцать."""
    return normalize_passport(str(raw))


def _read_snils(raw: object) -> str | None:
    return normalize_snils(str(raw))


def _individual_inn(raw: object) -> str | None:
    """ИНН ФИЗЛИЦА — двенадцать цифр, и только он.

    Тот же отбор, что в телефонном мосте, и по той же причине: десятизначный
    принадлежит юрлицу, одиннадцатизначный — это СНИЛС, который поставщик
    кладёт в поле ``inn``. Три источника отвергнут и тот и другой, а вызов
    будет оплачен.
    """
    if not raw:
        return None
    value = normalize_inn(str(raw))
    return value if value and len(value) == INN_INDIVIDUAL_LENGTH else None


def build_name_bridge(settings: Settings) -> PassportByNameProvider | None:
    """Мост, если он настроен, иначе ``None``.

    ``None``, а не выключенный провайдер: ненастроенный мост не должен занимать
    строку в отчёте у тех, чей паспорт и так известен.
    """
    if not settings.name_bridge_enabled:
        return None
    return PassportByNameProvider(settings)
