"""Накопительная карточка запроса: память между сообщениями.

Жалоба, ради которой это написано, звучала дословно так: «не написала клочкова
елена николаевна а потом 24 11 1994 и мне пишут а это к чему вообще». Разбор
строки был ни при чём — он и тогда понимал и ФИО, и дату. Не было **памяти**:
каждое сообщение разбиралось изолированно, первое уходило в платную проверку,
второе оказывалось новым запросом ни о ком.

Карточка — это состояние сборки, живущее между сообщениями. Любой присланный
кусок дописывается в неё, а не начинает проверку. Платит ровно одна кнопка.

Три решения, которые стоит понимать до чтения кода.

**Карточка не состояние FSM.** Все конкурирующие текстовые сценарии — госномер,
VIN, адрес, договор, паспорт, импорт — привязаны к состояниям, а свободный текст
ловит последний роутер с ``StateFilter(None)``. Стань карточка состоянием, она
забрала бы себе весь текст и сняла бы этот фильтр. Поэтому «какое поле мы ждём»
— колонка в таблице, а не ``State``, и порядок роутеров не меняется вовсе.

**Паспорт и СНИЛС хранятся при поднятом ``STORE_SENSITIVE_IDENTIFIERS``** — то
же правило, что у ``debtors``: маска всегда, документ при флаге. Раньше их не
хранили ни под каким флагом; отменила это владелица («нам надо наоборот
сохранять эти номера»), и довод против устарел вместе с продуктом: бот теперь
документы не принимает, а НАХОДИТ и платит за это. Черновик, теряющий
оплаченное при перезапуске, заставляет платить дважды.

**Телефон в базу не едет** ни под каким флагом — только маска. Его оператор
вводит сам и помнит, а ``phone_hash`` в ``debtors`` считается без соли.

При опущенном флаге после перезапуска карточка честно пишет «сам номер не храню,
пришлите заново»: разница между «не спрашивали» и «было, но не сохранилось»
видна.

**Пустое поле — это «мы не спрашивали», а не «мы не нашли».** Главный инвариант
проекта в применении к карточке. Отсюда же запрет на «заполнено 4 из 7» и
проценты: полей семь, а связок три ({ФИО + дата}, {ИНН}, {паспорт + ФИО +
дата}), и линейная шкала соврала бы про полноту проверки.
"""

from __future__ import annotations

import json
from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Any

from app.bot.identifiers import (
    TOO_MANY_NAME_WORDS,
    Field,
    Fragment,
    FragmentKind,
    ParsedQuery,
    classify_fragment,
    echo,
    looks_like_patronymic,
    looks_like_russian_name,
    parse_query,
    spills_beyond,
)
from app.config import Settings
from app.db.repository import QueryCardRepository
from app.db.session import Database
from app.domain.enums import SearchType
from app.domain.identity import (
    NameParseError,
    PersonName,
    SearchSubject,
    VehicleDescriptor,
    capitalize_name,
    parse_fio,
)
from app.utils.dates import utcnow
from app.utils.formatting import format_phone
from app.utils.hashing import stable_hash
from app.utils.masking import mask_passport, mask_phone, mask_snils

#: Сколько живут паспорт и телефон в памяти процесса. Тот же час, что у
#: ``SubjectStore``, и по той же причине: дольше держать чужие идентификаторы
#: незачем, короче — оператор не успеет нажать «Проверить».
SECRET_TTL = timedelta(hours=1)
SECRET_CAPACITY = 500

#: Порядок строк карточки. Постоянный: сотня должников в день делается
#: мышечной памятью, и переставлять строки под содержимое — значит её ломать.
FIELD_ORDER: tuple[str, ...] = (
    "phone",
    "last_name",
    "first_name",
    "middle_name",
    "birth_date",
    "inn",
    "passport",
    "passport_issued",
    "snils",
    "plate",
    "vin",
    "contract_number",
    "address",
)

#: Поля, чья строка появляется в карточке только когда заполнена или ожидается.
#: Семь пустых строк подряд читаются как шкала полноты, а это ровно то
#: впечатление, которое карточка создавать не должна.
OPTIONAL_ROWS: frozenset[str] = frozenset(
    {"passport", "passport_issued", "snils", "plate", "vin", "contract_number", "address"}
)

FIELD_TITLES: dict[str, str] = {
    "last_name": "Фамилия",
    "first_name": "Имя",
    "middle_name": "Отчество",
    "birth_date": "Дата рождения",
    "phone": "Телефон",
    "inn": "ИНН",
    "passport": "Паспорт",
    "passport_issued": "Паспорт выдан",
    "snils": "СНИЛС",
    "plate": "Госномер",
    "vin": "VIN",
    "contract_number": "Договор",
    "address": "Адрес",
}

#: Три основных шага, ровно в этом порядке и ровно эти три.
#:
#: Порядок задан владелицей дословно — «вообще только телефон и фамилия и имя,
#: и всё, остальное это опция», — и телефон стоит первым по её же указанию:
#: «сначала спрашиваем только номер телефона, номер либо пишется либо не
#: пишется». Это единственный идентификатор, который бот умеет РАЗВЕРНУТЬ: по
#: нему мост поднимает ФИО, а по ФИО — строку выгрузки с датой рождения,
#: договором и машиной. В самой выгрузке 1С телефона нет ни у одного из 2052
#: должников, поэтому первый шаг принимает и любой другой номер — см.
#: :meth:`QueryCardService.apply`.
#:
#: Отчества, даты рождения и ИНН здесь нет намеренно. Они дороже по вводу и
#: реже известны оператору на руках; их место — в меню опций, где рядом
#: написано, что каждое из них открывает.
STEPS: tuple[Field, ...] = (Field.PHONE, Field.LAST_NAME, Field.FIRST_NAME)

#: Куда ложится ответ на вопрос про конкретный слот имени и какие слоты идут
#: за ним, если слов прислали несколько.
NAME_SLOTS_FOR: dict[Field, tuple[str, ...]] = {
    Field.LAST_NAME: ("last_name", "first_name", "middle_name"),
    Field.FIRST_NAME: ("first_name", "middle_name"),
    Field.MIDDLE_NAME: ("middle_name",),
}


@dataclass(slots=True)
class Card:
    """Карточка в памяти обработчика: строка БД плюс то, что в БД не едет.

    ``phone`` и ``passport`` — полные значения из :class:`CardSecrets`. В базе
    им соответствуют только маски, и после перезапуска бота они пусты, а маски
    на месте: это и есть видимая разница между «не спрашивали» и «было, но не
    сохраняю».
    """

    telegram_user_id: int
    chat_id: int
    card_message_id: int | None = None
    last_name: str | None = None
    first_name: str | None = None
    middle_name: str | None = None
    birth_date: date | None = None
    inn: str | None = None
    phone_masked: str | None = None
    passport_masked: str | None = None
    snils_masked: str | None = None
    #: Дата выдачи паспорта. В базу едет как есть, а не маской, и это не
    #: послабление: сама по себе дата не опознаёт никого — опознаёт номер, а он
    #: по-прежнему живёт только в памяти. Ради маски над датой пришлось бы
    #: заводить формат, который ничего не скрывает.
    passport_issued: date | None = None
    plate: str | None = None
    vin: str | None = None
    contract_number: str | None = None
    address: str | None = None
    skipped: frozenset[str] = frozenset()
    awaiting_field: str | None = None
    #: Идём ли по трём основным шагам. Колонка, а не память процесса: оператор
    #: бросает недособранную карточку на полчаса чаще, чем бот перезапускается,
    #: и вернуться он должен на тот же вопрос, а не в начало.
    guided: bool = False
    last_run_hash: str | None = None
    checked_at: datetime | None = None
    #: Последняя проверка не нашла ничего. Нужен, чтобы предложить меню опций
    #: там, где оператор иначе уходит с пустым отчётом ни с чем: у него на
    #: руках может лежать ИНН из договора или госномер машины.
    last_run_empty: bool = False
    #: Когда карточку трогали в последний раз. Нужна одному — отличить «отвлёкся
    #: на минуту» от «это было вчера»: остывшая карточка начинает нового
    #: должника, а не показывает чужие поля как свои.
    updated_at: datetime | None = None
    #: Живут только в памяти процесса.
    phone: str | None = None
    passport: str | None = None
    snils: str | None = None
    #: Десять цифр с девятки, ждущие ответа «паспорт или телефон». Тоже только
    #: в памяти: половину времени это паспорт.
    pending_ten: str | None = None
    #: ФИО, ждущее ответа «новый человек или исправление».
    pending_name: str | None = None

    @property
    def key(self) -> tuple[int, int]:
        return self.telegram_user_id, self.chat_id

    @property
    def name(self) -> PersonName | None:
        """ФИО, если его уже хватает на имя.

        Собирается лениво и строго: ``PersonName`` требует фамилию и имя, а
        карточка обязана существовать при одной фамилии — ради этого она и
        заведена. Ослаблять разбор ФИО здесь нельзя, поэтому вместо разбора —
        прямая сборка из уже разложенных по полям слов.
        """
        if not self.last_name or not self.first_name:
            return None
        return PersonName(
            last_name=self.last_name,
            first_name=self.first_name,
            middle_name=self.middle_name,
        )

    #: Через сколько бездействия недособранная карточка считается остывшей.
    #: Полчаса — граница между «отвлёкся» и «это было вчера»: в первом случае
    #: оператор возвращается к своему человеку и стирать ответы нельзя, во
    #: втором он начинает нового и видеть чужие поля не должен.
    STALE_AFTER = timedelta(minutes=30)

    @property
    def stale(self) -> bool:
        """Карточка — остаток от прошлого раза, а не работа в процессе.

        Два признака, и оба означают одно.

        Бот забыл секрет. Телефон и паспорт живут в памяти процесса, а в базе
        от них остаётся маска; после перезапуска карточка честно пишет «сам
        номер не храню, пришлите заново». Запускать её нечем: значения нет.

        Карточку давно не трогали. Оператор, нажавший «Проверить человека»
        через сутки, начинает нового должника, и чужие «пропустили» на экране
        читаются как поломка бота — что и случилось.
        """
        if self.forgotten("phone") or self.forgotten("passport"):
            return True
        # Только что созданная в памяти карточка отметки не имеет и остыть не
        # может: остывает то, что пролежало в базе.
        return self.updated_at is not None and utcnow() - self.updated_at > self.STALE_AFTER

    @property
    def blank(self) -> bool:
        """Ничего не собрано, ничего не пропущено, ничего не проверено.

        Отличается от «нечего проверять» (:attr:`runnable`) намеренно: карточка
        с одним телефоном не запускаемая, но и не пустая — вопросы по ней уже
        задавали, и начинать сценарий заново было бы стиранием ответа.
        """
        return (
            not any(self.shown(name) for name in FIELD_ORDER)
            and not self.skipped
            and self.checked_at is None
        )

    @property
    def runnable(self) -> bool:
        """Есть ли по чему запускать проверку.

        Телефона здесь нет, и это не противоречит тому, что телефон — первый
        шаг. По телефону работает ПОИСК В 1С, и когда он находит человека,
        карточка получает от него ФИО и дату — вот тогда она и становится
        запускаемой. А телефон, по которому в выгрузке никого нет, во внешние
        реестры не несёт ничего: ни один из них по номеру не ищет, и платный
        прогон дал бы шапку «—» и пять строк «нужно ФИО». Такой случай кончается
        меню опций, а не проверкой ни о ком.

        Паспорта тоже нет: сам по себе он не открывает ничего, мост требует к
        нему ещё ФИО и дату. Договор и адрес есть: по ним поднимается строка из
        1С — главного источника, — и это законный самостоятельный запрос.

        Всё сказанное верно, ПОКА нет моста «телефон → ФИО». С ним номер
        перестаёт быть тупиком: мост переводит его в имя, и дальше запускается
        обычная проверка человека. Поэтому есть второй вход —
        :meth:`runnable_with`, — и решает там не карточка, а тот, кто знает,
        подключён ли мост.
        """
        return bool(
            self.name or self.inn or self.plate or self.vin or self.contract_number or self.address
        )

    def runnable_with(self, *, phone_resolves: bool) -> bool:
        """Есть ли по чему запускать, если номер умеет превращаться в ФИО.

        Отдельным методом, а не флагом на карточке: подключённость моста —
        свойство развёртывания, а не этого должника, и хранить её в строке БД
        значило бы засолить в карточке вчерашнюю настройку.
        """
        return self.runnable or (phone_resolves and bool(self.phone))

    def subject(self, *, allow_phone_only: bool = False) -> SearchSubject | None:
        """Субъект поиска из карточки. ``None`` — спрашивать нечего.

        Живёт на самой карточке, а не в сервисе, потому что потребителей два:
        сервис (перед прогоном) и рендер (чтобы посчитать, что откроется). Две
        сборки разошлись бы, и карточка обещала бы одно, а проверялось бы
        другое.

        ``regions`` остаётся пустым, и это не заглушка: ``fssp._region_codes``
        читает пустой кортеж как «все регионы» одним запросом.

        Голый госномер или VIN — это не человек: сделать его ``person`` значило
        бы отправить в ФССП субъект без ФИО и получить «нужно ФИО» там, где
        вопрос был про машину.
        """
        if not (self.runnable or (allow_phone_only and self.phone)):
            return None
        vehicle = (
            VehicleDescriptor(plate=self.plate, vin=self.vin) if (self.plate or self.vin) else None
        )
        if vehicle is not None and not (self.name or self.inn or self.contract_number):
            search_type = SearchType.VIN if self.vin else SearchType.VEHICLE_PLATE
            return SearchSubject(search_type=search_type.value, vehicle=vehicle)
        return SearchSubject(
            search_type=SearchType.PERSON.value,
            name=self.name,
            birth_date=self.birth_date,
            phone=self.phone,
            inn=self.inn,
            passport=self.passport,
            snils=self.snils,
            passport_issued=self.passport_issued,
            vehicle=vehicle,
            contract_number=self.contract_number,
            address=self.address,
        )

    def value(self, name: str) -> object | None:
        """Значение поля по имени — для рендера и для хэша прогона."""
        return getattr(self, name, None)

    def shown(self, name: str) -> str | None:
        """Что печатать в строке поля.

        ДОКУМЕНТЫ ПЕЧАТАЮТСЯ ЦЕЛИКОМ. Раньше здесь стояла маска, и держалась
        она на обещании, которое бот давал перед вводом паспорта: «номер не
        сохраняю и сообщение удалю». Обещание владелица отменила дословно —
        «надо убрать это, нам надо наоборот сохранять эти номера», — и вместе с
        ним отпало основание для маски. Бот закрыт, принадлежит одному
        человеку, а документы добывает ровно затем, чтобы тот подал с ними в
        суд: заявление подаётся с серией и номером, не с их тенью.

        Маска показывается ровно в одном случае — когда самого документа нет, а
        она осталась. Так бывает после перезапуска, если развёртывание не
        хранит документы (``STORE_SENSITIVE_IDENTIFIERS`` опущен): карточка
        честно говорит «было, но не сохранилось», и это не то же самое, что «не
        спрашивали».

        Телефон подчиняется тому же правилу, что паспорт и СНИЛС. Раньше он был
        исключением — всегда маска, «оператор его и так знает». Но после
        перезапуска карточка показывала маску номера, который оператор ввёл сам,
        и предлагала ввести его заново: маска экономила ровно ничего и мешала
        по-настоящему.
        """
        match name:
            case "phone":
                return format_phone(self.phone) or self.phone_masked
            case "passport":
                return self.passport or self.passport_masked
            case "snils":
                return self.snils or self.snils_masked
            case "birth_date" | "passport_issued":
                value = self.value(name)
                return value.strftime("%d.%m.%Y") if isinstance(value, date) else None
            case _:
                value = self.value(name)
                return str(value) if value else None

    def forgotten(self, name: str) -> bool:
        """Маска есть, самого значения нет: пережило перезапуск, но не полностью."""
        if name == "phone":
            return bool(self.phone_masked) and not self.phone
        if name == "passport":
            return bool(self.passport_masked) and not self.passport
        if name == "snils":
            return bool(self.snils_masked) and not self.snils
        return False

    def to_columns(self) -> dict[str, Any]:
        return {
            "card_message_id": self.card_message_id,
            "last_name": self.last_name,
            "first_name": self.first_name,
            "middle_name": self.middle_name,
            "birth_date": self.birth_date,
            "inn": self.inn,
            "phone": self.phone,
            "phone_masked": self.phone_masked,
            "passport": self.passport,
            "passport_masked": self.passport_masked,
            "snils": self.snils,
            "snils_masked": self.snils_masked,
            "passport_issued": self.passport_issued,
            "plate": self.plate,
            "vin": self.vin,
            "contract_number": self.contract_number,
            "address": self.address,
            "skipped_json": json.dumps(sorted(self.skipped)),
            "awaiting_field": self.awaiting_field,
            "guided": self.guided,
            "last_run_hash": self.last_run_hash,
            "checked_at": self.checked_at,
            "last_run_empty": self.last_run_empty,
        }

    # ------------------------------------------------------------ шаги

    @property
    def step(self) -> Field | None:
        """Какой из трёх основных шагов идёт сейчас. ``None`` — шаги кончились."""
        if not self.guided:
            return None
        current = _field_or_none(self.awaiting_field)
        return current if current in STEPS else None

    @property
    def step_number(self) -> int:
        """Номер текущего шага, считая с единицы. Ноль — шаги кончились."""
        step = self.step
        return STEPS.index(step) + 1 if step is not None else 0

    def next_step(self) -> Field | None:
        """Следующий незаданный шаг — или ``None``, если спрашивать больше нечего.

        Уже заполненные поля пропускаются молча: оператор, приславший строку
        целиком, не должен отвечать на вопрос, на который он только что ответил
        сам. Это и есть «шаги пропускаются, спрашивается только недостающее».
        """
        step = self.step
        start = STEPS.index(step) + 1 if step is not None else 0
        for candidate in STEPS[start:]:
            if not self.has(candidate) and candidate.value not in self.skipped:
                return candidate
        return None

    def has(self, field: Field) -> bool:
        """Заполнено ли поле, которое стоит за кнопкой или шагом."""
        match field:
            case Field.PHONE:
                return bool(self.phone_masked)
            case Field.PASSPORT:
                return bool(self.passport_masked)
            case Field.LAST_NAME:
                return bool(self.last_name)
            case Field.FIRST_NAME:
                return bool(self.first_name)
            case Field.MIDDLE_NAME:
                return bool(self.middle_name)
            case Field.FIO:
                return bool(self.last_name or self.first_name or self.middle_name)
            case Field.AUTO:
                return bool(self.plate or self.vin)
            case Field.CONTRACT:
                return bool(self.contract_number)
            case _:
                return bool(self.value(field.value))


@dataclass(slots=True)
class Applied:
    """Что сделало одно присланное сообщение.

    ``notice`` — строка под списком полей: «записал в фамилию», «на дату не
    похоже», «не понял». Она часть карточки, а не отдельное сообщение: ответ,
    уехавший вверх чата, теряется ровно так же, как терялся прежний ввод.

    ``conflict`` — единственный случай, когда карточка не меняется, хотя всё
    разобрано: прислали полное ФИО поверх ДРУГОЙ фамилии.
    """

    card: Card
    notice: str | None = None
    changed: bool = False
    conflict: PersonName | None = None
    #: Присланное не подошло под названное поле, вопрос остаётся заданным.
    #: Ведомый сценарий по такому вводу вперёд не идёт: шаг, который не
    #: получил ответа, обязан задаться ещё раз, а не молча пропасть.
    rejected: bool = False


@dataclass(slots=True)
class _Secrets:
    phone: str | None = None
    passport: str | None = None
    snils: str | None = None
    pending_ten: str | None = None
    #: ФИО, присланное поверх другой фамилии и ждущее ответа «новый человек или
    #: исправление». Не секрет, но такой же незакрытый вопрос: пережить
    #: перезапуск он не должен, иначе бот встретит оператора чужим вопросом.
    pending_name: str | None = None


class CardSecrets:
    """Паспорт и телефон карточки — в памяти процесса и ненадолго.

    Одно хранилище на два секрета вместо двух механизмов, с тем же сроком и той
    же гарантией, что у :class:`~app.services.subject_store.SubjectStore`: на
    диск отсюда не попадает ничего ни при каком флаге.

    Отдельно про то, почему не хэш. ``phone_hash`` в ``debtors`` считается
    ``stable_hash("phone", phone)`` без соли, а пространство российских
    мобильных — порядка десяти миллиардов: такой хэш перебирается за минуты.
    Тащить эту конструкцию в новую таблицу ради черновика не надо.
    """

    def __init__(self, *, ttl: timedelta = SECRET_TTL, capacity: int = SECRET_CAPACITY) -> None:
        self._ttl = ttl
        self._capacity = capacity
        self._items: OrderedDict[tuple[int, int], tuple[_Secrets, float]] = OrderedDict()

    def get(self, key: tuple[int, int]) -> _Secrets:
        self._evict_expired()
        entry = self._items.get(key)
        return entry[0] if entry else _Secrets()

    def put(self, key: tuple[int, int], secrets: _Secrets) -> None:
        self._evict_expired()
        self._items[key] = (secrets, utcnow().timestamp())
        self._items.move_to_end(key)
        while len(self._items) > self._capacity:
            self._items.popitem(last=False)

    def drop(self, key: tuple[int, int]) -> None:
        self._items.pop(key, None)

    def _evict_expired(self) -> None:
        cutoff = utcnow().timestamp() - self._ttl.total_seconds()
        for key in [key for key, (_, stored) in self._items.items() if stored < cutoff]:
            del self._items[key]

    def __len__(self) -> int:
        return len(self._items)


@dataclass(slots=True)
class _Screen:
    """Последний нарисованный вид карточки. Только в памяти."""

    text: str
    markup: str


class QueryCardService:
    """Чтение, запись и пополнение карточки.

    Держит два хранилища в памяти рядом с базой: секреты (см.
    :class:`CardSecrets`) и последний нарисованный вид. Второе нужно, чтобы не
    звать ``edit_text`` с тем же самым текстом: Telegram отвечает на это ошибкой
    400 «message is not modified», а по нашему обработчику ошибки карточка
    удалялась бы и прыгала вниз чата.
    """

    def __init__(self, database: Database, settings: Settings) -> None:
        self._database = database
        self._settings = settings
        self._secrets = CardSecrets()
        self._screens: dict[tuple[int, int], _Screen] = {}

    # ------------------------------------------------------------ хранение

    async def load(self, telegram_user_id: int, chat_id: int) -> Card:
        """Карточка из базы плюс секреты из памяти. Никогда не ``None``.

        Пустая карточка — законное состояние, а не отсутствие: «оператор ещё
        ничего не прислал» и «оператора нет» с точки зрения экрана одно и то же.
        """
        async with self._database.session() as session:
            row = await QueryCardRepository(session).get(telegram_user_id, chat_id)
        card = Card(telegram_user_id=telegram_user_id, chat_id=chat_id)
        if row is not None:
            card = Card(
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                card_message_id=row.card_message_id,
                last_name=row.last_name,
                first_name=row.first_name,
                middle_name=row.middle_name,
                birth_date=row.birth_date,
                inn=row.inn,
                phone=row.phone,
                phone_masked=row.phone_masked,
                passport=row.passport,
                passport_masked=row.passport_masked,
                snils=row.snils,
                snils_masked=row.snils_masked,
                passport_issued=row.passport_issued,
                plate=row.plate,
                vin=row.vin,
                contract_number=row.contract_number,
                address=row.address,
                skipped=frozenset(json.loads(row.skipped_json or "[]")),
                awaiting_field=row.awaiting_field,
                guided=row.guided,
                last_run_hash=row.last_run_hash,
                checked_at=row.checked_at,
                last_run_empty=row.last_run_empty,
                updated_at=row.updated_at,
            )
        secrets = self._secrets.get(card.key)
        card.phone = secrets.phone
        # Память свежее базы и старше её: при опущенном флаге в базе документа
        # нет вовсе, а в памяти он ещё живой. Пустая память базу не затирает.
        card.passport = secrets.passport or card.passport
        card.snils = secrets.snils or card.snils
        card.pending_ten = secrets.pending_ten
        card.pending_name = secrets.pending_name
        return card

    async def save(self, card: Card) -> None:
        self._secrets.put(
            card.key,
            _Secrets(
                phone=card.phone,
                passport=card.passport,
                snils=card.snils,
                pending_ten=card.pending_ten,
                pending_name=card.pending_name,
            ),
        )
        columns = card.to_columns()
        if not self._settings.store_sensitive_identifiers:
            # Флаг опущен — в базу едут только маски. Решение развёртывания, а
            # не умолчание кода: снаружи это видно по тому, что после
            # перезапуска карточка просит прислать номер заново.
            columns["phone"] = None
            columns["passport"] = None
            columns["snils"] = None
        async with self._database.session() as session:
            await QueryCardRepository(session).save(card.telegram_user_id, card.chat_id, columns)

    async def wipe(self, telegram_user_id: int, chat_id: int) -> Card:
        """Очистить карточку целиком, включая секреты.

        ``card_message_id`` не сохраняется: очистка — это новый должник, и
        карточка под ним должна быть новым сообщением, а не отредактированным
        прежним. Иначе отчёт по предыдущему человеку остался бы ниже карточки
        следующего.
        """
        async with self._database.session() as session:
            await QueryCardRepository(session).delete(telegram_user_id, chat_id)
        self._secrets.drop((telegram_user_id, chat_id))
        self._screens.pop((telegram_user_id, chat_id), None)
        return Card(telegram_user_id=telegram_user_id, chat_id=chat_id)

    # ------------------------------------------------------------ экран

    def last_screen(self, card: Card) -> _Screen | None:
        return self._screens.get(card.key)

    def remember_screen(self, card: Card, text: str, markup: str) -> None:
        self._screens[card.key] = _Screen(text=text, markup=markup)

    def forget_screen(self, card: Card) -> None:
        self._screens.pop(card.key, None)

    # ------------------------------------------------------------ прогон

    def run_hash(self, card: Card) -> str:
        """Отпечаток набора полей. Одинаковый — значит платить не за что.

        Считается по значениям, а не по «сколько полей заполнено»: исправленная
        фамилия — это другой запрос, хотя полей столько же.
        """
        return stable_hash(
            *(str(card.shown(name) or "") for name in FIELD_ORDER),
            card.passport or "",
            card.phone or "",
        )

    # ------------------------------------------------------------ пополнение

    def apply(self, card: Card, raw: str) -> Applied:
        """Дописать присланное в карточку. Единственная точка входа для текста.

        Порядок разбора: если поле названо кнопкой — читаем текст как это поле и
        только как это поле; иначе решает форма записи. Гадание сведено к одному
        случаю — одинокому слову буквами, — и даже оно показывается вслух:
        «„Иванова“ записал в фамилию».
        """
        if not raw.strip():
            # Пустое сообщение не правит ничего. Иначе ``edit_text`` уехал бы с
            # тем же текстом и вернул 400 «message is not modified».
            return Applied(card=card)

        expected = _field_or_none(card.awaiting_field)
        if card.awaiting_field == _AWAITING_TEN:
            return self._resolve_ten_by_text(card, raw)
        if expected is Field.PHONE and card.step is Field.PHONE:
            # Экран первого шага просит ТЕЛЕФОН — так велела владелица, и это
            # её сценарий. А код принимает ЛЮБОЙ номер, и расхождение здесь
            # намеренное: в выгрузке заказчика телефона нет ни у одного из 2052
            # должников — и не будет, это часть таблицы 1С, — а госномер есть у
            # 2008. Оператор, приславший на этот вопрос госномер, обязан
            # получить отчёт, а не отказ «на телефон не похоже»; страховка не
            # стоит ничего и молча спасает самый частый ввод.
            #
            # Форма записи решает сама: госномер ляжет в госномер, ИНН в ИНН,
            # телефон в телефон, и принятое называется вслух строкой «Принял —
            # Госномер: …». Гадания тут нет — гадание только у одинокого слова
            # буквами, и оно тоже показывается вслух.
            #
            # Ветку сторожат тесты «первый шаг принимает госномер» (кириллицей и
            # латиницей): её отключение весь остальной суите оставляло зелёным.
            card.awaiting_field = None
            return self._apply_free(card, raw)
        if expected is not None and card.guided and spills_beyond(raw, expected):
            # Строка целиком в ответ на вопрос об одном поле — короткий путь,
            # который уже в проде. Шаг закрывается тем, что из строки в него
            # легло, остальное расходится по своим полям, а спрашивать бот
            # будет только то, чего в строке не было.
            card.awaiting_field = None
            return self._apply_free(card, raw)
        if expected is not None:
            return self._apply_expected(card, raw, expected)
        return self._apply_free(card, raw)

    def _apply_expected(self, card: Card, raw: str, expect: Field) -> Applied:
        fragment = classify_fragment(raw, expect=expect)
        if fragment.kind in {FragmentKind.UNKNOWN, FragmentKind.BAD_DATE, FragmentKind.INN10}:
            # Ожидание не снимается: оператор пришёл сюда сам и, скорее всего,
            # хочет попробовать ещё раз. Уйти можно кнопками.
            return Applied(card=card, notice=fragment.reason, rejected=True)
        card.awaiting_field = None
        card.skipped = card.skipped - {expect.value}
        return self._absorb(card, fragment, into=expect)

    def _apply_free(self, card: Card, raw: str) -> Applied:
        """Кнопку не нажимали. Строка может нести сразу несколько полей.

        Здесь работает полный :func:`parse_query`, а не разбор одного фрагмента:
        типичный ввод — строка из 1С целиком, «Иванова Мария Сергеевна
        05.07.1985 770912345601», и разложить её надо всю за одно сообщение.
        """
        parsed = parse_query(raw)
        applied = Applied(card=card)
        words = _name_words(parsed)
        if words:
            applied = _merge(applied, _place_words(card, words))
            if applied.conflict is not None:
                return applied
        if parsed.birth_date is not None:
            applied = _merge(applied, _set(card, "birth_date", parsed.birth_date))
        if parsed.inn is not None:
            applied = _merge(applied, _set(card, "inn", parsed.inn))
        if parsed.phone is not None:
            applied = _merge(applied, _set_phone(card, parsed.phone))
        if parsed.passport is not None:
            applied = _merge(applied, _set_passport(card, parsed.passport))
        if parsed.plate is not None:
            applied = _merge(applied, _set(card, "plate", parsed.plate))
        if parsed.vin is not None:
            applied = _merge(applied, _set(card, "vin", parsed.vin))

        if parsed.ambiguity is not None:
            # Вопрос про десять цифр задаётся ПОСЛЕ того, как всё остальное из
            # строки уже легло в карточку. Иначе «Тестов Андрей Сергеевич
            # 12.03.1985 9204384710» теряло бы и ФИО, и дату ради одного
            # неразрешимого токена — то есть ровно то, от чего лечим.
            card.pending_ten = "".join(ch for ch in parsed.ambiguity.token if ch.isdigit())
            card.awaiting_field = _AWAITING_TEN
            return replace(applied, changed=True)

        if applied.changed:
            notices = [problem.text for problem in parsed.problems]
            return replace(applied, notice=applied.notice or (notices[0] if notices else None))

        # Ничего не легло — разбираемся, что это было, и говорим вслух.
        fragment = classify_fragment(raw)
        return self._absorb_guess(card, fragment)

    def _absorb_guess(self, card: Card, fragment: Fragment) -> Applied:
        if fragment.kind is FragmentKind.NAME_WORD:
            return _place_name_word(card, fragment.word)
        if fragment.kind is FragmentKind.EMPTY:
            return Applied(card=card)
        return Applied(card=card, notice=fragment.reason or None)

    def _absorb(self, card: Card, fragment: Fragment, *, into: Field) -> Applied:
        """Положить разобранный фрагмент в названное поле."""
        match fragment.kind:
            case FragmentKind.DATE:
                return _set(card, "birth_date", fragment.date)
            case FragmentKind.INN12:
                return _set(card, "inn", fragment.digits)
            case FragmentKind.PHONE:
                return _set_phone(card, fragment.phone or "")
            case FragmentKind.PASSPORT:
                return _set_passport(card, fragment.digits)
            case FragmentKind.PLATE:
                return _set(card, "plate", fragment.plate)
            case FragmentKind.VIN:
                return _set(card, "vin", fragment.vin)
            case FragmentKind.FIO:
                assert fragment.name is not None
                return _set_name(card, fragment.name, overwrite=True)
            case FragmentKind.NAME_WORD:
                slots = NAME_SLOTS_FOR.get(into)
                if slots is not None:
                    # Спросили конкретный слот — гадать не о чем. «Николаевна» в
                    # ответ на «Фамилия» это фамилия Николаевна, и спорить с
                    # оператором о его же должнике мы не будем.
                    return _fill_slots(card, [fragment.word], slots)
                return _place_name_word(card, fragment.word, asked=into is Field.FIO)
            case FragmentKind.NAME_PARTS:
                slots = NAME_SLOTS_FOR.get(into, _NAME_SLOTS)
                return _fill_slots(card, list(fragment.words), slots)
            case FragmentKind.TEXT:
                column = _TEXT_COLUMNS.get(into)
                return _set(card, column, fragment.text) if column else Applied(card=card)
            case _:  # pragma: no cover — отсеяно в _apply_expected
                return Applied(card=card)

    # ------------------------------------------------------------ кнопки

    def ask(self, card: Card, field_name: str) -> Card:
        """Ждать конкретное поле. Кнопки схлопываются, строка поля светится."""
        card.awaiting_field = field_name
        # Нажатие опции выводит из ведомого сценария: оператор сказал, чего он
        # хочет, и возвращать его после ответа к «шагу 2 из 3» значило бы
        # спорить с ним о порядке.
        card.guided = False
        return card

    def begin_steps(self, card: Card) -> Card:
        """Встать на первый незаданный из трёх основных шагов."""
        card.guided = True
        card.awaiting_field = None
        step = card.next_step()
        card.guided = step is not None
        card.awaiting_field = step.value if step is not None else None
        return card

    def step_forward(self, card: Card) -> Card:
        """Перейти к следующему шагу. Шаги кончились — ведомый режим снят.

        Единственное место, где двигается ведомый сценарий. Ни «Дальше», ни
        разобранный ответ, ни строка целиком не двигают его сами: иначе три
        разных пути разошлись бы в том, какой вопрос считается заданным.
        """
        step = card.next_step()
        card.awaiting_field = step.value if step is not None else None
        card.guided = step is not None
        return card

    def leave_steps(self, card: Card) -> Card:
        """Выйти из шагов в меню опций, ничего не пропуская.

        Зовётся, когда человек опознан в 1С: спрашивать больше нечего, но и
        помечать оставшиеся шаги «пропустили» нельзя — их не пропускали, на
        них ответила выгрузка.
        """
        card.guided = False
        card.awaiting_field = None
        return card

    def skip(self, card: Card) -> Card:
        """Пропустить ожидаемое поле явно.

        Пропуск отличается от пустого места: пустое — «я не спрашивал»,
        пропущенное — «спросил, ответа нет». В отчёт эта разница не едет (она
        про оператора, а не про качество проверки), но кнопка «Пропустить»
        обязана делать что-то видимое, иначе она неотличима от «Отмены».
        """
        if card.awaiting_field:
            card.skipped = card.skipped | _SKIP_MARKS.get(
                card.awaiting_field, {card.awaiting_field}
            )
            card.awaiting_field = None
        return card

    def cancel_ask(self, card: Card) -> Card:
        card.awaiting_field = None
        card.pending_ten = None
        card.pending_name = None
        return card

    def take_pending_name(self, card: Card) -> PersonName | None:
        """Забрать отложенное ФИО и снять вопрос. Разбор строгий, как везде."""
        raw = card.pending_name
        card.pending_name = None
        if not raw:
            return None
        try:
            return parse_fio(raw)
        except NameParseError:  # pragma: no cover — кладём только разобранное
            return None

    def resolve_ten(self, card: Card, *, as_passport: bool) -> Applied:
        """Ответ на «паспорт или телефон» про десять цифр с девятки."""
        digits = card.pending_ten
        card.pending_ten = None
        card.awaiting_field = None
        if not digits:
            return Applied(card=card, changed=True)
        if as_passport:
            return _set_passport(card, digits)
        return _set_phone(card, f"+7{digits}")

    def _resolve_ten_by_text(self, card: Card, raw: str) -> Applied:
        """Оператор написал вместо того, чтобы нажать.

        Значит это новый ввод, а не ответ. Вопрос снимается, текст разбирается
        как обычно — иначе бот застрял бы на вопросе, на который ему уже не
        отвечают.
        """
        card.pending_ten = None
        card.awaiting_field = None
        return self._apply_free(card, raw)


#: Какие строки карточки гасит один пропуск. Кнопка одна, а строк за ней может
#: стоять несколько: «ФИО» это три слота, «Госномер или VIN» — два. Без этой
#: карты «Пропустить» под ними не делало бы ничего видимого, то есть было бы
#: неотличимо от «Отмены».
_SKIP_MARKS: dict[str, frozenset[str]] = {
    Field.FIO.value: frozenset({Field.FIO.value, "last_name", "first_name", "middle_name"}),
    Field.AUTO.value: frozenset({Field.AUTO.value, "plate", "vin"}),
    Field.LAST_NAME.value: frozenset({Field.LAST_NAME.value, "last_name"}),
    Field.FIRST_NAME.value: frozenset({Field.FIRST_NAME.value, "first_name"}),
    Field.MIDDLE_NAME.value: frozenset({Field.MIDDLE_NAME.value, "middle_name"}),
    Field.CONTRACT.value: frozenset({Field.CONTRACT.value, "contract_number"}),
}

#: Куда ложится поле, которое хранится текстом как есть.
_TEXT_COLUMNS: dict[Field, str] = {
    Field.CONTRACT: "contract_number",
    Field.ADDRESS: "address",
}

#: Значение ``awaiting_field`` для вопроса «паспорт или телефон». Не член
#: :class:`Field`: это не поле карточки, а незакрытый вопрос о том, какое поле
#: перед нами.
_AWAITING_TEN = "ten"


def _field_or_none(name: str | None) -> Field | None:
    try:
        return Field(name) if name else None
    except ValueError:
        # Значение из будущей (или прошлой) версии бота. Молча игнорируем: хуже
        # неизвестного ожидания только застрявшее в нём ожидание.
        return None


def _merge(first: Applied, second: Applied) -> Applied:
    """Склеить два применения в одно.

    ``rejected`` переносится наравне с остальным: он значит «заданный вопрос
    ответа не получил», и потерять его здесь — значит увести сценарий на
    следующий шаг по вводу, который тот же экран только что отверг.
    """
    return Applied(
        card=second.card,
        notice=first.notice or second.notice,
        changed=first.changed or second.changed,
        conflict=first.conflict or second.conflict,
        rejected=first.rejected or second.rejected,
    )


def _set(card: Card, name: str, value: object) -> Applied:
    if card.value(name) == value:
        return Applied(card=card)
    setattr(card, name, value)
    card.skipped = card.skipped - {name}
    return Applied(card=card, changed=True)


def _set_phone(card: Card, phone: str) -> Applied:
    if card.phone == phone and card.phone_masked:
        return Applied(card=card)
    card.phone = phone
    card.phone_masked = mask_phone(phone)
    card.skipped = card.skipped - {"phone"}
    return Applied(card=card, changed=True)


def _set_passport(card: Card, passport: str) -> Applied:
    """Паспорт: и в память, и маской в карточку.

    Раньше отсюда уходило поручение удалить сообщение оператора, а карточка
    печатала маску: бот обещал «номер не сохраняю и сообщение удалю» и обещание
    держал. Владелица это отменила дословно — «надо убрать это, нам надо
    наоборот сохранять эти номера», — и она права: бот закрыт, принадлежит
    одному человеку и добывает документы ровно затем, чтобы тот подал с ними в
    суд. Удалять сообщение с номером, который через минуту сам же покажешь на
    странице, — это не приватность, а неудобство.

    Маска остаётся рядом с номером, а не вместо него: по ней карточка после
    перезапуска отличает «было, но не сохранилось» от «не спрашивали».
    """
    if card.passport == passport and card.passport_masked:
        return Applied(card=card)
    card.passport = passport
    card.passport_masked = mask_passport(passport)
    card.skipped = card.skipped - {"passport"}
    return Applied(card=card, changed=True)


def _set_snils(card: Card, snils: str) -> Applied:
    """СНИЛС: и в память, и маской в карточку. То же, что у паспорта."""
    if card.snils == snils and card.snils_masked:
        return Applied(card=card)
    card.snils = snils
    card.snils_masked = mask_snils(snils)
    card.skipped = card.skipped - {"snils"}
    return Applied(card=card, changed=True)


def _set_name(card: Card, name: PersonName, *, overwrite: bool = False) -> Applied:
    changed = (
        card.last_name != name.last_name
        or card.first_name != name.first_name
        or card.middle_name != name.middle_name
    )
    if not changed and not overwrite:
        return Applied(card=card)
    card.last_name = name.last_name
    card.first_name = name.first_name
    card.middle_name = name.middle_name
    card.skipped = card.skipped - {"last_name", "first_name", "middle_name"}
    return Applied(card=card, changed=changed)


_NAME_SLOTS: tuple[str, str, str] = ("last_name", "first_name", "middle_name")


def _name_words(parsed: ParsedQuery) -> list[str]:
    """Слова-имена из строки — независимо от того, сложились они в ФИО или нет.

    Куда их класть, решает карточка, а не разбор: у неё видно, какие слоты уже
    заняты, а у :func:`parse_fio` — нет. Он по-прежнему строгий и по-прежнему
    зовётся там, где слов действительно на целое имя (:func:`_as_fio`).
    """
    words = parsed.name.full.split() if parsed.name is not None else list(parsed.leftover)
    # Кириллица обязательна: «asdf» проходит алфавит имени и без этого условия
    # молча уезжал бы в фамилию. Когда поле названо кнопкой, требование
    # снимается — там оператор уже сказал, что это имя.
    return words if words and all(looks_like_russian_name(word) for word in words) else []


def _place_words(card: Card, words: list[str]) -> Applied:
    """Разложить слова-имена по слотам карточки.

    Ровно та «умная логика», которую просили: фамилия отдельно, потом имя,
    потом отчество — и всё это к тому же человеку. Разбор ФИО сам по себе так
    не умеет и уметь не должен: «Елена Николаевна» он законно читает как
    фамилию с именем, потому что не знает, что фамилия уже есть.

    Три случая, и граница между ними — состояние карточки, а не текст.

    Карточка пуста — строка целиком есть ФИО, работает строгий
    :func:`parse_fio`.

    Карточка несёт человека целиком (фамилия и имя) — присланное ФИО это либо
    исправление, либо ДРУГОЙ человек, и молча выбрать нельзя (:func:`_contradicts`).

    Карточка заполнена частично — слова дописываются в пустые слоты. Отсюда
    единственное исключение по форме слова: два слова поверх одной фамилии
    читаются как «имя и отчество», только если второе на отчество и похоже.
    «Иванов Иван» поверх «Иванова» — это другой человек, а не имя Иванов.
    """
    if len(words) == 1:
        return _place_name_word(card, capitalize_name(words[0]))

    filled = [slot for slot in _NAME_SLOTS if getattr(card, slot)]
    complete = bool(card.last_name and card.first_name)
    if complete or not filled or not _continues_the_name(card, words):
        name = _as_fio(words)
        if name is None:
            return Applied(card=card, notice=_TOO_MANY_WORDS)
        if _contradicts(card, name):
            card.pending_name = name.full
            return Applied(card=card, conflict=name, changed=True)
        return _set_name(card, name, overwrite=True)
    return _fill_free_slots(card, words)


def _continues_the_name(card: Card, words: list[str]) -> bool:
    """Дописывают ли эти слова уже начатое имя — или это новое ФИО целиком.

    Три слова всегда новое ФИО: больше трёх слотов не бывает. Два слова поверх
    одной фамилии — имя с отчеством, если второе слово на отчество похоже.
    """
    if len(words) > len(_NAME_SLOTS) - 1:
        return False
    free = [slot for slot in _NAME_SLOTS if not getattr(card, slot)]
    if len(words) > len(free):
        return False
    return len(words) == 1 or looks_like_patronymic(words[-1])


def _fill_free_slots(card: Card, words: list[str]) -> Applied:
    applied = Applied(card=card)
    placed: list[str] = []
    for word in words:
        slot = _free_slot(card, word)
        if slot is None:
            break
        setattr(card, slot, capitalize_name(word))
        card.skipped = card.skipped - {slot}
        placed.append(f"«{echo(word)}» — в {_SLOT_TITLES[slot]}")
        applied = replace(applied, changed=True)
    if not placed:
        return Applied(card=card, notice=_TOO_MANY_WORDS)
    return replace(applied, notice=_FILLED_NOTICE.format(", ".join(placed)))


def _fill_slots(card: Card, words: list[str], slots: tuple[str, ...]) -> Applied:
    """Разложить слова по НАЗВАННЫМ слотам, начиная с первого из них.

    Отличается от :func:`_fill_free_slots` тем, что слоты заданы вопросом, а не
    тем, какие из них пусты: ответ на «Фамилия» переписывает фамилию, даже если
    она уже стоит. Оператор нажал «Исправить» или переотвечает на шаг — в обоих
    случаях он знает лучше.
    """
    if len(words) > len(slots):
        return Applied(card=card, notice=TOO_MANY_NAME_WORDS, rejected=True)
    applied = Applied(card=card)
    for slot, word in zip(slots, words, strict=False):
        applied = _merge(applied, _set(card, slot, capitalize_name(word)))
    return applied


def _as_fio(words: list[str]) -> PersonName | None:
    try:
        return parse_fio(" ".join(words))
    except NameParseError:
        return None


def _contradicts(card: Card, name: PersonName) -> bool:
    """Прислали полное ФИО, а в карточке стоит ДРУГАЯ фамилия.

    Единственная защита от молчаливого слияния двух должников, и она нужна:
    «новый человек или исправление опечатки» из текста неразрешимо, а цена
    ошибки — отчёт, где производства одного приписаны другому.

    Совпадение фамилии считается перезаписью и проходит молча: «Иванова Мария»
    поверх «Иванова М.» — это уточнение, а не второй человек.
    """
    return bool(card.last_name) and card.last_name != name.last_name


def _place_name_word(card: Card, word: str, *, asked: bool = False) -> Applied:
    """Одно слово буквами — в первый пустой слот ФИО.

    Слоты заполняются по порядку: фамилия, имя, отчество. Из порядка есть одно
    исключение — слово с окончанием отчества («Николаевна») при пустых слотах
    ставится сразу в отчество: «Николаевна» в графе «Фамилия» это ошибка,
    заметная глазом, и чинить её оператору дороже, чем нам угадать.

    Догадка **показывается**, а не прячется: строка «„Иванова“ записал в
    фамилию. Не туда — нажмите „Исправить ФИО“» стоит под карточкой. Спрятанная
    догадка и есть тот самый молчаливый разбор, от которого лечим.
    """
    if not word:
        return Applied(card=card)
    slot = _free_slot(card, word)
    if slot is None:
        # Все три слота заняты. Считаем это исправлением фамилии: чаще всего
        # так и есть — оператор увидел не ту догадку и прислал слово заново.
        slot = "last_name"
    setattr(card, slot, word)
    card.skipped = card.skipped - {slot}
    notice = None if asked else _GUESS_NOTICE.format(word=echo(word), slot=_SLOT_TITLES[slot])
    return Applied(card=card, changed=True, notice=notice)


def _free_slot(card: Card, word: str) -> str | None:
    if looks_like_patronymic(word) and not card.middle_name:
        return "middle_name"
    for slot in ("last_name", "first_name", "middle_name"):
        if not getattr(card, slot):
            return slot
    return None


_SLOT_TITLES = {"last_name": "фамилию", "first_name": "имя", "middle_name": "отчество"}

_GUESS_NOTICE = "«{word}» записал в {slot}. Не туда — нажмите «Исправить ФИО»."

_FILLED_NOTICE = "Дописал: {}. Не туда — нажмите «Исправить ФИО»."

#: Формулировка одна на два места: разбор шага и разбор свободной строки.
_TOO_MANY_WORDS = TOO_MANY_NAME_WORDS


def fill_from_bridge(
    card: Card,
    *,
    name: PersonName,
    birth_date: date | None = None,
    inn: str | None = None,
    passport: str | None = None,
    snils: str | None = None,
    passport_issued: date | None = None,
) -> None:
    """Положить в карточку всё, что мост поднял по номеру телефона.

    Раньше отсюда переносились только ФИО и дата рождения, а паспорт, ИНН и
    СНИЛС из того же — уже оплаченного — ответа молча терялись: карточка их не
    показывала, отчёт не получал, а владелец шёл искать те же документы руками.
    Это и был вопрос «почему паспорт не доезжает»: он доезжал до провайдера и
    не доезжал до карточки.

    Перенос происходит ТОЛЬКО в пустые поля. Введённое оператором старше
    найденного: он держит договор в руках, а мост собирает личность из чужих
    находок, объединённых одним номером телефона, — и номером пользуются и
    родственники, и прежние владельцы номера. Спор между ними всегда решается
    в пользу человека.

    Имя — исключение и ставится целиком: мост зовут только тогда, когда имени в
    карточке нет вовсе (см. ``_resolve_name``), так что затирать здесь нечего.

    """
    _set_name(card, name)
    if birth_date is not None and card.birth_date is None:
        card.birth_date = birth_date
    if inn and not card.inn:
        card.inn = inn
    if passport and not card.passport_masked:
        _set_passport(card, passport)
    if snils and not card.snils_masked:
        _set_snils(card, snils)
    if passport_issued is not None and card.passport_issued is None:
        card.passport_issued = passport_issued


__all__ = [
    "FIELD_ORDER",
    "FIELD_TITLES",
    "OPTIONAL_ROWS",
    "Applied",
    "Card",
    "CardSecrets",
    "QueryCardService",
    "fill_from_bridge",
]
