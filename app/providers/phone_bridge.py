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
    normalize_snils,
    parse_fio,
)
from app.domain.models import ProviderResult
from app.logging_setup import get_logger
from app.providers.base import BaseProvider
from app.providers.http import RetryPolicy
from app.providers.mapping import RecordDict
from app.providers.vendor_http import VendorConfig, VendorJsonClient
from app.utils.dates import parse_date
from app.utils.masking import mask_phone

logger = get_logger(__name__)

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
    #: СНИЛС. В отличие от ИНН и паспорта он ничего не экономит: по нему не
    #: ищет ни один источник и ни один мост его не спрашивает. Он здесь потому,
    #: что нужен владельцу в заявлении, а приходит тем же ответом — не взять
    #: его значит заставить искать документ отдельно и вручную.
    snils: str | None = None
    #: Дата выдачи паспорта. Тоже не ключ поиска: ФНС ищет ИНН по серии и
    #: номеру и даты не спрашивает (см. ``identity_bridge.missing_input_for``).
    #: Нужна там же, где СНИЛС, — в заявлении, где паспорт указывают полностью.
    #: ``Any``, а не ``date``, по той же причине, что и ``birth_date``:
    #: ``ProviderResult`` не знает про домен.
    passport_issued: Any = None
    #: Адрес. Единственное поле здесь, которое открывает ещё один источник:
    #: ЕГРН ищет объект по адресу, и без адреса он молчит. Требование к адресу
    #: жёсткое — см. :func:`_pick_address`.
    address: str | None = None


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

    Поставщик отвечает не записью о человеке, а свалкой находок: сорок с лишним
    строк из разных утечек, где половина про доставку еды, а личность разложена
    по нескольким блокам. Форма при этом устойчива — владелица сформулировала
    это дословно: «названия полей и где в каком блоке, для каждого номера это
    всё идентично». На живом ответе так и есть, и разбор построен на этом.

    ТРИ ШАГА, И СРЕДНИЙ — САМЫЙ ВАЖНЫЙ.

    1. **Якорь.** Ищется блок, где признаков личности больше всего: ФИО, дата
       рождения, паспорт, СНИЛС, ИНН в одной записи. На живом ответе это
       единственный блок с четырьмя из пяти, и он же несёт кириллическое ФИО.
       Раньше якорем была ПЕРВАЯ строка с читаемым именем — на том же ответе ею
       оказывалась строка из иностранной утечки, где кроме латинского имени нет
       ничего.

    2. **Родня.** К якорю добираются только те блоки, которые с ним связаны
       общим идентификатором: тем же паспортом, тем же СНИЛСом или тем же ФИО.
       Это единственное место, где разбор мог соврать дорого, и вот чем.
       Находки в ответе объединяет ОДИН ТОЛЬКО НОМЕР ТЕЛЕФОНА, а номером
       пользуются и родственники, и прежние владельцы номера: на живом ответе
       рядом с владелицей лежат ещё три человека с другими ФИО. Разбор, который
       добирал недостающее «по всему ответу», однажды взял бы дату рождения
       одного человека к паспорту другого — и собрал бы личность, которой не
       существует. Проверить это по отчёту нельзя: он выглядел бы обычно.

       Связь именно по идентификатору, а не по соседству: на живом ответе ИНН
       лежит отдельным блоком без имени, но с тем же паспортом, что у якоря, —
       и это проверяемое «тот же человек», а не догадка.

    3. **Годность.** Внутри родни берётся первое ПРИГОДНОЕ значение, и каждая
       проверка снята с живого ответа:

       * ИНН — только двенадцать знаков. Десять принадлежат ЮРЛИЦУ: на живом
         ответе такой пришёл из строки почтовой доставки, где рядом стоит
         отправитель-ООО. За ним ушли бы три платных запроса про чужую компанию
         (:func:`_individual_inn`);
       * паспорт — только десять цифр. В том же ответе лежат документы на семь
         и девять цифр (иностранный и загран). Паспорт едет дальше в ФНС за
         ИНН, и негодный — это оплаченный пустой ответ;
       * СНИЛС — с контрольной суммой (:func:`_read_snils`).

    ГЛАВНОЕ ПРАВИЛО ОСТАЁТСЯ. Даже собранная так личность — гипотеза, а не
    запись реестра. Поэтому имя отсюда остаётся ключом поиска, записей мост не
    приносит, а на странице проверок под таблицей написано, что документы не
    подтверждены и перед подачей их надо сверить.
    """
    name, anchor = _pick_anchor(rows)
    if name is None:
        return PhoneNameResult(
            provider=provider.name,
            status=ProviderStatus.NO_RESULTS,
            records=(),
            name=None,
            note=f"По номеру {mask_phone(phone)} имя не определено",
        )

    kin = _kin(rows, anchor)
    passport = _pick(kin, _read_passport, "passport", "passport_number")
    result = PhoneNameResult(
        provider=provider.name,
        status=ProviderStatus.SUCCESS,
        records=(),
        name=name,
        birth_date=_pick(kin, _read_day, "birth_date", "dob"),
        inn=_pick(kin, _individual_inn, "inn", "innfiz"),
        passport=passport,
        snils=_pick(kin, _read_snils, "snils"),
        passport_issued=_issue_date(kin, passport),
        address=_pick_address(kin),
        note=f"ФИО определено по номеру {mask_phone(phone)}",
    )
    # ЧТО РАЗОБРАЛОСЬ, А ЧТО НЕТ — списком имён полей, без значений.
    #
    # Строка появилась после первого же вопроса, на который нечем было
    # ответить: «адрес в итоге забирает?». По одному отчёту это не различить —
    # раздел ЕГРН пишет «недостаточно данных» и когда адреса не было в ответе
    # поставщика, и когда он там был, а разбор его не нашёл. Первое — не наша
    # беда, второе — наша, и лечатся они противоположным.
    #
    # Значения не печатаются: имён полей хватает, чтобы отличить одно от
    # другого, а лог живёт дольше и расходится шире, чем отчёт.
    logger.info(
        "phone_bridge.parsed",
        rows=len(rows),
        kin=len(kin),
        found=sorted(
            field
            for field in ("birth_date", "inn", "passport", "snils", "passport_issued", "address")
            if getattr(result, field) is not None
        ),
    )
    return result


#: Кириллическое слово. Требование не про язык, а про пригодность ключа: искать
#: в русской выгрузке транслитерацией — то же самое, что не искать.
_CYRILLIC = re.compile(r"[А-Яа-яЁё]")

#: Признаки личности. По их числу в одной записи выбирается якорь: блок, где
#: они стоят вместе, — это человек, а не обрывок чужой утечки.
_IDENTITY_KEYS = ("fio", "full_name", "name", "birth_date", "dob", "passport", "snils", "inn")


def _pick_anchor(rows: list[RecordDict]) -> tuple[PersonName | None, RecordDict | None]:
    """Блок, вокруг которого собирается личность, и разобранное из него имя.

    Выбирается по числу признаков личности в одной записи, а не по порядку в
    ответе. Владелица описала это правило дословно: «названия полей и где в
    каком блоке, для каждого номера это всё идентично» — форма ответа
    устойчива, и личность в ней лежит одним блоком, где ФИО, дата рождения,
    паспорт и СНИЛС стоят вместе.

    Раньше якорем была ПЕРВАЯ запись с читаемым именем. На живом ответе ею
    оказывалась запись из иностранной утечки, где кроме латинского имени нет
    ничего, — а настоящий блок с четырьмя признаками лежал шестым.

    Кириллица предпочтительнее латиницы, и это не про язык: искать в русской
    выгрузке транслитерацией всё равно что не искать. Но обязательной её
    сделать нельзя — если поставщик отдал только транслитерацию, честное «в
    выгрузке не найден» лучше молчания.

    Разбор имени строгий: то, что не читается как ФИО, не собирается по кускам,
    потому что полусобранное поднимет чужую строку.
    """
    best: tuple[tuple[int, int, int], PersonName, RecordDict] | None = None
    for order, row in enumerate(rows):
        raw = _first(row, "fio", "full_name", "name")
        if not raw:
            continue
        try:
            parsed = parse_fio(str(raw))
        except NameParseError:
            continue
        # Ключ сравнения: сначала кириллица, потом богатство блока, потом
        # порядок в ответе. Минус у порядка — чтобы при равенстве побеждал
        # первый, а не последний.
        rank = (
            1 if _CYRILLIC.search(str(raw)) else 0,
            sum(1 for key in _IDENTITY_KEYS if row.get(key)),
            -order,
        )
        if best is None or rank > best[0]:
            best = (rank, parsed, row)
    return (best[1], best[2]) if best is not None else (None, None)


def _marks(row: RecordDict) -> dict[str, object]:
    """Годные идентификаторы одной записи. Негодные не попадают сюда вовсе.

    Проверка обязательна и не формальна: в живом ответе поле ``inn`` несёт
    одиннадцать цифр (это СНИЛС, а не ИНН), а ``passport`` — то семь цифр, то
    девять. Негодное значение не должно ни приниматься, ни служить поводом
    отвергнуть чужой блок: мусор не идентифицирует никого.

    Список собирается внутри функции, а не рядом с ней: читатели объявлены
    ниже по файлу, и модульная константа падала бы при импорте.
    """
    kinds: tuple[tuple[str, Callable[[object], object | None], tuple[str, ...]], ...] = (
        ("passport", _read_passport, ("passport", "passport_number")),
        ("snils", _read_snils, ("snils",)),
        ("birth_date", _read_day, ("birth_date", "dob")),
        ("fio", _read_fio_mark, ("fio", "full_name", "name")),
    )
    found: dict[str, object] = {}
    for kind, read, keys in kinds:
        raw = _first(row, *keys)
        if raw is not None and (value := read(raw)) is not None:
            found[kind] = value
    return found


#: Адрес годится для ЕГРН, только если доходит до помещения. Проверено живьём:
#: по адресу до дома Росреестр отвечает ошибкой, и вызов всё равно оплачен.
#: Список полей — все, под которыми поставщик присылает адрес.
_ADDRESS_KEYS = (
    "address",
    "address_reg",
    "permanent_registration_address",
    "actual_residence_address",
    "address_fact",
    "residence",
)
_PREMISES = re.compile(r"(?:кв|квартира|помещ\w*|пом\.?|оф(?:ис)?)\.?\s*№?\s*\d", re.IGNORECASE)


def _pick_address(kin: list[RecordDict]) -> str | None:
    """Адрес нашего человека — с квартирой, если он вообще есть.

    Правило владелицы было «берём первый адрес», и на трёх живых ответах
    первый действительно оказался верным по улице. Но для ЕГРН этого мало:
    Росреестр по адресу до дома отвечает ошибкой, а вызов всё равно оплачен —
    и ни у одного из трёх номеров первый адрес до квартиры не доходил.

    Поэтому сначала ищется адрес с квартирой, и только если такого нет —
    первый попавшийся. Второй годится показать в карточке, но не для ЕГРН;
    отсеет его сам провайдер, бесплатно (см. ``property._query_for``).

    Ищется только среди РОДНИ — записей, не противоречащих опорной. Адрес
    чужого человека из той же выдачи отправил бы платный запрос в Росреестр
    про чужую квартиру.
    """
    fallback: str | None = None
    for row in kin:
        raw = _first(row, *_ADDRESS_KEYS)
        if not raw:
            continue
        text = " ".join(str(raw).split())
        if len(text) < 10:
            continue
        if _PREMISES.search(text):
            return text
        if fallback is None:
            fallback = text
    return fallback


def _read_fio_mark(raw: object) -> str | None:
    """ФИО, приведённое к сравнимому виду: регистр и пробелы не различают людей."""
    text = " ".join(str(raw).split()).lower()
    return text or None


def _kin(rows: list[RecordDict], anchor: RecordDict | None) -> list[RecordDict]:
    """Якорь и блоки, которые ему НЕ ПРОТИВОРЕЧАТ. Порядок ответа.

    Противоречие — это когда у якоря и у блока есть годный идентификатор одного
    вида и они РАЗНЫЕ: другое ФИО, другой паспорт, другой СНИЛС, другая дата
    рождения. Такой блок про другого человека, и брать из него недостающее
    нельзя.

    Это главная защита разбора, и вот от чего. Находки в ответе объединяет ОДИН
    ТОЛЬКО НОМЕР ТЕЛЕФОНА, а номером пользуются и родственники, и прежние
    владельцы номера: в живом ответе рядом с владелицей лежат ещё три человека
    с другими ФИО. Разбор, добиравший недостающее «по всему ответу», однажды
    взял бы дату рождения одного человека к паспорту другого — и собрал бы
    личность, которой не существует. По отчёту это не проверить: он выглядел бы
    совершенно обычно.

    Правило именно «не противоречит», а не «совпадает хоть чем-то», и разница
    существенна. Блок, где лежит один голый ИНН без имени, не совпадает с
    якорем ни по чему — но и не спорит с ним, и выбросить его значило бы
    потерять поле ради подозрения. Блок с другой фамилией спорит прямо, и его
    довод сильнее.

    Негодные значения в счёт не идут ни с одной стороны: СНИЛС с несошедшейся
    контрольной суммой — не СНИЛС, и отвергать по нему чужой блок не за что.
    """
    if anchor is None:
        return list(rows)
    mine = _marks(anchor)
    kin = [anchor]
    for row in rows:
        if row is anchor:
            continue
        theirs = _marks(row)
        if any(kind in mine and mine[kind] != value for kind, value in theirs.items()):
            continue
        kin.append(row)
    return kin


def _pick[T](
    kin: list[RecordDict],
    read: Callable[[object], T | None],
    *keys: str,
) -> T | None:
    """Первое значение, которое ``read`` признал годным, среди блоков родни.

    Порядок — тот, в котором записи пришли, начиная с якоря. Родня уже
    отобрана: здесь остаётся выбрать годное, а не решать, чьё оно.
    """
    for row in kin:
        raw = _first(row, *keys)
        if raw is None:
            continue
        value = read(raw)
        if value is not None:
            return value
    return None


def _issue_date(kin: list[RecordDict], passport: str | None) -> date | None:
    """Дата выдачи — только у ТОГО ЖЕ паспорта, который мы взяли.

    Отдельного поля под неё поставщик не отдаёт: на живом ответе она лежит
    внутри ``passport_info`` — свободным текстом вида «выдан … 15.01.2015», и
    только в блоках одной медицинской утечки. В том же ответе рядом лежат ещё
    два документа с другими номерами (семь и девять цифр), и у них своя дата.

    Поэтому дата берётся не «первая найденная», а из блока, где номер паспорта
    совпадает с выбранным. Приписать дату выдачи одного документа к номеру
    другого — это ошибка, которую в заявлении заметит только суд.
    """
    if not passport:
        return None
    for row in kin:
        if _read_passport(_first(row, "passport", "passport_number") or "") != passport:
            continue
        clean = _first(row, "passport_date", "passport_issued", "issue_date")
        if clean is not None and (parsed := _read_day(clean)) is not None:
            return parsed
        text = row.get("passport_info")
        found = _DATE_IN_TEXT.search(str(text)) if text else None
        if found is not None and (parsed := _read_day(found.group(0))) is not None:
            return parsed
    return None


#: Дата внутри свободного текста. Два формата, потому что оба встречаются в
#: одном и том же ответе.
_DATE_IN_TEXT = re.compile(r"\b\d{2}[.\-/]\d{2}[.\-/]\d{4}\b|\b\d{4}-\d{2}-\d{2}\b")


def _first(row: RecordDict, *keys: str) -> Any:
    """Первое непустое из синонимов поля в одной записи ответа.

    Синонимы нужны потому, что поставщик не один: у кого ``fio``, у кого
    ``full_name``. Пустая строка не считается значением — иначе она заслонила
    бы заполненный синоним рядом.
    """
    for key in keys:
        value = row.get(key)
        if value:
            return value
    return None


def _read_day(raw: object) -> date | None:
    """Дата из строки поставщика — в любом из форматов, которые он шлёт.

    Одна функция на дату рождения и на дату выдачи паспорта: формат у них один
    и тот же, а два разбора разошлись бы на первой правке.
    """
    return parse_date(str(raw))


def _read_passport(raw: object) -> str | None:
    """Паспорт РФ из строки поставщика.

    Источник подписывает вид документа словами — «Паспорт гражданина РФ 4510
    123456», «Загранпаспорт», «Паспорт иностранного гражданина». Нормализация
    оставляет цифры, и негодные отсеиваются длиной: у загранпаспорта их девять,
    у иностранного семь, и мост ФНС на них ответит пустотой за наши деньги.
    """
    return normalize_passport(str(raw))


def _read_snils(raw: object) -> str | None:
    """СНИЛС из строки поставщика — с проверкой контрольной суммы.

    Проверка здесь не педантизм: в ответе одиннадцатизначные числа лежат в
    нескольких полях, и взять не то — значит вписать в заявление чужой
    идентификатор. Разбор живёт в :func:`normalize_snils`.
    """
    return normalize_snils(str(raw))


def build_phone_bridge(settings: Settings) -> PhoneNameProvider | None:
    """Мост, если он настроен, иначе ``None``.

    ``None``, а не выключенный провайдер: незаполненный мост не должен занимать
    строку в отчёте у тех, кто вводит ФИО и в переводе номера не нуждается.
    """
    if not settings.phone_bridge_enabled:
        return None
    return PhoneNameProvider(settings)
