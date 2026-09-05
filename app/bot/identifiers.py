"""Разбор одной свободной строки, которую оператор набрал про должника.

Один вход вместо пяти вопросов. Оператор пишет всё, что у него есть, в любом
порядке — «Иванов Иван Иванович 01.01.1985 770912345601», «ИНН 770912345601»,
просто «Сидоров Сидор Сидорович», — а :func:`parse_query` раскладывает это по
полям. Спрашивать бот будет ровно то, чего не понял, и ровно тогда, когда без
этого нельзя.

Три правила, из которых состоит весь модуль.

**Никогда не бросает.** Строку набрал живой человек на бегу; исключение в
разборе означало бы «попробуйте ещё раз» вместо ответа. Всё, что не разобралось,
приезжает полями :attr:`ParsedQuery.name_error` и :attr:`ParsedQuery.problems` —
их проговаривают вслух, но проверку они не останавливают.

**Никогда не гадает молча.** Нераспознанная дата не исчезает, а становится
:class:`Problem` — иначе «15.13.1985» превратилось бы в поиск без даты рождения,
и оператор увидел бы «ФССП: нечем спросить», хотя дату он дал. Десять цифр с
девятки — это и серия паспорта (90xx, 92xx), и мобильный без кода страны;
ошибка здесь необратима: угаданный «телефон» закрывает единственный вход в мост
«паспорт → ИНН». Такой случай приезжает :class:`Ambiguity` и стоит одного
вопроса.

**Разбор по токенам, а не по всей строке.** Метка ищется целым словом, поэтому
«Котельников» — не «тел», а «Иннокентьев» — не «инн»; цифры считаются по группе,
а не по строке, поэтому «Иванов Иван Иванович 01.01.1985 770912345601»
раскладывается, а не отвергается как «слишком много слов».

Разбор ФИО по-прежнему делает строгий :func:`app.domain.identity.parse_fio`:
неверное разбиение имени молча отравляет всякое последующее сопоставление, и
ослаблять его здесь незачем.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

from app.domain.identity import (
    BIRTH_DATE_MARKER,
    INN_ENTITY_LENGTH,
    INN_INDIVIDUAL_LENGTH,
    PASSPORT_LENGTH,
    PHONE_LENGTH,
    NameParseError,
    PersonName,
    normalize_phone,
    normalize_plate,
    normalize_vin,
    parse_fio,
)
from app.utils.dates import MAX_PLAUSIBLE_AGE_YEARS, MIN_PLAUSIBLE_YEAR, parse_date, utcnow

# ---------------------------------------------------------------- результат


class ProblemKind(StrEnum):
    """Что в строке прочиталось, но использовать это нельзя."""

    #: Токен похож на дату, но датой не является.
    BAD_DATE = "bad_date"
    #: Десять цифр под меткой «ИНН» — это идентификатор организации.
    ENTITY_INN = "entity_inn"
    #: Группа цифр не подошла ни под один известный идентификатор.
    UNKNOWN_DIGITS = "unknown_digits"


@dataclass(frozen=True, slots=True)
class Problem:
    """Оговорка к разбору: проговаривается оператору, но не блокирует его.

    ``token`` — ровно то, что написал человек, чтобы он узнал своё в ответе.
    ``text`` — готовая фраза; собирать её в хэндлере значило бы развести
    формулировку и причину по разным файлам.
    """

    kind: ProblemKind
    token: str
    text: str


@dataclass(frozen=True, slots=True)
class Ambiguity:
    """Прочтений ровно два, и выбрать за оператора нельзя.

    Единственный случай — десять цифр, начинающихся с девятки, одним слитным
    токеном. Записать их телефоном значит потерять паспорт, то есть закрыть
    единственный вход в мост «паспорт → ИНН»; записать паспортом — отправить
    мобильный в ФНС. Вопрос дешевле любой из двух ошибок.
    """

    token: str


@dataclass(frozen=True, slots=True)
class ParsedQuery:
    """Всё, что удалось понять из одной строки.

    Пустой экземпляр — законный результат («ни ФИО, ни ИНН, ни госномера»), а не
    признак сбоя.
    """

    name: PersonName | None = None
    #: Текст :class:`NameParseError`, а не исключение: разбор строки не падает.
    name_error: str | None = None
    birth_date: date | None = None
    inn: str | None = None
    passport: str | None = None
    phone: str | None = None
    plate: str | None = None
    vin: str | None = None
    #: Заполнено — значит нужен вопрос ДО запуска проверки.
    ambiguity: Ambiguity | None = None
    problems: tuple[Problem, ...] = ()
    #: Слова, которые не сложились в ФИО. Ничего не теряем молча.
    leftover: tuple[str, ...] = ()

    @property
    def has_subject(self) -> bool:
        """Есть ли вообще по чему запускать проверку."""
        return any((self.name, self.inn, self.passport, self.phone, self.plate, self.vin))

    def problem(self, kind: ProblemKind) -> Problem | None:
        return next((item for item in self.problems if item.kind is kind), None)


class TenDigits(StrEnum):
    """Ответ оператора на :class:`Ambiguity`."""

    PASSPORT = "passport"
    PHONE = "phone"


# ---------------------------------------------------------------- шаблоны

#: Знаки, которые оператор ставит между полями и которые ничего не значат.
#: Точка, дефис, плюс и скобки НЕ здесь: они держат дату и телефон.
_NOISE = re.compile(r"[,;|:№«»\"\t]+")

#: «01.01.1985 г.р.» — оператор копирует строку из выгрузки целиком.
#:
#: Шаблон берётся у :data:`app.domain.identity.BIRTH_DATE_MARKER`, чтобы две
#: копии одного правила не разошлись, и дополняется двумя вещами. ``IGNORECASE``
#: — потому что там он применяется к уже приведённой к нижнему регистру строке,
#: а сюда приходит то, что набрал человек. ``(?!\w)`` — потому что без него
#: «01.01.1985 Гришин» теряло бы «Гр» из фамилии: лексема «г.р.» разрешает
#: пробел между буквами, и «Гр» после цифр читается ею как маркер.
_BIRTH_MARKER = re.compile(f"(?:{BIRTH_DATE_MARKER.pattern})(?!\\w)", re.IGNORECASE)

#: Токен формы даты. Разбирается только целым словом: внутри «+7-916-000-00-00»
#: тоже найдётся «цифры-цифры-цифры», и вырезать оттуда «дату» — верный способ
#: развалить телефон.
_DATE_SHAPE = re.compile(r"\d{1,2}[.\-/]\d{1,2}[.\-/]\d{4}|\d{4}-\d{2}-\d{2}|\d{8}")

#: Из числового токена выбрасываются только разделители записи.
_NUMERIC_NOISE = str.maketrans("", "", "+()-. ")

#: Признаки телефонной записи внутри группы цифр.
_PHONE_MARKS = re.compile(r"[+()]")

#: Разбивки десяти цифр, однозначно читающиеся телефоном («916 123 45 67»).
_PHONE_GROUPINGS: frozenset[tuple[int, ...]] = frozenset(
    {(3, 3, 2, 2), (3, 2, 2, 3), (3, 3, 4), (3, 7), (3, 4, 3)}
)

#: Разбивка десяти цифр, однозначно читающаяся паспортом: серия и номер.
_PASSPORT_GROUPING: tuple[int, ...] = (4, 6)


class _Label(StrEnum):
    """Оператор назвал тип идентификатора словом — гадать больше не нужно."""

    INN = "inn"
    PASSPORT = "passport"
    PHONE = "phone"


_LABEL_PATTERNS: tuple[tuple[_Label, re.Pattern[str]], ...] = (
    (_Label.INN, re.compile(r"инн", re.IGNORECASE)),
    (_Label.PASSPORT, re.compile(r"паспорт\w*|сери[яи]", re.IGNORECASE)),
    (_Label.PHONE, re.compile(r"тел\w*|моб\w*|phone", re.IGNORECASE)),
)

_LABEL_TITLES: dict[_Label, str] = {
    _Label.INN: "ИНН",
    _Label.PASSPORT: "паспорт",
    _Label.PHONE: "телефон",
}


class _Kind(StrEnum):
    DATE = "date"
    LABEL = "label"
    DIGITS = "digits"
    VIN = "vin"
    PLATE = "plate"
    WORD = "word"


@dataclass(slots=True)
class _Item:
    """Один токен строки и то, чем он оказался."""

    raw: str
    kind: _Kind
    #: Цифры без разделителей — для ``DIGITS``.
    digits: str = ""
    label: _Label | None = None
    #: Нормализованное значение — для ``VIN`` и ``PLATE``.
    value: str = ""
    taken: bool = False


@dataclass(slots=True)
class _Draft:
    """Черновик результата: поля заполняются по мере разбора."""

    birth_date: date | None = None
    inn: str | None = None
    passport: str | None = None
    phone: str | None = None
    plate: str | None = None
    vin: str | None = None
    ambiguity: Ambiguity | None = None
    problems: list[Problem] = field(default_factory=list)


# ---------------------------------------------------------------- разбор


def parse_query(raw: str | None, *, ten_digits_as: TenDigits | None = None) -> ParsedQuery:
    """Разобрать строку оператора. Никогда не бросает исключений.

    ``ten_digits_as`` — ответ на заданный ранее вопрос про десять цифр с
    девятки. Строка разбирается заново целиком, а не «дописывается»: держать
    полуразобранное состояние между сообщениями дороже и хрупче, чем перечитать
    ту же строку.
    """
    text = _normalize(raw)
    if not text:
        return ParsedQuery()

    draft = _Draft()
    stream = [_classify_token(token) for token in text.split()]
    _take_dates(stream, draft)
    _take_numbers(stream, draft, ten_digits_as=ten_digits_as)
    name, name_error, leftover = _take_name(stream)

    return ParsedQuery(
        name=name,
        name_error=name_error,
        birth_date=draft.birth_date,
        inn=draft.inn,
        passport=draft.passport,
        phone=draft.phone,
        plate=draft.plate,
        vin=draft.vin,
        ambiguity=draft.ambiguity,
        problems=tuple(draft.problems),
        leftover=leftover,
    )


def _normalize(raw: str | None) -> str:
    # Неразрывный пробел приезжает из выгрузок и с мобильной клавиатуры.
    text = (raw or "").replace(" ", " ")
    text = _BIRTH_MARKER.sub(" ", text)
    text = _NOISE.sub(" ", text)
    return " ".join(text.split())


def _classify_token(token: str) -> _Item:
    """Порядок веток — правило разрешения совпадений, и он не случаен.

    Дата проверяется первой: «01.01.1985» иначе стало бы «числовым токеном».
    Метка — до цифр, чтобы «инн» не ушло в ФИО. Цифры — до VIN и госномера,
    потому что и то и другое содержит буквы, а чистые цифры не бывают ни тем,
    ни другим.
    """
    if _DATE_SHAPE.fullmatch(token.rstrip(".")):
        return _Item(raw=token, kind=_Kind.DATE)

    for label, pattern in _LABEL_PATTERNS:
        if pattern.fullmatch(token):
            return _Item(raw=token, kind=_Kind.LABEL, label=label)

    digits = _digits_of(token)
    if digits is not None:
        return _Item(raw=token, kind=_Kind.DIGITS, digits=digits)

    vin = normalize_vin(token)
    if vin is not None:
        return _Item(raw=token, kind=_Kind.VIN, value=vin)

    plate = normalize_plate(token)
    if plate is not None:
        return _Item(raw=token, kind=_Kind.PLATE, value=plate)

    return _Item(raw=token, kind=_Kind.WORD)


def _digits_of(token: str) -> str | None:
    cleaned = token.translate(_NUMERIC_NOISE)
    if not cleaned or not cleaned.isascii() or not cleaned.isdigit():
        return None
    return cleaned


# ---------------------------------------------------------------- дата


def _take_dates(stream: list[_Item], draft: _Draft) -> None:
    """Первая разобравшаяся дата — рождение. Неразобравшаяся — оговорка.

    Молча проглотить «15.13.1985» нельзя: без даты рождения ФССП и залоги не
    ищут вовсе, и отчёт вышел бы с двумя пустыми разделами по вине опечатки,
    которую оператор считает исправленной.
    """
    for item in stream:
        if item.kind is not _Kind.DATE:
            continue
        item.taken = True
        parsed = parse_date(item.raw.rstrip("."))
        if parsed is None:
            draft.problems.append(
                Problem(
                    kind=ProblemKind.BAD_DATE,
                    token=item.raw,
                    text=f"«{item.raw}» на дату не похоже — {describe_bad_date(item.raw)}",
                )
            )
        elif draft.birth_date is None:
            draft.birth_date = parsed


def describe_bad_date(token: str) -> str:
    """Почему именно эта дата не годится.

    «Не разобрал формат» на «01.01.2030» — бесполезный ответ: формат как раз
    разобран, не годится год. Причина называется, потому что от неё зависит,
    что оператору исправлять.
    """
    parts = _date_parts(token.strip().rstrip("."))
    if parts is None:
        return "не разобрал формат"
    year, month, day = parts
    if year < MIN_PLAUSIBLE_YEAR:
        return f"год раньше {MIN_PLAUSIBLE_YEAR}"
    try:
        value = date(year, month, day)
    except ValueError:
        return "такого месяца или числа не бывает"
    today = utcnow().date()
    if value > today:
        return "дата в будущем"
    if today.year - value.year > MAX_PLAUSIBLE_AGE_YEARS:
        return f"по ней должнику больше {MAX_PLAUSIBLE_AGE_YEARS} лет"
    return "не разобрал формат"


def _date_parts(text: str) -> tuple[int, int, int] | None:
    """(год, месяц, день) из токена любой из принимаемых форм."""
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if iso:
        return int(iso.group(1)), int(iso.group(2)), int(iso.group(3))
    ru = re.fullmatch(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
    if ru:
        return int(ru.group(3)), int(ru.group(2)), int(ru.group(1))
    if re.fullmatch(r"\d{8}", text):
        return int(text[4:]), int(text[2:4]), int(text[:2])
    return None


# ---------------------------------------------------------------- цифры


def _take_numbers(stream: list[_Item], draft: _Draft, *, ten_digits_as: TenDigits | None) -> None:
    """Разложить группы цифр по полям.

    Соседние числовые токены сначала пробуются как одно число: «4515 384710» —
    это один паспорт, а не две непонятных группы. Если склейка ни на что не
    похожа, группа разбирается по одному токену: «770912345601 4515384710» —
    это ИНН и паспорт, а не двадцать две цифры ниоткуда.
    """
    index = 0
    while index < len(stream):
        if stream[index].kind is not _Kind.DIGITS:
            index += 1
            continue
        end = index
        while end < len(stream) and stream[end].kind is _Kind.DIGITS:
            end += 1
        run = stream[index:end]
        _take_run(run, draft, label=_label_before(stream, index), ten_digits_as=ten_digits_as)
        for entry in run:
            entry.taken = True
        index = end

    for item in stream:
        if item.kind is _Kind.VIN and draft.vin is None:
            draft.vin = item.value
            item.taken = True
        elif item.kind is _Kind.PLATE and draft.plate is None:
            draft.plate = item.value
            item.taken = True


def _label_before(stream: list[_Item], index: int) -> _Label | None:
    """Метка действует только на ближайшую следующую группу цифр.

    Поэтому «инн 770912345601 паспорт 4515384710» разбирается, а не отдаёт весь
    хвост строки под первое же слово.
    """
    if index == 0:
        return None
    previous = stream[index - 1]
    if previous.kind is _Kind.LABEL:
        previous.taken = True
        return previous.label
    return None


def _take_run(
    run: list[_Item], draft: _Draft, *, label: _Label | None, ten_digits_as: TenDigits | None
) -> None:
    joined = "".join(item.digits for item in run)
    shown = " ".join(item.raw for item in run)

    if label is not None:
        _take_labelled(joined, shown, draft, label=label)
        return
    if _assign(joined, draft):
        return
    if _take_ten_from_nine(run, joined, shown, draft, ten_digits_as=ten_digits_as):
        return
    if len(run) > 1:
        # Склейка ни на что не похожа — значит это несколько идентификаторов
        # подряд, а не одно длинное число.
        for item in run:
            _take_run([item], draft, label=None, ten_digits_as=ten_digits_as)
        return

    draft.problems.append(
        Problem(
            kind=ProblemKind.UNKNOWN_DIGITS,
            token=shown,
            text=(
                f"«{shown}» — не похоже ни на ИНН физлица ({INN_INDIVIDUAL_LENGTH} цифр), "
                f"ни на паспорт ({PASSPORT_LENGTH}), ни на телефон. Проверяю без него."
            ),
        )
    )


def _assign(digits: str, draft: _Draft) -> bool:
    """Однозначные длины. Десять цифр с девятки сюда не попадают намеренно."""
    if len(digits) == INN_INDIVIDUAL_LENGTH:
        draft.inn = draft.inn or digits
        return True
    if len(digits) == PHONE_LENGTH and digits[0] in {"7", "8"}:
        draft.phone = draft.phone or f"+7{digits[1:]}"
        return True
    if len(digits) == PASSPORT_LENGTH and not digits.startswith("9"):
        draft.passport = draft.passport or digits
        return True
    return False


def _take_ten_from_nine(
    run: list[_Item],
    digits: str,
    shown: str,
    draft: _Draft,
    *,
    ten_digits_as: TenDigits | None,
) -> bool:
    """Десять цифр с девятки: серия паспорта 9xxx или мобильный без «+7».

    Форма записи решает там, где она однозначна, — «4515 384710» это серия и
    номер, «(916) 000-00-00» это телефон. Слитные десять цифр не решает ничто, и
    здесь бот спрашивает вместо того, чтобы угадать.
    """
    if len(digits) != PASSPORT_LENGTH or not digits.startswith("9"):
        return False

    if ten_digits_as is TenDigits.PASSPORT:
        draft.passport = draft.passport or digits
        return True
    if ten_digits_as is TenDigits.PHONE:
        draft.phone = draft.phone or normalize_phone(digits)
        return True

    shape = tuple(len(item.digits) for item in run)
    if shape == _PASSPORT_GROUPING:
        draft.passport = draft.passport or digits
        return True
    if shape in _PHONE_GROUPINGS or _PHONE_MARKS.search(shown):
        draft.phone = draft.phone or normalize_phone(digits)
        return True

    draft.ambiguity = draft.ambiguity or Ambiguity(token=shown)
    return True


def _take_labelled(digits: str, shown: str, draft: _Draft, *, label: _Label) -> None:
    """Оператор назвал тип сам — переклассифицировать его ввод молча нельзя."""
    if label is _Label.INN:
        if len(digits) == INN_INDIVIDUAL_LENGTH:
            draft.inn = draft.inn or digits
            return
        if len(digits) == INN_ENTITY_LENGTH:
            draft.problems.append(
                Problem(
                    kind=ProblemKind.ENTITY_INN,
                    token=shown,
                    text=(
                        f"{shown} — это ИНН организации, для человека нужны "
                        f"{INN_INDIVIDUAL_LENGTH} цифр. Проверяю без него."
                    ),
                )
            )
            return
    elif label is _Label.PASSPORT:
        if len(digits) == PASSPORT_LENGTH:
            draft.passport = draft.passport or digits
            return
    elif (phone := normalize_phone(digits)) is not None:
        draft.phone = draft.phone or phone
        return

    draft.problems.append(
        Problem(
            kind=ProblemKind.UNKNOWN_DIGITS,
            token=shown,
            text=f"«{shown}» под меткой «{_LABEL_TITLES[label]}» не разобрал. Проверяю без него.",
        )
    )


# ---------------------------------------------------------------- имя


def _take_name(stream: list[_Item]) -> tuple[PersonName | None, str | None, tuple[str, ...]]:
    """Всё, что осталось буквами, — ФИО.

    Пустой остаток ошибкой не является: «ИНН 770912345601» — законный запрос, по
    двенадцати цифрам ищут банкротство, статус ИП и арбитраж, и требовать к ним
    ещё и фамилию значило бы вернуть тот самый допрос.
    """
    words = [item.raw for item in stream if item.kind is _Kind.WORD and not item.taken]
    if not words:
        return None, None, ()
    try:
        return parse_fio(" ".join(words)), None, ()
    except NameParseError as exc:
        return None, str(exc), tuple(words)


__all__ = [
    "Ambiguity",
    "ParsedQuery",
    "Problem",
    "ProblemKind",
    "TenDigits",
    "describe_bad_date",
    "parse_query",
]
