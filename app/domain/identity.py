"""Identity value objects and normalization.

Russian names arrive in wildly inconsistent shapes. Everything that compares two
people goes through here so the rules live in exactly one place.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.utils.hashing import normalize_token

_NAME_SEPARATORS = re.compile(r"[\s ]+")
_NAME_ALLOWED = re.compile(r"^[а-яёa-z\-']+$", re.IGNORECASE)
# Anything that is not part of a word separates two words. Dots matter here:
# sources abbreviate as «Бычков Д.Ю.», with no space to split on.
_NAME_WORD_SPLIT = re.compile(r"[^\w'\-]+", re.UNICODE)
_SOLE_PROPRIETOR_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("ип",),
    ("индивидуальный", "предприниматель"),
)
# Хвосты, которыми реестры дополняют ФИО. Все пять форм ниже — живые:
# «Тестов Андрей Сергеевич, 15.03.1980 г.р.» (ФНП и ЕГРЮЛ печатают дату
# рождения прямо в строке лица), «ТЕСТОВ АНДРЕЙ СЕРГЕЕВИЧ (ИНН 770...)» (КАД,
# участник с идентификатором), «Тестов Андрей Сергеевич ИП» (ЕГРИП, форма
# записи в конце, а не в начале). Убираются ДО разбиения на слова, а не после:
# «15.03.1980 г.р.» распадается на слова «г» и «р», и выбрасывать одиночные
# буквы после разбиения значило бы сломать сравнение по инициалам, где «Г.» —
# это инициал.
_LEGAL_FORM_ANYWHERE = re.compile(r"\b(?:ип|индивидуальный\s+предприниматель)\b")
_IDENTIFIER_TAIL = re.compile(r"\b(?:инн|огрнип|огрн|снилс|кпп|паспорт)\b[\s:№n°-]*\d*")
# «г.р.» вырезается только следом за датой: одиночные «г» и «р» бывают и
# инициалами («Тестов Г.Р.»), и терять их вслепую нельзя.
#
# Публичное имя: тот же хвост приходится срезать и на входе — оператор копирует
# «Тестов Андрей Сергеевич 15.03.1980 г.р.» из выгрузки целиком. Две копии
# одного правила разошлись бы на первой же правке.
BIRTH_DATE_MARKER = re.compile(r"(?<=\d)\s*г\s*\.?\s*р\s*\.?|\b(?:года|дата)\s+рожд\w*")
_PARENTHESIZED = re.compile(r"\(([^)]*)\)")

FIO_MIN_PARTS = 2
FIO_MAX_PARTS = 3
#: Частицы тюркского отчества: «Ахмед оглы», «Мамед кызы». В паспорте они пишутся
#: отдельным словом, поэтому ФИО с ними — четыре слова, а не три, и строгий
#: разбор их отвергал. На выгрузке заказчика это 72 человека из 2315: их имена
#: оставались неразобранными, а значит не участвовали в сверке с ответами
#: источников — совпадение по такому должнику читалось как «слабое».
#: Частица приклеивается к отчеству и остаётся строчной, как в документе.
NAME_PARTICLES = frozenset({"оглы", "оглу", "улы", "уулу", "угли", "кызы", "гызы", "кизи"})
INN_INDIVIDUAL_LENGTH = 12
INN_ENTITY_LENGTH = 10
#: Серия и номер российского паспорта, слитно.
PASSPORT_LENGTH = 10
#: СНИЛС: девять значащих цифр плюс двузначная контрольная сумма.
SNILS_LENGTH = 11
SNILS_PAYLOAD_LENGTH = 9
#: Остаток, с которого контрольное число СНИЛС становится нулём. Не «модуль
#: 101 и всё»: у 100 и 101 результат один и тот же — ноль.
SNILS_MODULO = 101
#: Российский номер в национальном формате: 8/7 плюс десять цифр.
PHONE_LENGTH = 11
VIN_LENGTH = 17
# I, O and Q are excluded from the VIN alphabet to avoid confusion with 1 and 0.
_VIN_ALLOWED = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")
_PLATE_PATTERNS = (
    # Passenger plate: А123ВС77 / А123ВС777
    re.compile(r"^[АВЕКМНОРСТУХ]\d{3}[АВЕКМНОРСТУХ]{2}\d{2,3}$"),
    # Trailer / motorcycle / public transport variants.
    re.compile(r"^[АВЕКМНОРСТУХ]{2}\d{4}\d{2,3}$"),
    re.compile(r"^\d{4}[АВЕКМНОРСТУХ]{2}\d{2,3}$"),
    re.compile(r"^[АВЕКМНОРСТУХ]{2}\d{3}\d{2,3}$"),
)
# Latin look-alikes routinely typed instead of Cyrillic on plates.
_PLATE_TRANSLITERATION = str.maketrans(
    {
        "A": "А",
        "B": "В",
        "E": "Е",
        "K": "К",
        "M": "М",
        "H": "Н",
        "O": "О",
        "P": "Р",
        "C": "С",
        "T": "Т",
        "Y": "У",
        "X": "Х",
    }
)


class NameParseError(ValueError):
    """Raised when input cannot be read as a name without guessing."""


class PersonName(BaseModel):
    """A parsed Russian name.

    A middle name is optional because plenty of records genuinely lack one; a
    surname and a given name are not, because without them no meaningful
    matching is possible.
    """

    model_config = ConfigDict(frozen=True)

    last_name: str
    first_name: str
    middle_name: str | None = None

    @property
    def full(self) -> str:
        parts = [self.last_name, self.first_name, self.middle_name]
        return " ".join(part for part in parts if part)

    @property
    def normalized(self) -> str:
        return normalize_token(self.full)

    @property
    def normalized_short(self) -> str:
        """Surname + given name only — used for comparing against sources that
        omit the patronymic."""
        return normalize_token(f"{self.last_name} {self.first_name}")

    @property
    def has_middle_name(self) -> bool:
        """Есть ли отчество.

        Нужно мосту «паспорт → ИНН»: ФНС по ФИО без отчества штатно возвращает
        пусто, и такой ответ обязан объясняться отдельно, а не читаться как
        «ИНН у человека нет».
        """
        return bool(self.middle_name)


def parse_fio(raw: str) -> PersonName:
    """Parse ``Фамилия Имя [Отчество]``.

    Deliberately strict: an unparseable name is rejected instead of being split
    on a guess, because a wrong split silently poisons every downstream match.
    """
    if not raw or not raw.strip():
        raise NameParseError("ФИО не указано")
    parts = [part for part in _NAME_SEPARATORS.split(raw.strip()) if part]
    # Частица отчества — не отдельное слово имени: «Ахмед оглы» это одно
    # отчество. Склеиваем до счёта слов, иначе такое ФИО не проходит по длине.
    if len(parts) == FIO_MAX_PARTS + 1 and parts[-1].casefold() in NAME_PARTICLES:
        parts = [*parts[:-2], f"{parts[-2]} {parts[-1].casefold()}"]
    if len(parts) < FIO_MIN_PARTS:
        raise NameParseError("Нужно как минимум фамилия и имя. Пример: Иванов Иван Иванович")
    if len(parts) > FIO_MAX_PARTS:
        raise NameParseError("Слишком много слов. Ожидается: Фамилия Имя Отчество")
    for part in parts:
        # Частица уже проверена по словарю, а пробел внутри отчества алфавит
        # имени не пропускает — проверяем слова, а не склеенную форму.
        if not all(_NAME_ALLOWED.match(word) for word in part.split()):
            raise NameParseError(f"Недопустимые символы в «{part}»")
    normalized = [capitalize_name(part) for part in parts]
    return PersonName(
        last_name=normalized[0],
        first_name=normalized[1],
        middle_name=normalized[2] if len(normalized) == FIO_MAX_PARTS else None,
    )


def is_name_word(word: str | None) -> bool:
    """Может ли отдельное слово быть частью имени.

    Тот же алфавит, что проверяет :func:`parse_fio`, — буквы, дефис, апостроф.
    Публичной эта проверка стала для карточки запроса: она принимает ФИО по
    одному слову, и ей нужно отличить «Клочкова» от «77091», не ослабляя сам
    разбор ФИО и не заводя вторую копию алфавита.
    """
    return bool(word and _NAME_ALLOWED.match(word))


def capitalize_name(part: str) -> str:
    """Capitalize each hyphen-separated segment: ``петров-водкин`` -> ``Петров-Водкин``.

    Публичная ради карточки запроса: она принимает имя по одному слову, минуя
    :func:`parse_fio`, и приводить регистр обязана теми же правилами — иначе
    «клочкова», набранное отдельным сообщением, оседало бы в карточке строчным.
    """
    return "-".join(segment.capitalize() for segment in part.split("-"))


class NameMatch(Enum):
    """How strongly a free-form name agrees with a parsed one.

    Пять уровней, а не четыре, и пятый — самый важный. ``NONE`` означает
    ПРОТИВОРЕЧИЕ: строка прочитана, и в ней стоит другой человек.
    ``INCONCLUSIVE`` означает, что читать было нечего: пустая строка, одна
    фамилия, хвост из слов, который не удалось разобрать. Пока эти два ответа
    были одним, добавление поля в карту могло только спрятать запись — источник,
    приславший «Тестов Андрей Сергеевич, 15.03.1980 г.р.», выглядел
    свидетельством против нас, тогда как отсутствие поля вовсе не стоило ничего
    (:data:`app.services.identity.NAME_UNKNOWN`).
    """

    NONE = "none"
    #: Сравнивать было нечего: доказательства нет ни за, ни против.
    INCONCLUSIVE = "inconclusive"
    #: Фамилия совпала, имя и отчество сошлись только инициалами.
    INITIALS = "initials"
    #: Фамилия и имя совпали, отчество сравнить не с чем.
    SHORT = "short"
    FULL = "full"


#: Насколько ответ благоприятен для записи. Нужен ровно там, где одну и ту же
#: строку можно прочитать двумя способами (см. скобки в ``_comparable_variants``)
#: и надо выбрать лучшее прочтение.
_MATCH_STRENGTH: dict[NameMatch, int] = {
    NameMatch.NONE: 0,
    NameMatch.INCONCLUSIVE: 1,
    NameMatch.INITIALS: 2,
    NameMatch.SHORT: 3,
    NameMatch.FULL: 4,
}


def is_name_evidence(match: NameMatch) -> bool:
    """Подтверждает ли ответ, что это тот самый человек.

    ``INCONCLUSIVE`` — не подтверждает: «мы не смогли прочитать» не должно
    работать как «совпало». Отдельная функция, потому что проверка ``is not
    NameMatch.NONE`` читается как то же самое и им не является.
    """
    return match in {NameMatch.FULL, NameMatch.SHORT, NameMatch.INITIALS}


def compare_names(name: PersonName, raw: str | None) -> NameMatch:
    """Compare a free-form name against a parsed one **without using word order**.

    Word order is not evidence about who a person is. ФНП prints its pledgors as
    ``ИМЯ ОТЧЕСТВО ФАМИЛИЯ`` (``СЕРГЕЙ АНДРЕЕВИЧ ПЕТРОВ``), Федресурс as
    ``Фамилия Имя Отчество``, our own CSV as whatever the operator typed. A
    positional comparison read the first of those as a different person
    entirely, and a found pledge came out of the report as "не найдено" — the
    one inversion this tool must never produce.

    What is compared is the *multiset* of words: the same words, each the same
    number of times. That is deliberately not an intersection and not a subset
    of convenience — «Иванов Иван Иванович» and «Иванов Иван Петрович» differ by
    exactly one word and stay two different people, which is the whole reason a
    name alone never reaches the confirmed band anyway.

    Три уступки, и каждая названа в ответе, а не спрятана в нём:

    :attr:`NameMatch.SHORT` is the concession the positional comparison already
    made — when one side carries no patronymic, a surname and a given name are
    all there is to compare. Лишние слова СВЕРХ нашего имени она тоже терпит:
    реестры дописывают к ФИО что угодно, вплоть до «Алиев Рашид Мамед оглы».
    Чего она больше не терпит — ЧУЖОГО отчества на месте нашего: «Леликов Андрей
    Петрович» при нашем «Леликов Андрей Сергеевич» это другой человек, и
    выдавать его за должника нельзя (см. :func:`_patronymic_verdict`).

    :attr:`NameMatch.INITIALS` is for the sources that abbreviate. КАД prints
    half of its participants as «Бычков Д.Ю.» and «ИП Иванов И.И.», and against
    a full name those are neither equal nor a subset, so a strict comparison
    called them strangers. A surname plus initials is genuinely weaker evidence
    than a name — it is returned as its own level so that each caller can decide
    what it is worth, and no caller has to guess from a boolean.

    :attr:`NameMatch.INCONCLUSIVE` — третий ответ, и он же ответ по умолчанию для
    всего, что не прочиталось: пустой строки, одной фамилии, хвоста из двух и
    более неопознанных слов. Раньше всё это возвращало ``NONE``, то есть
    противоречие, — и «ТЕСТОВ АНДРЕЙ СЕРГЕЕВИЧ (ИНН 770…)» стоил записи дороже,
    чем полное отсутствие ФИО в ответе.
    """
    variants = _comparable_variants(raw)
    if not variants:
        # Строки нет вовсе, либо в ней не осталось ни одного слова-имени.
        return NameMatch.INCONCLUSIVE
    return max(
        (_compare_words(name, words) for words in variants),
        key=lambda match: _MATCH_STRENGTH[match],
    )


def _compare_words(name: PersonName, words: list[str]) -> NameMatch:
    counted = Counter(words)
    mine_full = Counter(_name_words(name.full))
    mine_short = Counter(_name_words(name.normalized_short))
    if counted == mine_full:
        return NameMatch.FULL
    if mine_full <= counted:
        # Наше имя целиком внутри строки, остальное — приписка источника.
        return NameMatch.SHORT
    if mine_short <= counted:
        # Фамилия и имя на месте, нашего отчества нет. Всё решает то, что
        # стоит вместо него.
        return _patronymic_verdict(name, counted - mine_short)
    if _initials_match(name, words):
        return NameMatch.INITIALS
    if _surname_alone(name, counted):
        return NameMatch.INCONCLUSIVE
    return NameMatch.NONE


def _patronymic_verdict(name: PersonName, surplus: Counter[str]) -> NameMatch:
    """Нашего отчества в строке нет. Что стоит на его месте?

    Ничего — сравнивать нечего, это давняя уступка :attr:`NameMatch.SHORT`.

    Одна буква — инициал: совпал с нашим, значит то же отчество записано
    коротко; не совпал — это другой человек, ровно как в :func:`_initials_match`.

    Одно слово — чужое отчество на месте нашего, то есть противоречие. Именно
    этот случай раньше давал ``SHORT``: КАД возвращал дело по ИНН должника,
    ответчиком в нём стоял однофамилец с другим отчеством, и код записывал
    должника в ответчики по чужому делу.

    Два и более слов — хвост, который мы не прочитали, а не отчество. Это не
    доказательство ни за, ни против: ``INCONCLUSIVE``.
    """
    if not surplus:
        return NameMatch.SHORT
    words = list(surplus.elements())
    middle = normalize_token(name.middle_name)
    if all(len(word) == 1 for word in words):
        return (
            NameMatch.SHORT
            if middle and all(word == middle[0] for word in words)
            else NameMatch.NONE
        )
    if len(words) == 1:
        return NameMatch.NONE
    return NameMatch.INCONCLUSIVE


def _surname_alone(name: PersonName, counted: Counter[str]) -> bool:
    """В строке нет ничего, кроме нашей фамилии.

    «Тестов» — это не «другой Тестов», это отсутствие имени. Противоречием
    такую строку считать нельзя, доказательством — тем более.
    """
    surname = normalize_token(name.last_name)
    return bool(counted[surname]) and sum(counted.values()) == counted[surname]


def _comparable_variants(raw: str | None) -> list[list[str]]:
    """Прочтения строки, из которых берётся лучшее.

    Скобки — единственное место, где прочтений честно два. «Иванова (Петрова)
    Мария» — это либо Иванова с девичьей Петровой, либо Петрова с девичьей
    Ивановой, и какая из них наша, знает только наш собственный список слов.
    Поэтому сравниваются оба варианта: со скобочной вставкой и без неё.
    """
    text = _without_registry_noise(normalize_token(raw))
    if not text:
        return []
    readings = [_PARENTHESIZED.sub(" ", text)]
    if _PARENTHESIZED.search(text):
        readings.append(text.replace("(", " ").replace(")", " "))
    variants: list[list[str]] = []
    seen: set[tuple[str, ...]] = set()
    for reading in readings:
        words = _name_words(reading)
        key = tuple(sorted(words))
        if words and key not in seen:
            seen.add(key)
            variants.append(words)
    return variants


def _without_registry_noise(text: str) -> str:
    """Убрать из уже нормализованной строки всё, что не является именем.

    Живые формы, на которых сравнение молча давало ноль: «Тестов Андрей
    Сергеевич, 15.03.1980 г.р.», «ТЕСТОВ АНДРЕЙ СЕРГЕЕВИЧ (ИНН 770…)», «Тестов
    Андрей Сергеевич ИП». Ни одна из них не про другого человека — все три про
    нашего, с припиской.
    """
    for pattern in (_IDENTIFIER_TAIL, BIRTH_DATE_MARKER, _LEGAL_FORM_ANYWHERE):
        text = pattern.sub(" ", text)
    return " ".join(text.split())


def _name_words(raw: str | None) -> list[str]:
    """Split a name into comparable words, dots and commas included.

    ``Бычков Д.Ю.`` has to become three words and not two, or the initials never
    line up with anything. Hyphens and apostrophes stay inside a word:
    ``Петров-Водкин`` is one surname, not two.
    """
    words = [word for word in _NAME_WORD_SPLIT.split(normalize_token(raw)) if _is_name_word(word)]
    return _without_legal_form(words)


def _is_name_word(word: str) -> bool:
    return any(char.isalpha() for char in word)


def _without_legal_form(words: list[str]) -> list[str]:
    """``ИП Иванов И.И.`` -> ``Иванов И.И.``

    A sole-proprietor prefix is a legal form, not part of anybody's name. Only
    that one is dropped: a company name is not a person's name at all, and
    trimming it into one would manufacture matches.
    """
    for prefix in _SOLE_PROPRIETOR_PREFIXES:
        if words[: len(prefix)] == list(prefix):
            return words[len(prefix) :]
    return words


def _initials_match(name: PersonName, words: list[str]) -> bool:
    """Surname in full, given name and patronymic as bare initials.

    Deliberately narrow. The surname must match as a whole word, every remaining
    word must be a single letter, and the *given name's* initial must be among
    them — «Бычков Ю.» is not Дмитрий Юрьевич with a letter missing, it is most
    likely a different Бычков. When we ourselves hold no patronymic, one extra
    initial is tolerated, which is the same allowance :attr:`NameMatch.SHORT`
    makes for one extra word.
    """
    surname = normalize_token(name.last_name)
    if surname not in words:
        return False
    rest = list(words)
    rest.remove(surname)
    if not rest or len(rest) > FIO_MAX_PARTS - 1:
        return False
    if any(len(word) != 1 for word in rest):
        return False
    expected = [
        normalized[0]
        for part in (name.first_name, name.middle_name)
        if (normalized := normalize_token(part))
    ]
    given = Counter(rest)
    # ``expected[0]`` — инициал имени, и он обязан присутствовать.
    if not expected or not given[expected[0]]:
        return False
    surplus = given - Counter(expected)
    return sum(surplus.values()) <= (0 if name.middle_name else 1)


def normalize_phone(raw: str | None) -> str | None:
    """Normalize a Russian phone number to ``+7XXXXXXXXXX``.

    Returns ``None`` for anything that is not a plausible RU number rather than
    padding or truncating it into one.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    if not digits:
        return None
    if len(digits) == PHONE_LENGTH and digits[0] in {"7", "8"}:
        return f"+7{digits[1:]}"
    if len(digits) == PHONE_LENGTH - 1 and digits[0] == "9":
        return f"+7{digits}"
    return None


def normalize_plate(raw: str | None) -> str | None:
    """Normalize a Russian licence plate, transliterating Latin look-alikes."""
    if not raw:
        return None
    cleaned = re.sub(r"[\s\-]", "", raw).upper().translate(_PLATE_TRANSLITERATION)
    if not cleaned:
        return None
    return cleaned if any(pattern.match(cleaned) for pattern in _PLATE_PATTERNS) else None


def normalize_vin(raw: str | None) -> str | None:
    """Validate and normalize a VIN: exactly 17 chars from the legal alphabet."""
    if not raw:
        return None
    cleaned = re.sub(r"[\s\-]", "", raw).upper()
    return cleaned if _VIN_ALLOWED.match(cleaned) else None


def normalize_inn(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) in {INN_ENTITY_LENGTH, INN_INDIVIDUAL_LENGTH} else None


def normalize_passport(raw: str | None) -> str | None:
    """Normalize an RF passport to 10 digits. Never logged, rarely stored."""
    if not raw:
        return None
    digits = re.sub(r"\D", "", raw)
    return digits if len(digits) == PASSPORT_LENGTH else None


def normalize_snils(raw: str | None) -> str | None:
    """СНИЛС из строки поставщика — одиннадцать цифр с сошедшейся контрольной.

    Контрольная сумма проверяется, а не игнорируется, и причина не в
    аккуратности. Поставщик отдаёт одиннадцатизначные числа в нескольких полях
    сразу, и одиннадцать цифр сами по себе не значат ничего: ИНН физлица — это
    двенадцать, ИНН юрлица — десять, а одиннадцать бывает и у внутреннего
    идентификатора чужой системы. Владелица уже приняла за свой ИНН
    одиннадцатизначное число из ответа — им оказался её же СНИЛС, и различила
    их ровно эта проверка. Записать в карточку чужой идентификатор под видом
    СНИЛС — это подать иск с ним.

    Алгоритм — тот, что в пенсионном законодательстве: девять значащих цифр
    взвешиваются позициями с девятой по первую, остаток от деления на 101
    сравнивается с двумя последними; 100 и 101 дают ноль. Номера до 001-001-998
    контрольного числа не имеют вовсе, но в жизни их не выдают, и принимать
    такие ради полноты значит открыть дверь всему подряд.
    """
    if not raw:
        return None
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) != SNILS_LENGTH:
        return None
    payload, checksum = digits[:SNILS_PAYLOAD_LENGTH], int(digits[SNILS_PAYLOAD_LENGTH:])
    total = sum(
        int(digit) * (SNILS_PAYLOAD_LENGTH - position) for position, digit in enumerate(payload)
    )
    remainder = total % SNILS_MODULO
    expected = 0 if remainder in (100, SNILS_MODULO) else remainder
    return digits if expected == checksum else None


def normalize_address(raw: str | None) -> str | None:
    if not raw:
        return None
    collapsed = " ".join(raw.split())
    return collapsed or None


@dataclass(frozen=True, slots=True)
class IdentityKey:
    """The set of signals used to decide whether two records are one person."""

    name: PersonName | None
    birth_date: date | None
    inn: str | None
    phone: str | None

    @property
    def has_strong_identifier(self) -> bool:
        """True when something better than a name alone is available."""
        return bool(self.birth_date or self.inn or self.phone)


class VehicleDescriptor(BaseModel):
    """What the operator knows about a car — every field optional by design."""

    model_config = ConfigDict(frozen=True)

    make: str | None = None
    model: str | None = None
    plate: str | None = None
    vin: str | None = None

    @property
    def has_unique_identifier(self) -> bool:
        """A make and model identify a *type* of car, never a specific one."""
        return bool(self.plate or self.vin)

    @property
    def title(self) -> str:
        parts = [self.make, self.model]
        label = " ".join(part for part in parts if part)
        identifiers = [self.plate, self.vin]
        suffix = " / ".join(item for item in identifiers if item)
        return " — ".join(item for item in (label, suffix) if item) or "—"


class SearchSubject(BaseModel):
    """Everything known about the subject of one search.

    This is the single input every provider receives. Providers pick the fields
    they can legally use and ignore the rest.
    """

    model_config = ConfigDict(frozen=True)

    search_type: str
    name: PersonName | None = None
    birth_date: date | None = None
    phone: str | None = None
    inn: str | None = None
    passport: str | None = None
    #: СНИЛС. Ни один поставщик по нему не ищет и ни один не спрашивает его на
    #: входе — он едет в субъекте только затем, чтобы попасть в карточку и в
    #: отчёт: владельцу он нужен в заявлении, а добывается вместе с паспортом
    #: одним и тем же обращением. В ``redact_subject`` он вычеркнут наравне с
    #: паспортом.
    snils: str | None = None
    address: str | None = None
    regions: tuple[str, ...] = Field(default_factory=tuple)
    vehicle: VehicleDescriptor | None = None
    contract_number: str | None = None
    claim_number: str | None = None
    debtor_id: str | None = None

    @property
    def identity_key(self) -> IdentityKey:
        return IdentityKey(
            name=self.name,
            birth_date=self.birth_date,
            inn=self.inn,
            phone=self.phone,
        )

    @property
    def display_name(self) -> str:
        if self.name:
            return self.name.full
        for candidate in (self.contract_number, self.claim_number, self.debtor_id):
            if candidate:
                return candidate
        if self.vehicle:
            return self.vehicle.title
        if self.address:
            return self.address
        if self.inn:
            # Поиск по одному ИНН — законный вход: банкротство, статус ИП и
            # арбитраж только по нему и ищут. Без этой ветки такой отчёт
            # назывался бы «—» и в чате, и в истории.
            from app.utils.masking import mask_inn

            return mask_inn(self.inn) or "—"
        return "—"
