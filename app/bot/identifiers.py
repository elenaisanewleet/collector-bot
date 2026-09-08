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
    capitalize_name,
    is_name_word,
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
    def runnable(self) -> bool:
        """Есть ли вообще по чему запускать проверку.

        Заменяет прежний ``has_subject``, и разница в двух полях, каждое из
        которых там было ошибкой.

        **Телефона здесь нет.** Ни один внешний реестр по телефону не ищет:
        он находит запись в нашей собственной базе и укрепляет сопоставление,
        не более. Прежний ``has_subject`` считал одинокий ``+79990001122``
        достаточным поводом для полного платного прогона, и оператор получал
        шапку ``👤 —`` и пять строк «нужно ФИО» за деньги.

        **Паспорта здесь тоже нет.** Сам по себе он не открывает ничего: мост
        ``passport_fns`` требует ещё ФИО и дату рождения и без них отвечает
        ``insufficient_query``, не сделав ни одного вызова.
        """
        return any((self.name, self.inn, self.plate, self.vin))

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

#: Разбивки трёх числовых токенов, читающиеся датой: «24 11 1994», «1 5 1980».
#:
#: Условие по форме групп обязательно. Без него склейка трёх любых чисел
#: пробовалась бы как дата и съедала бы чужие числа — «4515 38 4710» стало бы
#: датой вместо паспорта. Год только четырёхзначный: двузначный век мы не
#: угадываем (см. :func:`describe_bad_date`).
_DATE_RUN_SHAPES: frozenset[tuple[int, ...]] = frozenset(
    {(2, 2, 4), (1, 2, 4), (2, 1, 4), (1, 1, 4)}
)

#: День, месяц, год — ровно три группы, не две и не четыре.
_DATE_RUN_TOKENS = 3


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
    if _take_spaced_date(run, draft):
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


def _take_spaced_date(run: list[_Item], draft: _Draft) -> bool:
    """«24 11 1994» — дата, набранная пробелами.

    Дословный пример владелицы, и до этой ветки он давал три оговорки
    «не похоже ни на ИНН, ни на паспорт, ни на телефон» подряд. Точку и дефис
    :data:`_DATE_SHAPE` ловит целым словом, пробел — нет: три числа приезжают
    тремя токенами и до разбора дат не доходят вовсе.

    Условий три, и каждое сужает ветку до безопасной. Ровно три группы цифр —
    иначе «01 01 1985 770912345601» читалось бы как одна длинная дата. Форма
    групп из :data:`_DATE_RUN_SHAPES` — иначе паспорт «4515 38 4710» стал бы
    датой. Дата рождения ещё не занята — первая разобравшаяся дата остаётся
    главной, как и в :func:`_take_dates`.
    """
    if draft.birth_date is not None or len(run) != _DATE_RUN_TOKENS:
        return False
    shape = tuple(len(item.digits) for item in run)
    if shape not in _DATE_RUN_SHAPES:
        return False
    day, month, year = (item.digits for item in run)
    parsed = parse_date(f"{int(day):02d}.{int(month):02d}.{year}")
    if parsed is None:
        return False
    draft.birth_date = parsed
    return True


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


# ---------------------------------------------------------------- фрагмент
#
# Всё, что выше, разбирает СТРОКУ — законченное описание должника. Ниже —
# разбор ФРАГМЕНТА: одного присланного сообщения, которое дописывается в уже
# существующую карточку. Разница не в алгоритме, а в вопросе. Строка отвечает
# на «кого проверяем», фрагмент — на «что это за одно значение и в какое поле
# оно ложится», и у фрагмента есть подсказка: поле, кнопку которого нажали.


class FragmentKind(StrEnum):
    """Чем оказался присланный кусок."""

    DATE = "date"
    PHONE = "phone"
    #: ИНН физлица, двенадцать цифр. Единственный, который годится источникам.
    INN12 = "inn12"
    #: Десять цифр под меткой «ИНН» — организация, человека по нему не ищут.
    INN10 = "inn10"
    PASSPORT = "passport"
    #: Десять цифр с девятки слитно: паспорт или телефон, решить нельзя.
    AMBIGUOUS_TEN = "ambiguous_ten"
    PLATE = "plate"
    VIN = "vin"
    #: Фамилия, имя и (иногда) отчество разом — разобрал :func:`parse_fio`.
    FIO = "fio"
    #: Одно слово буквами. В какое поле оно ложится, решает карточка.
    NAME_WORD = "name_word"
    #: Несколько слов буквами в ответ на вопрос про КОНКРЕТНЫЙ слот имени.
    #: «Елена Николаевна» на вопрос «Имя» — это имя с отчеством, а на вопрос
    #: «Фамилия» — фамилия с именем. Разложить их может только тот, кто знает
    #: вопрос, поэтому слова едут в карточку списком, а не разобранным ФИО.
    NAME_PARTS = "name_parts"
    #: Номер договора или адрес — то, что ищется только в нашей выгрузке.
    TEXT = "text"
    #: Похоже на дату, но датой не является.
    BAD_DATE = "bad_date"
    #: Прислали, но не разобрали.
    UNKNOWN = "unknown"
    #: Не прислали ничего — пробелы.
    EMPTY = "empty"


class Field(StrEnum):
    """Поле карточки, которое ждут после нажатия кнопки.

    Значения короткие и ASCII: они уезжают и в ``callback_data`` (лимит 64
    байта), и в колонку ``query_cards.awaiting_field``.
    """

    FIO = "fio"
    BIRTH_DATE = "birth_date"
    INN = "inn"
    PHONE = "phone"
    PASSPORT = "passport"
    #: Госномер или VIN одной кнопкой: оператор не обязан знать, чем они
    #: отличаются, а форма записи различает их сама.
    AUTO = "auto"
    #: Фамилия и имя по отдельности. Нужны ведомому сценарию, который спрашивает
    #: их разными шагами: «Елена Николаевна» в ответ на «Имя» — это имя с
    #: отчеством, а тот же текст в ответ на «Фамилия» — фамилия с именем.
    #: :data:`FIO` эту разницу передать не может, он про все три слота разом.
    LAST_NAME = "last"
    FIRST_NAME = "first"
    #: Отчество отдельной кнопкой: в основные шаги оно не входит («это опция»),
    #: но однофамильцев различает лучше всего.
    MIDDLE_NAME = "middle"
    #: Номер договора и адрес. Ни один внешний реестр по ним не ищет — они
    #: поднимают строку из выгрузки 1С, то есть работают на главный источник.
    CONTRACT = "contract"
    ADDRESS = "address"


@dataclass(frozen=True, slots=True)
class Fragment:
    """Разобранный кусок сообщения.

    Значение лежит в поле своего типа, а не в общем ``value: Any``: карточка
    кладёт его в колонку, у колонки есть тип, и «строка, которая иногда дата»
    развалилась бы на первом же ``mypy``.
    """

    kind: FragmentKind
    #: То, что прислал человек, — обрезанное до :data:`ECHO_LIMIT`.
    shown: str = ""
    date: date | None = None
    digits: str = ""
    phone: str | None = None
    name: PersonName | None = None
    word: str = ""
    #: Слова имени по порядку — для :attr:`FragmentKind.NAME_PARTS`.
    words: tuple[str, ...] = ()
    #: Значение поля, которое хранится как есть: номер договора, адрес.
    text: str = ""
    plate: str | None = None
    vin: str | None = None
    #: Почему не разобрали. Готовая фраза, как у :class:`Problem`.
    reason: str = ""


#: Сколько символов чужого ввода бот повторяет обратно. Оператор способен
#: вставить в чат весь абзац из 1С, а лимит сообщения Telegram — 4096.
ECHO_LIMIT = 32

#: Слово из русских букв. Латиница сюда не входит намеренно: «asdf» проходит
#: :data:`app.domain.identity._NAME_ALLOWED` и без этого условия молча уехало
#: бы в фамилию.
_CYRILLIC_WORD = re.compile(r"^[а-яё]+(?:-[а-яё]+)*$", re.IGNORECASE)

#: Окончания отчеств. При пустых слотах слово с таким хвостом — отчество, а не
#: фамилия: «Николаевна» в графе «Фамилия» это заметная глазом ошибка.
_PATRONYMIC_SUFFIXES = ("овна", "евна", "ична", "инична", "ович", "евич", "ьич")


def echo(text: str) -> str:
    """Чужой ввод, безопасный для повторения в сообщении."""
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= ECHO_LIMIT:
        return collapsed
    return collapsed[:ECHO_LIMIT] + "…"


def looks_like_patronymic(word: str) -> bool:
    return word.casefold().endswith(_PATRONYMIC_SUFFIXES)


def looks_like_russian_name(word: str) -> bool:
    """Русское слово, которое можно молча положить в слот ФИО.

    Требование кириллицы здесь не про язык, а про разницу между «Иванова» и
    «asdf»: оба состоят из букв, оба проходят алфавит
    :func:`app.domain.identity.is_name_word`, и без этого условия мусор молча
    уезжал бы в фамилию. Там, где поле названо кнопкой, требование снимается —
    оператор уже сказал, что это имя, и спорить не с чем.
    """
    return bool(_CYRILLIC_WORD.match(word))


def classify_fragment(text: str | None, *, expect: Field | None = None) -> Fragment:
    """Чем является одно присланное сообщение.

    ``expect`` — поле, кнопку которого нажали. Когда оно задано, гадать не
    нужно и нельзя: «Иванова» в ответ на «+ ИНН» — это не фамилия, которую
    надо тихо положить в другое поле, а ошибка, о которой надо сказать. Когда
    оно не задано, форма записи решает сама, а неразрешимое (десять цифр с
    девятки) приезжает :attr:`FragmentKind.AMBIGUOUS_TEN` и стоит одного
    вопроса.

    Никогда не бросает — по той же причине, что и :func:`parse_query`.
    """
    shown = echo(text or "")
    if not (text or "").strip():
        return Fragment(kind=FragmentKind.EMPTY)
    if expect is not None:
        return _expected(text or "", shown, expect)
    return _guessed(text or "", shown)


def _expected(text: str, shown: str, expect: Field) -> Fragment:
    """Прочитать текст как названное поле. Ответ «не оно» — тоже ответ."""
    match expect:
        case Field.BIRTH_DATE:
            return _as_date(text, shown)
        case Field.INN:
            return _as_inn(text, shown)
        case Field.PHONE:
            return _as_phone(text, shown)
        case Field.PASSPORT:
            return _as_passport(text, shown)
        case Field.AUTO:
            return _as_auto(text, shown)
        case Field.FIO:
            return _as_name(text, shown)
        case Field.LAST_NAME | Field.FIRST_NAME | Field.MIDDLE_NAME:
            return _as_name_parts(text, shown)
        case Field.CONTRACT | Field.ADDRESS:
            return Fragment(kind=FragmentKind.TEXT, shown=shown, text=" ".join(text.split()))


def _as_date(text: str, shown: str) -> Fragment:
    parsed = _any_date(text)
    if parsed is not None:
        return Fragment(kind=FragmentKind.DATE, shown=shown, date=parsed)
    return Fragment(
        kind=FragmentKind.BAD_DATE,
        shown=shown,
        reason=f"«{shown}» на дату не похоже — {describe_bad_date(text.strip())}.",
    )


def _any_date(text: str) -> date | None:
    """Дата в любой принимаемой форме, включая набранную пробелами."""
    stripped = _normalize(text)
    direct = parse_date(stripped.rstrip("."))
    if direct is not None:
        return direct
    draft = _Draft()
    stream = [_classify_token(token) for token in stripped.split()]
    if all(item.kind is _Kind.DIGITS for item in stream):
        _take_spaced_date(stream, draft)
    return draft.birth_date


def _as_inn(text: str, shown: str) -> Fragment:
    digits = _only_digits(text)
    if len(digits) == INN_INDIVIDUAL_LENGTH:
        return Fragment(kind=FragmentKind.INN12, shown=shown, digits=digits)
    if len(digits) == INN_ENTITY_LENGTH:
        return Fragment(kind=FragmentKind.INN10, shown=shown, digits=digits, reason=_ENTITY_INN)
    return Fragment(
        kind=FragmentKind.UNKNOWN,
        shown=shown,
        reason=f"«{shown}» — на ИНН физлица не похоже, нужны {INN_INDIVIDUAL_LENGTH} цифр.",
    )


def _as_phone(text: str, shown: str) -> Fragment:
    phone = normalize_phone(text)
    if phone is not None:
        return Fragment(kind=FragmentKind.PHONE, shown=shown, phone=phone)
    return Fragment(
        kind=FragmentKind.UNKNOWN,
        shown=shown,
        reason=f"«{shown}» — на телефон не похоже. Пример: +7 916 000 00 00.",
    )


def _as_passport(text: str, shown: str) -> Fragment:
    digits = _only_digits(text)
    if len(digits) == PASSPORT_LENGTH:
        return Fragment(kind=FragmentKind.PASSPORT, shown=shown, digits=digits)
    return Fragment(
        kind=FragmentKind.UNKNOWN,
        shown=shown,
        reason=f"Нужно ровно {PASSPORT_LENGTH} цифр — серия и номер.",
    )


def _as_auto(text: str, shown: str) -> Fragment:
    vin = normalize_vin(text)
    if vin is not None:
        return Fragment(kind=FragmentKind.VIN, shown=shown, vin=vin)
    plate = normalize_plate(text)
    if plate is not None:
        return Fragment(kind=FragmentKind.PLATE, shown=shown, plate=plate)
    return Fragment(
        kind=FragmentKind.UNKNOWN,
        shown=shown,
        reason=f"«{shown}» — не госномер и не VIN. Пример: О123АА777.",
    )


def _as_name(text: str, shown: str) -> Fragment:
    """ФИО целиком или одно слово из него.

    :func:`parse_fio` не ослабляется: неверное разбиение имени молча отравляет
    всякое последующее сопоставление. Одно слово он законно отвергает — и это
    ровно тот случай, ради которого заведён :attr:`FragmentKind.NAME_WORD`:
    решение, в какой слот его положить, принимает карточка, у которой видно,
    какие слоты пусты.
    """
    words = [word for word in _normalize(text).split() if word]
    if len(words) == 1 and is_name_word(words[0]):
        return Fragment(kind=FragmentKind.NAME_WORD, shown=shown, word=capitalize_name(words[0]))
    try:
        return Fragment(kind=FragmentKind.FIO, shown=shown, name=parse_fio(" ".join(words)))
    except NameParseError as exc:
        return Fragment(kind=FragmentKind.UNKNOWN, shown=shown, reason=str(exc))


def _as_name_parts(text: str, shown: str) -> Fragment:
    """Ответ на вопрос про один слот имени: «Фамилия», «Имя», «Отчество».

    От :func:`_as_name` отличается тем, что НЕ зовёт :func:`parse_fio`, и это
    принципиально. ``parse_fio`` раскладывает слова по своему порядку —
    фамилия, имя, отчество, — а здесь порядок задан вопросом: «Елена
    Николаевна» на шаге «Имя» это имя с отчеством, и разобрать её фамилией
    значило бы переспросить оператора о том, на что он только что ответил.
    Куда лягут слова, решает карточка, знающая вопрос; сюда приезжает список.
    """
    words = [word for word in _normalize(text).split() if word]
    if not words:
        return Fragment(kind=FragmentKind.EMPTY, shown=shown)
    if not all(is_name_word(word) for word in words):
        return Fragment(
            kind=FragmentKind.UNKNOWN,
            shown=shown,
            reason=f"«{shown}» на часть имени не похоже — жду одно слово буквами.",
        )
    if len(words) > _NAME_SLOT_COUNT:
        return Fragment(kind=FragmentKind.UNKNOWN, shown=shown, reason=TOO_MANY_NAME_WORDS)
    capitalized = tuple(capitalize_name(word) for word in words)
    if len(capitalized) == 1:
        return Fragment(kind=FragmentKind.NAME_WORD, shown=shown, word=capitalized[0])
    return Fragment(kind=FragmentKind.NAME_PARTS, shown=shown, words=capitalized)


def _guessed(text: str, shown: str) -> Fragment:
    """Кнопку не нажимали — решает форма записи.

    Порядок веток тот же, что в :func:`parse_query`, и по тем же причинам;
    сам разбор тоже его, чтобы два места не разъехались.
    """
    parsed = parse_query(text)
    if parsed.ambiguity is not None:
        return Fragment(
            kind=FragmentKind.AMBIGUOUS_TEN,
            shown=shown,
            digits=_only_digits(parsed.ambiguity.token),
        )
    if parsed.birth_date is not None:
        return Fragment(kind=FragmentKind.DATE, shown=shown, date=parsed.birth_date)
    if parsed.inn is not None:
        return Fragment(kind=FragmentKind.INN12, shown=shown, digits=parsed.inn)
    if parsed.passport is not None:
        return Fragment(kind=FragmentKind.PASSPORT, shown=shown, digits=parsed.passport)
    if parsed.phone is not None:
        return Fragment(kind=FragmentKind.PHONE, shown=shown, phone=parsed.phone)
    if parsed.vin is not None:
        return Fragment(kind=FragmentKind.VIN, shown=shown, vin=parsed.vin)
    if parsed.plate is not None:
        return Fragment(kind=FragmentKind.PLATE, shown=shown, plate=parsed.plate)
    if parsed.name is not None:
        return Fragment(kind=FragmentKind.FIO, shown=shown, name=parsed.name)

    entity_inn = parsed.problem(ProblemKind.ENTITY_INN)
    if entity_inn is not None:
        return Fragment(
            kind=FragmentKind.INN10,
            shown=shown,
            digits=_only_digits(entity_inn.token),
            reason=_ENTITY_INN,
        )
    bad_date = parsed.problem(ProblemKind.BAD_DATE)
    if bad_date is not None:
        return Fragment(kind=FragmentKind.BAD_DATE, shown=shown, reason=f"{bad_date.text}.")
    if _has_two_digit_year(text):
        return Fragment(
            kind=FragmentKind.BAD_DATE, shown=shown, reason=TWO_DIGIT_YEAR.format(shown)
        )
    return _guessed_word(parsed, shown)


def _has_two_digit_year(text: str) -> bool:
    """«24 11 94» — три группы цифр с коротким годом.

    Век не угадываем: «94» это и 1994, и 2094 у ребёнка, и 1894 в архивной
    выгрузке. Ошибка в веке стоит пустого ответа ФССП, который читается как
    «производств нет», — цена вопроса несопоставима с ценой двух символов.
    """
    groups = [_only_digits(token) for token in _normalize(text).split()]
    if len(groups) != _DATE_RUN_TOKENS or not all(groups):
        return False
    shape = tuple(len(group) for group in groups)
    return shape[:2] in {(1, 1), (1, 2), (2, 1), (2, 2)} and shape[2] == _SHORT_YEAR_DIGITS


def _guessed_word(parsed: ParsedQuery, shown: str) -> Fragment:
    """Осталось буквами. Одно русское слово — часть имени, всё прочее — мусор.

    Требование кириллицы здесь не про язык, а про разницу между «Иванова» и
    «asdf»: оба проходят :data:`app.domain.identity._NAME_ALLOWED`, оба
    состоят из букв, и без этого условия мусор молча уезжал бы в фамилию.
    Когда кнопку нажали, требование снимается (:func:`_as_name`) — там
    оператор сказал, что это имя, и спорить не с чем.
    """
    words = list(parsed.leftover)
    if len(words) == 1 and looks_like_russian_name(words[0]):
        return Fragment(kind=FragmentKind.NAME_WORD, shown=shown, word=capitalize_name(words[0]))
    if words and parsed.name_error and all(looks_like_russian_name(word) for word in words):
        return Fragment(kind=FragmentKind.UNKNOWN, shown=shown, reason=parsed.name_error)
    unknown_digits = parsed.problem(ProblemKind.UNKNOWN_DIGITS)
    if unknown_digits is not None:
        return Fragment(kind=FragmentKind.UNKNOWN, shown=shown, reason=unknown_digits.text)
    return Fragment(kind=FragmentKind.UNKNOWN, shown=shown, reason=NOT_UNDERSTOOD.format(shown))


#: Больше трёх слов в имени не бывает: фамилия, имя, отчество.
_NAME_SLOT_COUNT = 3

TOO_MANY_NAME_WORDS = (
    "Слишком много слов для имени. Ожидается: Фамилия Имя Отчество — или по одному слову за раз."
)

#: Цифра в ответе на вопрос про имя и русская буква в ответе на вопрос про
#: телефон. Оба — признак того, что оператор ответил не на вопрос, а прислал
#: строку целиком.
_A_DIGIT = re.compile(r"\d")
_A_RUSSIAN_LETTER = re.compile(r"[а-яё]", re.IGNORECASE)

#: Поля, вопрос о которых можно перебить строкой целиком.
_NAME_FIELDS = frozenset({Field.FIO, Field.LAST_NAME, Field.FIRST_NAME, Field.MIDDLE_NAME})


def spills_beyond(text: str | None, expect: Field) -> bool:
    """Прислали не ответ на вопрос, а всю строку про должника.

    Короткий путь, который нельзя ломать: он уже в проде, и оператор,
    вставляющий «Иванова Мария Сергеевна 05.07.1985 +79990001122» в ответ на
    «Фамилия», ждёт, что заполнится всё, а не что у него спросят, при чём тут
    цифры. Признак нарочно грубый и наблюдаемый глазом — цифра там, где ждали
    имя, и русская буква там, где ждали телефон. Тонкая эвристика здесь
    опаснее: она сработала бы неожиданно, а эта видна в самом вводе.

    Действует только на трёх ведомых шагах (см. :mod:`app.services.query_card`).
    У кнопок «+ ИНН» и «+ Паспорт» правило прежнее и обратное: названное поле
    читается как названное поле, иначе бот молча положит ответ не туда.
    """
    if not text:
        return False
    if expect in _NAME_FIELDS:
        return bool(_A_DIGIT.search(text))
    if expect is Field.PHONE:
        return bool(_A_RUSSIAN_LETTER.search(text))
    return False


_ENTITY_INN = f"Это ИНН организации, для человека нужны {INN_INDIVIDUAL_LENGTH} цифр."

_SHORT_YEAR_DIGITS = 2

TWO_DIGIT_YEAR = "«{}» — двузначный год не разбираю, век угадывать не буду. Напишите 05.07.1985."

NOT_UNDERSTOOD = (
    "«{}» не понял — на дату, телефон, ИНН, паспорт, госномер и VIN не похоже. "
    "Если это фамилия или имя — нажмите «Исправить ФИО»."
)


def _only_digits(text: str) -> str:
    return "".join(char for char in text if char.isdigit())


__all__ = [
    "ECHO_LIMIT",
    "TOO_MANY_NAME_WORDS",
    "Ambiguity",
    "Field",
    "Fragment",
    "FragmentKind",
    "ParsedQuery",
    "Problem",
    "ProblemKind",
    "TenDigits",
    "classify_fragment",
    "describe_bad_date",
    "echo",
    "looks_like_patronymic",
    "looks_like_russian_name",
    "parse_query",
    "spills_beyond",
]
