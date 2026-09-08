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
    """Собрать имя из ответа поставщика.

    Разбор строгий, как везде в проекте: имя, которое не читается как ФИО, не
    угадывается по кускам. Неверно разобранное имя тихо отравляет всё, что
    ниже, — им будет найден не тот человек в выгрузке, и отчёт уедет про него.
    """
    for row in rows:
        raw = _first(row, "fio", "full_name", "name")
        if not raw:
            continue
        try:
            name = parse_fio(str(raw))
        except NameParseError:
            continue
        birth_raw = _first(row, "birth_date", "dob")
        # ИНН и паспорт проходят ту же нормализацию, что и введённые руками, и
        # молча отбрасываются, если не проходят. Здесь это не придирка к
        # формату: по кривому ИНН уйдут ПЛАТНЫЕ запросы в банкротство, ИП и
        # арбитраж — и вернут чужие дела или пустоту, неотличимую от «чисто».
        inn_raw = _first(row, "inn", "innfiz")
        passport_raw = _first(row, "passport", "passport_number")
        return PhoneNameResult(
            provider=provider.name,
            status=ProviderStatus.SUCCESS,
            records=(),
            name=name,
            birth_date=parse_date(str(birth_raw)) if birth_raw else None,
            inn=_individual_inn(inn_raw),
            passport=normalize_passport(str(passport_raw)) if passport_raw else None,
            note=f"ФИО определено по номеру {mask_phone(phone)}",
        )

    return PhoneNameResult(
        provider=provider.name,
        status=ProviderStatus.NO_RESULTS,
        records=(),
        name=None,
        note=f"По номеру {mask_phone(phone)} имя не определено",
    )


def _first(row: RecordDict, *keys: str) -> Any:
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return None


def build_phone_bridge(settings: Settings) -> PhoneNameProvider | None:
    """Мост, если он настроен, иначе ``None``.

    ``None``, а не выключенный провайдер: незаполненный мост не должен занимать
    строку в отчёте у тех, кто вводит ФИО и в переводе номера не нуждается.
    """
    if not settings.phone_bridge_enabled:
        return None
    return PhoneNameProvider(settings)
