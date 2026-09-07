"""Parsing and validation of the internal debtor CSV export.

Real exports are messy: columns get renamed, encodings vary, amounts arrive with
currency symbols, and some rows are simply broken. The rules here are permissive
about shape and strict about identity — a row is accepted when it carries at
least a name or a contract number, and rejected otherwise, because a row with
neither cannot be matched to anything.

Второе правило такое же жёсткое: разбор шапки ничего не выбрасывает молча.
Колонка, которой нет в словаре синонимов, попадает в :class:`HeaderMapping` и
дальше в отчёт оператору. Иначе выгрузка с колонкой «ФИО должника» вместо «ФИО»
импортируется как «300 строк, 0 ошибок» — без имён, и разделы по человеку в
отчёте для суда окажутся пустыми, то есть «не проверено» прочитается «чисто».
"""

from __future__ import annotations

import csv
import io
import re
import unicodedata
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from app.domain.identity import (
    INN_INDIVIDUAL_LENGTH,
    NameParseError,
    normalize_address,
    normalize_inn,
    normalize_phone,
    normalize_plate,
    normalize_vin,
    parse_fio,
)
from app.utils.dates import parse_date
from app.utils.hashing import stable_hash
from app.utils.money import parse_amount

CANONICAL_COLUMNS = (
    "debtor_id",
    "fio",
    "birth_date",
    "phone",
    "inn",
    "contract_number",
    "claim_number",
    "debt_amount",
    "address",
    "vehicle_plate",
    "vin",
    "created_at",
    "source_id",
    "impounded_at",
    "released_at",
)

# Как поле зовут в разговоре с оператором. Внутреннее имя колонки в текст не
# уходит: «Нужна хотя бы одна из колонок: fio или contract_number» — это ответ
# программиста программисту, а получает его человек, который правит выгрузку в
# Excel и ищет там колонку с таким названием.
COLUMN_TITLES: dict[str, str] = {
    "debtor_id": "Код должника",
    "fio": "ФИО",
    "birth_date": "Дата рождения",
    "phone": "Телефон",
    "inn": "ИНН",
    "contract_number": "Номер договора",
    "claim_number": "Номер заявки",
    "debt_amount": "Сумма долга",
    "address": "Адрес",
    "vehicle_plate": "Госномер",
    "vin": "VIN",
    "created_at": "Дата создания",
    "source_id": "ИД записи",
    "impounded_at": "Дата постановки",
    "released_at": "Дата выдачи",
}

# Синонимы пишутся так, как их печатает 1С, — со словами, точками и «№».
# Ключи нормализуются один раз при импорте модуля (см. ``_ALIAS_INDEX``), поэтому
# «Гос. номер», «гос номер» и «ГосНомер» попадают в одну и ту же запись.
#
# Заказчик переименовывает колонки как хочет, и словарь всегда будет неполным.
# Поэтому он не единственная защита: всё нераспознанное перечисляется оператору
# в отчёте об импорте (см. :class:`HeaderMapping`). Раньше такая колонка просто
# исчезала, и выгрузка без распознанного ФИО импортировалась как «300 строк,
# 0 ошибок» — с пустыми разделами по человеку, которые читаются как «чисто».
COLUMN_ALIASES: dict[str, str] = {
    "id": "debtor_id",
    "debtorid": "debtor_id",
    "external_id": "debtor_id",
    "код": "debtor_id",
    "код 1С": "debtor_id",
    "код должника": "debtor_id",
    "код контрагента": "debtor_id",
    "идентификатор": "debtor_id",
    "fio": "fio",
    "name": "fio",
    "full_name": "fio",
    "фио": "fio",
    "ф.и.о.": "fio",
    "фио должника": "fio",
    "фио клиента": "fio",
    "фио контрагента": "fio",
    "фамилия имя отчество": "fio",
    "полное имя": "fio",
    "наименование должника": "fio",
    "наименование контрагента": "fio",
    # Выгрузка из 1С часто вообще не содержит слова «ФИО»: колонка называется
    # по роли человека в документе.
    "должник": "fio",
    "контрагент": "fio",
    "клиент": "fio",
    "плательщик": "fio",
    "ответчик": "fio",
    "birthdate": "birth_date",
    "dob": "birth_date",
    "birth": "birth_date",
    "дата рождения": "birth_date",
    "дата рожд.": "birth_date",
    "др": "birth_date",
    "phone": "phone",
    "tel": "phone",
    "телефон": "phone",
    "тел.": "phone",
    "номер телефона": "phone",
    "телефон номер": "phone",
    "контактный телефон": "phone",
    "мобильный телефон": "phone",
    "мобильный": "phone",
    "сотовый": "phone",
    # ИНН физлица. Ради него колонка и заводится: без него банкротство, статус
    # ИП и арбитраж не проверяются вовсе — эти источники ищут только по нему.
    # Выгрузка из 1С его обычно содержит, а импорт до сих пор молча выбрасывал.
    "inn": "inn",
    "инн": "inn",
    "innfiz": "inn",
    "инн должника": "inn",
    "инн физлица": "inn",
    "инн контрагента": "inn",
    "contract": "contract_number",
    "contract_no": "contract_number",
    "договор": "contract_number",
    "дог.": "contract_number",
    "номер договора": "contract_number",
    "договор номер": "contract_number",
    "контракт": "contract_number",
    "номер контракта": "contract_number",
    "claim": "claim_number",
    "заявка": "claim_number",
    "номер заявки": "claim_number",
    "заявка номер": "claim_number",
    "amount": "debt_amount",
    "debt": "debt_amount",
    "долг": "debt_amount",
    "sum": "debt_amount",
    "сумма долга": "debt_amount",
    "сумма задолженности": "debt_amount",
    "задолженность": "debt_amount",
    "остаток долга": "debt_amount",
    "остаток задолженности": "debt_amount",
    "текущий долг": "debt_amount",
    "долг руб.": "debt_amount",
    # Голая «Сумма» сюда намеренно не попадает: в выгрузке ею называют и оплату,
    # и начисление, и госпошлину. Молча взять чужую сумму как долг — попасть с
    # ней в отчёт для суда; не взять — увидеть «Сумма» в списке нераспознанных.
    "address": "address",
    "адрес": "address",
    "адрес регистрации": "address",
    # 1С печатает адрес с уточнением, какой именно он из нескольких.
    "актуальный адрес регистрации": "address",
    "адрес регистрации по месту жительства": "address",
    "адрес по прописке": "address",
    "адрес проживания": "address",
    "адрес должника": "address",
    "адрес фактический": "address",
    "фактический адрес": "address",
    "место жительства": "address",
    "plate": "vehicle_plate",
    "gosnomer": "vehicle_plate",
    "госномер": "vehicle_plate",
    "гос. номер": "vehicle_plate",
    "государственный номер": "vehicle_plate",
    "регистрационный знак": "vehicle_plate",
    "гос. рег. знак": "vehicle_plate",
    "грз": "vehicle_plate",
    "номер тс": "vehicle_plate",
    "номер автомобиля": "vehicle_plate",
    "vin": "vin",
    "вин": "vin",
    "vin номер": "vin",
    "номер vin": "vin",
    "vin код": "vin",
    # Номер записи в системе заказчика. Ключом дедупликации он намеренно НЕ
    # становится (для этого есть debtor_id): выгрузка эвакуатора — список
    # задержаний, и один человек стоит в ней до пяти раз с разными ИД. Взять его
    # ключом значило бы разбить 2052 должника обратно на 2631 эпизод и оплатить
    # 579 лишних проверок одних и тех же людей. А хранить его надо: это ссылка
    # на исходную запись и то, по чему приедут суммы, когда их выгрузят.
    # Две даты, из которых считается срок хранения, а из него — долг. В выгрузке
    # эвакуатора это единственный источник суммы: в учёте её нет, она считается
    # по тарифу.
    "дата постановки": "impounded_at",
    "дата помещения": "impounded_at",
    "дата задержания": "impounded_at",
    "дата эвакуации": "impounded_at",
    "дата выдачи": "released_at",
    "дата возврата": "released_at",
    "ид": "source_id",
    "ид записи": "source_id",
    "номер записи": "source_id",
    "номер эпизода": "source_id",
    "created": "created_at",
    "дата создания": "created_at",
    "дата записи": "created_at",
}

SUPPORTED_ENCODINGS = ("utf-8-sig", "utf-8", "cp1251")
SUPPORTED_DELIMITERS = ",;\t"
MAX_FIELD_LENGTH = 512

# «Без номера» в колонке госномера. Пишут кто во что горазд, поэтому сравнение
# идёт по буквам без разделителей: «б/н», «б\н», «б.н», «б...н», «бн», «н/у».
# Хвост после маркера («Б/Н МОПЕД», «Б/Н ЧЁРНАЯ») — описание машины, а не номер.
_NO_PLATE_MARKERS = frozenset({"бн", "ну", "безгрз", "безномера", "безнор", "нетномера"})


def _letters_only(raw: str) -> str:
    return re.sub(r"[^А-Яа-яЁёA-Za-z]", "", raw).casefold().replace("ё", "е")


def _means_no_plate(raw: str) -> bool:
    words = raw.split()
    if not words:
        return False
    # Целиком — ради «без ГРЗ» из двух слов; по первому слову — ради «Б/Н МОПЕД»,
    # где второе слово описывает машину, а не номер.
    return _letters_only(raw) in _NO_PLATE_MARKERS or _letters_only(words[0]) in _NO_PLATE_MARKERS


# Итоговая строка отчёта 1С в первой значимой колонке. Как должник она даёт
# лишнюю запись и завышает счётчик импортированных.
TOTALS_MARKERS = frozenset(
    {
        "итого",
        "итог",
        "всего",
        "итого по отчету",
        "всего по отчету",
        "общий итог",
        "итоговая строка",
    }
)

# 1С пишет шапку слитно («СуммаДолга», «ФИОДолжника») не реже, чем словами.
_CAMEL_BOUNDARIES = (
    re.compile(r"(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])"),
    re.compile(r"(?<=[A-ZА-ЯЁ])(?=[A-ZА-ЯЁ][a-zа-яё])"),
)
# Всё, что в заголовке служит оформлением, а не смыслом: точки в сокращениях,
# кавычки, скобки, дефисы, подчёркивания.
_HEADER_PUNCTUATION = re.compile(r"""[.,:;!?()\[\]{}«»"'`/\\|+\-_]+""")


class CsvFormatError(ValueError):
    """The file as a whole cannot be read as a debtor export."""


@dataclass(slots=True)
class DebtorRow:
    """One validated row, normalized and ready to store."""

    debtor_id: str | None = None
    full_name: str | None = None
    birth_date: date | None = None
    phone: str | None = None
    inn: str | None = None
    contract_number: str | None = None
    claim_number: str | None = None
    debt_amount: Decimal | None = None
    address: str | None = None
    vehicle_plate: str | None = None
    vin: str | None = None
    created_at: datetime | None = None
    warnings: list[str] = field(default_factory=list)
    #: Все машины должника, а не одна. У взыскателя-эвакуатора выгрузка — это
    #: список задержаний, и один человек приезжает в ней несколько раз с разными
    #: госномерами. Схлопывание таких строк в одного должника правильно (платная
    #: проверка человека нужна одна), но машины при этом терялись все, кроме
    #: последней, — а именно из них складывается требование.
    vehicle_plates: list[str] = field(default_factory=list)
    #: Номера записей в системе заказчика, из которых собран этот должник.
    #: Столько же, сколько эпизодов у человека.
    source_ids: list[str] = field(default_factory=list)
    #: Когда машину привезли на стоянку и когда забрали. Из них считается срок
    #: хранения, а из срока — долг по тарифу.
    impounded_at: datetime | None = None
    released_at: datetime | None = None
    #: Сумма посчитана по тарифу, а не взята из выгрузки. Признак едет до самого
    #: отчёта: расчётная сумма не имеет права выглядеть подтверждённой.
    debt_is_estimated: bool = False

    @property
    def dedup_key(self) -> str:
        """Deterministic identity for upserts.

        Лестница, а не один уровень, и порядок ступеней важнее их содержания.

        Код должника — ключ самого заказчика, спорить с ним не о чем. Номер
        договора — следующий по надёжности: он уникален у взыскателя и не
        меняется от того, что в выгрузку дописали телефон. Дата рождения
        различает однофамильцев. И только когда нет ничего из трёх, в ключ идут
        остальные опознавательные поля.

        Почему не свалить всё в один уровень. Так и было сделано сначала — ради
        однофамильцев, — и это сломало повторный импорт: та же выгрузка с
        дописанным телефоном давала вторую запись вместо обновления первой, хотя
        номер договора в обеих строках стоял один. Обещание модуля «повторный
        импорт обновляет, а не множит» держится ровно до тех пор, пока в ключ не
        попадают поля, которые заказчик дозаполняет со временем.

        Последняя ступень остаётся широкой сознательно. Когда о человеке известно
        одно ФИО, отличить «тот же, но с дописанным телефоном» от «полный тёзка»
        нечем в принципе, и выбор тут между видимой лишней строкой и невидимо
        слитыми людьми. Слитый должник уносит в суд чужие долги, поэтому цена
        лишней строки ниже — и о схлопывании оператору говорят отдельно.
        """
        if self.debtor_id:
            return stable_hash("id", self.debtor_id)
        birth = self.birth_date.isoformat() if self.birth_date else None
        if self.contract_number:
            return stable_hash("contract", self.full_name, birth, self.contract_number)
        if birth:
            return stable_hash("birth", self.full_name, birth)
        return stable_hash("composite", *self.identity_parts)

    @property
    def identity_parts(self) -> tuple[str | None, ...]:
        """Поля, по которым строка считается тем же должником."""
        return (
            self.full_name,
            self.birth_date.isoformat() if self.birth_date else None,
            self.contract_number,
            self.inn,
            self.phone,
            self.vehicle_plate,
            self.vin,
            self.address,
        )

    @property
    def comparable(self) -> tuple[object, ...]:
        """Содержательная часть строки — чтобы отличить повтор от разных строк.

        Долг и заявка в ключ не входят (одному человеку их можно дописать), но
        различие в них означает, что схлопнулись не копии, и об этом надо
        сказать оператору.
        """
        return (*self.identity_parts, self.debt_amount, self.claim_number)

    @property
    def personal_parts(self) -> tuple[str | None, ...]:
        """Кто это за человек — без того, что относится к отдельному случаю.

        Машина, VIN, сумма и номер заявки сюда не входят намеренно. У эвакуатора
        один и тот же должник встречается в выгрузке несколько раз, каждый раз с
        другой машиной, и это не повод кричать про однофамильцев: имя, дата
        рождения, ИНН, телефон и адрес совпали.
        """
        return (self.full_name, self.contract_number, self.inn, self.phone, self.address)

    @property
    def hard_identifiers(self) -> tuple[str | None, ...]:
        """Поля, расхождение в которых означает, что это разные люди.

        Адреса здесь нет намеренно, и это стоило отдельного разбора. На живой
        выгрузке все 83 предупреждения «возможно, тёзка» разошлись ровно по
        адресу — при совпавших ФИО **и** дате рождения. Полный тёзка с точностью
        до дня рождения — редкость; переезд и другая запись того же адреса в 1С —
        обычное дело. Восемьдесят три ложные тревоги подряд не осторожность: за
        ними перестают читать настоящие.
        """
        return (self.full_name, self.contract_number, self.inn, self.phone)

    def contradicts(self, other: DebtorRow) -> bool:
        """Расходятся ли строки в том, кто это за человек.

        Сравниваются только поля, заполненные в обеих строках. Пустое место — не
        возражение: во второй строке того же должника телефон часто просто не
        указан, и читать это как «а вдруг тёзка» значит поднять тревогу там, где
        никто ни с кем не спорит.
        """
        return any(
            mine is not None and theirs is not None and mine != theirs
            for mine, theirs in zip(self.hard_identifiers, other.hard_identifiers, strict=True)
        )

    @property
    def identity_is_strong(self) -> bool:
        """Есть ли чем отличить этого человека от полного тёзки.

        Код должника, номер договора и дата рождения — есть; одно голое ФИО —
        нет. Разница принципиальная: при слабой личности любое расхождение в
        строках означает «возможно, это два разных человека», а при сильной —
        «тот же человек приехал во второй раз». Молча слить двух людей дороже,
        чем показать лишнюю строку, поэтому при слабой личности тревога
        поднимается всегда.
        """
        return bool(self.debtor_id or self.contract_number or self.birth_date)

    def absorb(self, earlier: DebtorRow) -> None:
        """Дописать в строку то, чего в ней нет, из более ранней строки того же
        должника.

        Раньше на одинаковом ключе поздняя строка заменяла раннюю целиком, и
        обещание модуля «более бедная выгрузка не затирает уже известное»
        держалось только между импортами, но не внутри одного файла: если ИНН
        стоял в первой строке человека, а во второй его не было, он терялся до
        того, как строка доходила до базы. Приоритет остаётся за поздней строкой
        — она свежее, — но пустоты в ней заполняет ранняя.
        """
        for name in (
            "debtor_id",
            "full_name",
            "birth_date",
            "phone",
            "inn",
            "contract_number",
            "claim_number",
            "debt_amount",
            "address",
            "vehicle_plate",
            "vin",
            "created_at",
            "impounded_at",
            "released_at",
        ):
            if getattr(self, name) is None and getattr(earlier, name) is not None:
                setattr(self, name, getattr(earlier, name))
        # Машины — в порядке файла: список читает человек, и первым он ждёт то,
        # что стоит выше в выгрузке.
        self.vehicle_plates = [
            *earlier.vehicle_plates,
            *(plate for plate in self.vehicle_plates if plate not in earlier.vehicle_plates),
        ]
        self.source_ids = [
            *earlier.source_ids,
            *(sid for sid in self.source_ids if sid not in earlier.source_ids),
        ]


@dataclass(slots=True)
class RowError:
    line_number: int
    message: str


@dataclass(frozen=True, slots=True)
class TotalsRow:
    """Итоговая строка отчёта («Итого», «Всего»), а не должник.

    Молча пропустить её нельзя: оператор должен видеть, что строка была и что
    решение принял импорт, а не он.
    """

    line_number: int
    label: str


@dataclass(frozen=True, slots=True)
class HeaderMapping:
    """Разбор шапки: что распознано и что выброшено.

    ``unknown`` и ``duplicates`` существуют ради отчёта оператору. Никакой
    словарь синонимов не покроет всех выгрузок 1С, поэтому единственная честная
    защита — назвать колонки, содержимое которых не импортировано.
    """

    columns: dict[int, str | None]
    unknown: tuple[str, ...]
    duplicates: tuple[str, ...]

    @property
    def known(self) -> set[str]:
        return {value for value in self.columns.values() if value}


@dataclass(frozen=True, slots=True)
class DebtorTable:
    """Шапка отдельно от строк: об отброшенных колонках надо рассказать до того,
    как что-то будет записано."""

    header: HeaderMapping
    rows: Iterator[tuple[int, DebtorRow | RowError | TotalsRow]]


def decode_csv_bytes(payload: bytes) -> str:
    """Decode an upload, trying the encodings Russian exports actually use."""
    for encoding in SUPPORTED_ENCODINGS:
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvFormatError(
        "Не удалось определить кодировку файла. Поддерживаются UTF-8 и Windows-1251."
    )


def _detect_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=SUPPORTED_DELIMITERS).delimiter
    except csv.Error:
        # Sniffing fails on single-column or unusual files; comma is the
        # documented default.
        return ","


def _normalize_key(name: str) -> str:
    """Заголовок — к виду, в котором его ищут в словаре синонимов.

    Приводится всё оформление, из-за которого «ФИО» распознавалось, а «Ф.И.О.»
    и «ФИО  должника» — нет: неразрывные и повторные пробелы, точки в
    сокращениях, кавычки, «№» словом, ё/е, слитная запись 1С.
    """
    # «№» разворачивается до NFKC: нормализация превратила бы его в «No», и
    # «№ договора» перестало бы совпадать с «номер договора».
    text = name.replace("№", " номер ")
    text = unicodedata.normalize("NFKC", text).replace("﻿", "").replace("\xa0", " ")
    for pattern in _CAMEL_BOUNDARIES:
        text = pattern.sub(" ", text)
    text = _HEADER_PUNCTUATION.sub(" ", text)
    text = text.casefold().replace("ё", "е")
    return "_".join(text.split())


# Словарь синонимов написан по-человечески, а сравнение идёт по нормализованной
# форме — иначе каждый синоним пришлось бы вносить во всех написаниях сразу.
_ALIAS_INDEX: dict[str, str] = {
    _normalize_key(alias): canonical for alias, canonical in COLUMN_ALIASES.items()
}


def normalize_header(name: str) -> str | None:
    key = _normalize_key(name)
    if key in CANONICAL_COLUMNS:
        return key
    return _ALIAS_INDEX.get(key)


def parse_header(header: list[str]) -> HeaderMapping:
    """Сопоставить колонки файла с полями должника.

    При двух колонках на одно поле выигрывает первая: раньше побеждала
    последняя — «НомерДокументаРасчетов» затирал «Договор», и никто об этом не
    узнавал. Проигравшая колонка попадает в отчёт, а не в тишину.
    """
    columns: dict[int, str | None] = {}
    unknown: list[str] = []
    duplicates: list[str] = []
    taken: dict[str, str] = {}
    for index, name in enumerate(header):
        label = " ".join(name.split())
        canonical = normalize_header(name)
        if canonical is None:
            columns[index] = None
            if label:
                unknown.append(label)
            continue
        if canonical in taken:
            columns[index] = None
            duplicates.append(f"«{label}» дублирует «{taken[canonical]}»")
            continue
        taken[canonical] = label
        columns[index] = canonical
    return HeaderMapping(columns=columns, unknown=tuple(unknown), duplicates=tuple(duplicates))


def read_table(text: str) -> DebtorTable:
    """Прочитать шапку сразу, строки — лениво.

    Шапка разбирается до первой строки данных: ошибка в ней должна свалить файл
    целиком, до любой частичной записи, а перечень выброшенных колонок нужен
    отчёту независимо от того, дочитали ли мы строки.
    """
    if not text.strip():
        raise CsvFormatError("Файл пуст.")

    sample = text[:4096]
    reader = csv.reader(io.StringIO(text), delimiter=_detect_delimiter(sample))
    try:
        header = next(reader)
    except StopIteration as exc:
        raise CsvFormatError("В файле нет заголовка.") from exc

    mapping = parse_header(header)
    known = mapping.known
    # Отказ называет непонятые заголовки: иначе оператор видит «не распознано»
    # и не знает, ту ли колонку переименовывать.
    found = ("В файле: " + ", ".join(mapping.unknown[:10])) if mapping.unknown else ""
    if not known:
        raise CsvFormatError(
            "Не распознана ни одна колонка. Ожидаются, например: "
            f"{', '.join(COLUMN_TITLES[name] for name in CANONICAL_COLUMNS[:5])}. {found}".strip()
        )
    if not ({"fio", "contract_number"} & known):
        raise CsvFormatError(
            f"Нужна хотя бы одна из колонок: "
            f"{COLUMN_TITLES['fio']} или {COLUMN_TITLES['contract_number']}. {found}".strip()
        )

    return DebtorTable(header=mapping, rows=_iter_data_rows(reader, mapping))


def iter_rows(text: str) -> Iterator[tuple[int, DebtorRow | RowError | TotalsRow]]:
    """Yield ``(line_number, row_or_error)`` for every data line.

    A malformed row produces a :class:`RowError` and the iteration continues —
    one bad line must never abort an import of several hundred good ones.
    """
    return read_table(text).rows


def _iter_data_rows(
    reader: Iterator[list[str]], mapping: HeaderMapping
) -> Iterator[tuple[int, DebtorRow | RowError | TotalsRow]]:
    for line_number, raw_row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in raw_row):
            continue
        totals = _totals_label(raw_row)
        if totals is not None:
            yield line_number, TotalsRow(line_number=line_number, label=totals)
            continue
        try:
            yield line_number, _build_row(mapping.columns, raw_row)
        except ValueError as exc:
            yield line_number, RowError(line_number=line_number, message=str(exc))


def _totals_label(raw_row: list[str]) -> str | None:
    """«Итого» в первой значимой колонке — подпись итоговой строки, иначе ``None``."""
    for cell in raw_row:
        text = cell.strip()
        if not text:
            continue
        key = " ".join(_HEADER_PUNCTUATION.sub(" ", text).casefold().replace("ё", "е").split())
        return text if key in TOTALS_MARKERS else None
    return None


def _build_row(mapping: dict[int, str | None], raw_row: list[str]) -> DebtorRow:
    values: dict[str, str] = {}
    for index, cell in enumerate(raw_row):
        column = mapping.get(index)
        if column:
            values[column] = cell.strip()[:MAX_FIELD_LENGTH]

    row = DebtorRow()
    row.debtor_id = values.get("debtor_id") or None
    row.full_name = _clean_name(values.get("fio"), row)
    row.contract_number = values.get("contract_number") or None
    row.claim_number = values.get("claim_number") or None

    if not row.full_name and not row.contract_number:
        raise ValueError("нет ни ФИО, ни номера договора — проверять некого")

    row.birth_date = _parse_optional_date(
        values.get("birth_date"), COLUMN_TITLES["birth_date"], row
    )
    row.phone = _parse_optional_phone(values.get("phone"), row)
    row.inn = _parse_optional_inn(values.get("inn"), row)
    row.debt_amount = _parse_optional_amount(values.get("debt_amount"), row)
    row.address = normalize_address(values.get("address"))
    row.vehicle_plate = _parse_optional_plate(values.get("vehicle_plate"), row)
    if row.vehicle_plate:
        row.vehicle_plates.append(row.vehicle_plate)
    row.vin = _parse_optional_vin(values.get("vin"), row)
    row.created_at = _parse_created_at(values.get("created_at"))
    row.impounded_at = _parse_moment(values.get("impounded_at"))
    row.released_at = _parse_moment(values.get("released_at"))
    source_id = values.get("source_id") or None
    if source_id:
        row.source_ids.append(source_id)
    return row


def _clean_name(raw: str | None, row: DebtorRow) -> str | None:
    if not raw:
        return None
    try:
        return parse_fio(raw).full
    except NameParseError:
        # Keep the raw value: an unparseable name is still worth storing and
        # displaying, it just cannot participate in structured name matching.
        # Без кавычек: в кавычках отчёт показывает конкретное значение поля, а
        # тут это часть самой формулировки.
        row.warnings.append("ФИО: не разобрано в формате Фамилия Имя Отчество")
        return " ".join(raw.split()) or None


def _parse_optional_date(raw: str | None, label: str, row: DebtorRow) -> date | None:
    if not raw:
        return None
    parsed = parse_date(raw)
    if parsed is None:
        row.warnings.append(f"{label}: не распознана дата «{raw}»")
    return parsed


def _parse_optional_inn(raw: str | None, row: DebtorRow) -> str | None:
    """ИНН физлица из выгрузки — двенадцать цифр, и только они.

    Десятизначный ИНН принадлежит юрлицу, и подставлять его в проверку человека
    нельзя: источники отвергнут запрос, а оператор увидит «не проверено» без
    объяснимой причины. Непохожее значение не молчит, а становится замечанием
    к строке — выгрузка чинится один раз, а неверный ИНН тянулся бы в каждый
    отчёт по этому должнику.
    """
    if not raw:
        return None
    normalized = normalize_inn(raw)
    if normalized is None or len(normalized) != INN_INDIVIDUAL_LENGTH:
        row.warnings.append(f"ИНН: не похоже на ИНН физлица «{raw}»")
        return None
    return normalized


def _parse_optional_phone(raw: str | None, row: DebtorRow) -> str | None:
    if not raw:
        return None
    normalized = normalize_phone(raw)
    if normalized is None:
        row.warnings.append("Телефон: не распознан российский номер")
    return normalized


def _parse_optional_amount(raw: str | None, row: DebtorRow) -> Decimal | None:
    if not raw:
        return None
    amount = parse_amount(raw)
    if amount is None:
        row.warnings.append("Сумма долга: не распознана")
        return None
    if amount < 0:
        row.warnings.append("Сумма долга: отрицательная, не принята")
        return None
    return amount


def _parse_optional_plate(raw: str | None, row: DebtorRow) -> str | None:
    if not raw:
        return None
    normalized = normalize_plate(raw)
    if normalized is None and not _means_no_plate(raw):
        # «Б/Н» — не опечатка, а запись о том, что номера у машины нет, и звать
        # оператора чинить выгрузку тут не за чем. Отличаем одно от другого:
        # иначе тридцать машин без номера выглядят как тридцать ошибок ввода, и
        # за ними теряются настоящие — иностранные номера и опечатки в регионе.
        row.warnings.append("Госномер: не распознан")
    return normalized


def _parse_optional_vin(raw: str | None, row: DebtorRow) -> str | None:
    if not raw:
        return None
    normalized = normalize_vin(raw)
    if normalized is None:
        row.warnings.append("VIN: не распознан, нужно 17 символов")
    return normalized


#: «03.03.2023 01:30» — как 1С печатает момент. ISO-разбор её не берёт, а разбор
#: одной даты теряет часы. Часы тут не мелочь: медиана хранения на живой выгрузке
#: десять часов при бесплатных первых сутках, то есть округление до даты
#: превратило бы бесплатную стоянку в платную для двух тысяч должников.
_MOMENT_FORMATS = ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M")


def _parse_moment(raw: str | None) -> datetime | None:
    """Момент со временем, если оно есть, и полночь, если его нет."""
    if not raw:
        return None
    text = raw.strip()
    for fmt in _MOMENT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return _parse_created_at(text)


def _parse_created_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = parse_date(raw)
        return datetime.combine(parsed, datetime.min.time()) if parsed else None
