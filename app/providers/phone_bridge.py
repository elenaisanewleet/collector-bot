"""ФИО по телефону — мост между тем, что вводит оператор, и тем, чем ищется должник.

Зачем он есть. Заказчик формулирует сценарий одной фразой: «ввёл номер телефона —
увидел должника». А в выгрузке из 1С телефона нет ни одной колонкой: там ИД, две
даты, спецстоянка, подразделение, ФИО, дата рождения, место рождения, паспорт,
адрес, водительское удостоверение, марка и госномер. Искать по номеру в этой
таблице физически не по чему, и никакой внешний реестр по номеру тоже не ищет —
ни ФССП, ни ЕФРСБ, ни ФНС. Без моста единственный ввод, который оператор считает
естественным, не находит никого.

Мост переводит номер в ФИО, и уже этим ФИО ищется строка в нашей же таблице.

ГЛАВНОЕ ПРАВИЛО: ИМЯ ОТСЮДА — КЛЮЧ ПОИСКА, А НЕ ФАКТ ОТЧЁТА

Найденное мостом ФИО не становится утверждением о человеке. Оно нужно ровно
затем, чтобы поднять строку из выгрузки заказчика; всё, что попадёт в отчёт, —
это данные самого взыскателя и ответы официальных реестров. Поэтому мост, как и
мост «паспорт → ИНН»:

*   не приносит записей (``records`` всегда пуст),
*   не входит в ``configured_names`` и не участвует в весах уверенности,
*   не увеличивает покрытие отчёта.

Он делает возможной проверку тех, кто в покрытие уже входит, — и ничего больше.
Строка в блоке ИСТОЧНИКИ у него при этом есть: «спросили и не нашли» и «не
спрашивали» обязаны различаться и здесь.

ПОЧЕМУ ПОСТАВЩИК НЕ ЗАШИТ В КОД

Ровно по той же причине, что и у остальных внешних адаптеров (см.
:mod:`app.providers.vendor_http`): чужая схема, зашитая в код, — это догадка,
одетая как интеграция. Здесь причина даже сильнее. Сервисы, отдающие ФИО по
номеру, различаются не только формой ответа, но и происхождением данных, и
решение, какому из них доверять и на каком основании, принимает владелец
сервиса, а не этот модуль. Код знает только контракт: дай номер — верни имя.

Без ``PHONE_BRIDGE_BASE_URL`` и карты полей мост отвечает ``NOT_CONFIGURED`` и не
делает ни одного обращения.

КОНТРАКТ

Запрос: HTTP на ``{PHONE_BRIDGE_BASE_URL}{PHONE_BRIDGE_PATH}``, номер
подставляется в ``{phone}`` в пути или уходит параметром ``PHONE_BRIDGE_QUERY``.
Номер нормализован до ``+7XXXXXXXXXX``.

Ответ: JSON. Карта полей (``PHONE_BRIDGE_FIELD_MAP``) говорит, где в нём лежат
ФИО и — необязательно — дата рождения. Пример карты:
``config/field_maps/example_phone_bridge.json``.
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
    NameParseError,
    PersonName,
    SearchSubject,
    normalize_inn,
    normalize_passport,
    parse_fio,
)
from app.domain.models import ProviderResult
from app.providers.base import BaseProvider
from app.providers.http import RetryPolicy
from app.providers.mapping import RecordDict
from app.providers.vendor_http import VendorConfig, VendorJsonClient
from app.utils.dates import parse_date
from app.utils.masking import mask_phone

__all__ = ["PhoneNameProvider", "PhoneNameResult", "build_phone_bridge"]


class PhoneNameResult(ProviderResult):
    """``ProviderResult`` моста плюс само имя.

    Как и у моста «паспорт → ИНН», поле живёт только в памяти одного прогона:
    в ``search_results`` оно не пишется и из кэша не восстанавливается. На
    кэш-хите имя берётся из сохранённого субъекта.
    """

    name: PersonName | None = None
    birth_date: Any = None
    #: ИНН и паспорт, если поставщик их отдал. Оба необязательны и оба сильно
    #: экономят: с готовым ИНН мост «паспорт → ИНН» не сработает вовсе (он
    #: проверяет ``is_needed``), а это минус одно платное обращение с каждого
    #: должника и охват всех, а не только тех, у кого паспорт есть в выгрузке.
    #:
    #: Как и имя, живут в памяти одного прогона: в ``search_results`` не
    #: пишутся, на кэш-хите берутся из сохранённого субъекта.
    inn: str | None = None
    passport: str | None = None


class PhoneNameProvider(BaseProvider):
    """Телефон → ФИО через настраиваемый эндпоинт.

    Политика «звать или нет» живёт здесь целиком: сервис поиска спрашивает
    :meth:`is_needed` и больше ничего не решает.
    """

    name = ProviderName.PHONE_BRIDGE
    title = PROVIDER_TITLES[ProviderName.PHONE_BRIDGE]

    def __init__(self, settings: Settings, client: VendorJsonClient | None = None) -> None:
        self._settings = settings
        self._client = client

    @property
    def is_configured(self) -> bool:
        return self._settings.phone_bridge_configured

    def _vendor_client(self) -> VendorJsonClient:
        if self._client is None:
            self._client = VendorJsonClient(
                VendorConfig(
                    base_url=self._settings.phone_bridge_base_url,
                    path=self._settings.phone_bridge_path,
                    auth_style=self._settings.phone_bridge_auth_style,
                    auth_name=self._settings.phone_bridge_auth_name,
                    api_key=self._settings.phone_bridge_api_key,
                    field_map_path=self._settings.phone_bridge_field_map,
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

        Только когда имени нет, а номер есть. Если оператор уже назвал ФИО —
        своё имя сильнее чужого, и переводить номер незачем.
        """
        return subject.name is None and bool(subject.phone)

    def will_query(self, subject: SearchSubject) -> bool:
        """Дойдёт ли дело до обращения. Нужно смете массового прогона."""
        return self.is_configured and self.is_needed(subject)

    def missing_input_for(self, subject: SearchSubject) -> tuple[MissingInput, ...]:
        return () if subject.phone else (MissingInput.PHONE,)

    async def _fetch(self, subject: SearchSubject) -> ProviderResult:
        missing = self.missing_input_for(subject)
        if missing or not subject.phone:
            return self.insufficient_query("Нужен номер телефона", missing=missing)
        records, _raw = await self._vendor_client().fetch_records({"phone": subject.phone})
        return _read_rows(records, phone=subject.phone, provider=self)


def _individual_inn(raw: object) -> str | None:
    """ИНН ФИЗЛИЦА — двенадцать цифр, и только он.

    Десятизначный ИНН принадлежит юрлицу. Три источника, ради которых ИНН и
    добывается, ищут по ``innfiz`` и валидируют его как двенадцать знаков:
    десятизначный будет отклонён, но вызов всё равно оплачен. Молча пропустить
    его — значит купить три гарантированно пустых ответа на каждом должнике.
    """
    if not raw:
        return None
    value = normalize_inn(str(raw))
    return value if value and len(value) == INN_INDIVIDUAL_LENGTH else None


def _read_rows(
    rows: list[RecordDict], *, phone: str, provider: PhoneNameProvider
) -> ProviderResult:
    """Собрать личность из ответа поставщика.

    Поставщики по номеру телефона отвечают не записью о человеке, а списком
    разнородных находок: сорок с лишним строк из разных утечек, где имя лежит в
    одной, дата рождения в другой, паспорт в третьей, а половина строк — про
    доставку еды. Поэтому каждое поле ищется по всему ответу, а не в одной
    строке: раньше брался первый ряд с читаемым именем и остальные поля
    доставались только из него — на живом ответе это давало имя без даты
    рождения, то есть ключ, по которому в выгрузке поднимется однофамилец.

    **Берётся первое ПРИГОДНОЕ значение, а не первое попавшееся.** Разница не
    косметическая, она снята с живого ответа:

    * первое ``full_name`` там оказалось латиницей — транслитерация из
      иностранной утечки. Как ФИО оно разбирается, а в русской выгрузке по нему
      не найдётся никто, и бот сказал бы «должник не найден» про человека,
      который в базе есть. Кириллическое имя лежало пятой строкой;
    * первый ``inn`` был десятизначным, то есть принадлежал юрлицу. Его молчаливый
      пропуск стоит трёх платных запросов, которые вернут чужие дела или пустоту
      (:func:`_individual_inn`);
    * паспорта приходят и как «4510123456», и как «Паспорт гражданина РФ 4510
      123456», и как загранпаспорт с девятью цифрами — последний не паспорт РФ и
      к мосту ФНС не годится.

    Поля, кроме имени, сначала ищутся в той же строке, где нашлось имя: строка,
    где ФИО и дата стоят вместе, — это одна личность, а не две склеенные.
    Остальное добирается по всему ответу, и вот здесь надо понимать цену:
    **собранная личность — гипотеза, а не запись источника.** Находки объединяет
    только номер, а номером пользуются и родственники, и прежние владельцы
    номера. Ровно поэтому имя остаётся ключом поиска и не попадает в отчёт
    фактом — правило, ради которого мост написан так, что записей не приносит.
    """
    name, home = _pick_name(rows)
    if name is None:
        return PhoneNameResult(
            provider=provider.name,
            status=ProviderStatus.NO_RESULTS,
            records=(),
            name=None,
            note=f"По номеру {mask_phone(phone)} имя не определено",
        )

    return PhoneNameResult(
        provider=provider.name,
        status=ProviderStatus.SUCCESS,
        records=(),
        name=name,
        birth_date=_pick(rows, home, _read_birth, "birth_date", "dob"),
        inn=_pick(rows, home, _individual_inn, "inn", "innfiz"),
        passport=_pick(rows, home, _read_passport, "passport", "passport_number"),
        note=f"ФИО определено по номеру {mask_phone(phone)}",
    )


#: Кириллическое слово. Требование не про язык, а про пригодность ключа: искать
#: в русской выгрузке транслитерацией — то же самое, что не искать.
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")


def _pick_name(rows: list[RecordDict]) -> tuple[PersonName | None, RecordDict | None]:
    """Первое имя, которым можно искать в выгрузке, и строка, где оно нашлось.

    Кириллица предпочтительнее, но не обязательна: если поставщик отдал только
    транслитерацию, лучше отдать её и получить честное «в выгрузке не найден»,
    чем промолчать. Разбор строгий — имя, которое не читается как ФИО, не
    собирается по кускам: полусобранное поднимет чужую строку.
    """
    fallback: tuple[PersonName, RecordDict] | None = None
    for row in rows:
        raw = _first(row, "fio", "full_name", "name")
        if not raw:
            continue
        try:
            name = parse_fio(str(raw))
        except NameParseError:
            continue
        if _CYRILLIC.search(str(raw)):
            return name, row
        if fallback is None:
            fallback = (name, row)
    return fallback if fallback is not None else (None, None)


def _pick[T](
    rows: list[RecordDict],
    home: RecordDict | None,
    read: Callable[[object], T | None],
    *keys: str,
) -> T | None:
    """Первое значение, которое ``read`` признал годным.

    Сначала строка, где нашлось имя: поля одной строки — про одного человека.
    Потом весь ответ по порядку — иначе дата рождения, лежащая отдельно от
    имени, потеряется, а без неё в выгрузке поднимется однофамилец.
    """
    ordered = [home, *rows] if home is not None else list(rows)
    for row in ordered:
        raw = _first(row, *keys)
        if raw is None:
            continue
        value = read(raw)
        if value is not None:
            return value
    return None


def _first(row: RecordDict, *keys: str) -> Any:
    """Первое непустое из синонимов поля в одной строке ответа.

    Синонимы нужны потому, что поставщик не один: у кого ``fio``, у кого
    ``full_name``. Пустая строка не считается значением — иначе она заслонила
    бы заполненный синоним рядом.
    """
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return None


def _read_birth(raw: object) -> date | None:
    return parse_date(str(raw))


def _read_passport(raw: object) -> str | None:
    """Паспорт РФ из строки поставщика.

    Источник подписывает вид документа словами — «Паспорт гражданина РФ 4510
    123456», «Загранпаспорт», «Паспорт иностранного гражданина». Нормализация
    оставляет цифры, и негодные отсеиваются длиной: у загранпаспорта их девять,
    у иностранного семь, и мост ФНС на них ответит пустотой за наши деньги.
    """
    return normalize_passport(str(raw))


def build_phone_bridge(settings: Settings) -> PhoneNameProvider | None:
    """Мост, если он настроен, иначе ``None``.

    ``None``, а не выключенный провайдер: незаполненный мост не должен занимать
    строку в отчёте у тех, кто вводит ФИО и в переводе номера не нуждается.
    """
    if not settings.phone_bridge_enabled:
        return None
    return PhoneNameProvider(settings)
