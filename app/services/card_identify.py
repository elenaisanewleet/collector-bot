"""Опознание должника в выгрузке 1С — главном источнике этого бота.

Владелица сказала это прямо: «основной источник — 1С», «ФССП это просто уже
потом, это мы прикрутили для крутости». Отсюда правило, которое важнее порядка
вопросов: **как только человек опознан однозначно, вопросы прекращаются**.
Дословно — «после телефона и фамилии, или даже телефона, если уже найдена одна
строка в базе 1С, то всё, просто готовим отчёт».

Механика ровно одна и живёт здесь: после каждого введённого поля карточка
спрашивает выгрузку и смотрит, сколько строк подошло.

* одна — спрашивать больше нечего. ФИО, дата рождения, договор и машина берутся
  из строки, оставшиеся шаги не задаются вовсе;
* несколько — следующий вопрос задаётся ЗАТЕМ, чтобы их различить, и говорит об
  этом вслух: «нашёл троих с таким телефоном — уточните фамилию»;
* ни одной — идём дальше по шагам.

Почему это здесь, а не в :mod:`app.services.query_card`. Карточка — состояние
сборки: она знает про поля, про БД и про то, что во что дописывается, и не знает
про поиск. Дай ей ``SearchService``, и модуль состояния начнёт ходить в сеть.
Поэтому опознание — отдельная функция над обоими, а карточка остаётся тем, чем
была.

Почему запрос бесплатный и его не жалко звать после каждого поля. Внутренний
источник — наш собственный файл или наша же таблица: ключа не просит, денег не
стоит, отвечает локально. Именно поэтому «показать данные из 1С сразу, не
дожидаясь реестров» — не оптимизация, а нормальный порядок работы.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.enums import SearchType
from app.domain.identity import (
    NameParseError,
    PersonName,
    SearchSubject,
    VehicleDescriptor,
    parse_fio,
)
from app.domain.models import InternalDebtorRecord
from app.services.query_card import Card
from app.services.search import SearchService
from app.utils.money import format_amount

#: Сколько записей перечислять поимённо, когда их несколько. Больше пяти строк
#: в карточке — это уже не подсказка, а простыня, которую не читают.
NAMES_SHOWN = 5


@dataclass(frozen=True, slots=True)
class Identified:
    """Что выгрузка ответила на то, что уже собрано в карточке.

    Пустой результат раньше значил «спросить было нечем ИЛИ никого нет» одним
    случаем: для порядка вопросов оба означают «задавай следующий». Но оператор
    читает не порядок вопросов, а экран, и на нём эти два состояния обязаны
    различаться — как и везде в этом продукте. Человек, приславший номер и
    получивший вопрос «Фамилия», решает, что бот его не понял; на деле бот искал
    и не нашёл, и это ответ, а не молчание.

    Поэтому ``asked`` — спрашивали ли выгрузку вообще.
    """

    records: tuple[InternalDebtorRecord, ...] = ()
    #: Выгрузку спросили. ``False`` — спрашивать было нечем.
    asked: bool = False

    @property
    def missed(self) -> bool:
        """Спросили и не нашли. Это ответ, и его говорят вслух."""
        return self.asked and not self.records

    @property
    def only(self) -> InternalDebtorRecord | None:
        """Единственная подошедшая строка. ``None`` — ноль или несколько."""
        return self.records[0] if len(self.records) == 1 else None

    @property
    def several(self) -> bool:
        return len(self.records) > 1

    @property
    def count(self) -> int:
        return len(self.records)


async def identify(search: SearchService, card: Card) -> Identified:
    """Спросить выгрузку тем, что уже есть в карточке.

    Не поднимает исключений: ``lookup_internal`` сам ловит отказ источника и
    возвращает пустой список. Для сценария упавшая выгрузка выглядит как «никого
    не нашёл» — то есть просто задаётся следующий вопрос. Это единственное
    место, где смешивать два случая можно: цена ошибки здесь — лишний вопрос, а
    не пустой раздел отчёта.
    """
    subject = probe(card)
    if subject is None:
        return Identified()
    return Identified(tuple(await search.lookup_internal(subject)), asked=True)


def probe(card: Card) -> SearchSubject | None:
    """Субъект для поиска ПО ВЫГРУПКЕ, а не для платной проверки.

    Отличается от :meth:`Card.subject` ровно одним, и это важно: сюда годится
    один телефон. ``Card.subject`` его не пускает намеренно — во внешние
    реестры с номером идти незачем, ни один из них по нему не ищет. А выгрузка
    ищет, и телефон в ней есть почти всегда: на этом стоит весь сценарий.

    Голая фамилия сюда не попадает, и это не упущение. ``find_by_fio`` сверяет
    имя целиком, откатываясь на «фамилия + имя»; одно слово не совпадёт ни с
    чем, и вызов был бы честно бесполезным. Поэтому опознание после второго шага
    случается только если телефон уже дал зацепку.
    """
    name = card.name
    vehicle = (
        VehicleDescriptor(plate=card.plate, vin=card.vin) if (card.plate or card.vin) else None
    )
    if not any((card.phone, name, card.contract_number, card.address, vehicle)):
        return None
    return SearchSubject(
        search_type=SearchType.PERSON.value,
        name=name,
        birth_date=card.birth_date,
        phone=card.phone,
        contract_number=card.contract_number,
        address=card.address,
        vehicle=vehicle,
    )


def absorb(card: Card, record: InternalDebtorRecord) -> list[str]:
    """Переписать в карточку то, чего в ней нет, из найденной строки 1С.

    Только пустые поля. Оператор набрал фамилию руками — значит он смотрит в
    свой документ, и затирать её тем, что лежит в выгрузке, нельзя: расхождение
    между документом и выгрузкой это факт, а не опечатка, и прятать его от
    оператора мы не будем.

    Возвращает подписи заполненных полей — их показывают вслух. Молча
    подставленная дата рождения ничем не отличается от угаданной, а «бот сам
    что-то дописал» — ровно та непрозрачность, от которой карточку и завели.
    """
    filled: list[str] = []
    name = _name_of(record)
    if name is not None and not card.last_name:
        card.last_name = name.last_name
        card.first_name = name.first_name
        card.middle_name = card.middle_name or name.middle_name
        card.skipped = card.skipped - {"last_name", "first_name", "middle_name"}
        filled.append("ФИО")
    for column, title, value in (
        ("birth_date", "дату рождения", record.birth_date),
        ("contract_number", "договор", record.contract_number),
        ("address", "адрес", record.address),
        ("plate", "госномер", record.vehicle_plate),
        ("vin", "VIN", record.vin),
    ):
        if value and not card.value(column):
            setattr(card, column, value)
            card.skipped = card.skipped - {column}
            filled.append(title)
    return filled


def describe(record: InternalDebtorRecord) -> str:
    """Одна строка про найденного: кто, по какому договору и сколько должен.

    Показывается ДО того, как опрошен хоть один реестр, и в этом весь смысл:
    внешние источники отвечают долго, стоят денег и иногда молчат, а то, что уже
    лежит у нас, полезно само по себе. Нашли — бот уже пригодился.
    """
    parts: list[str] = [record.full_name or "без имени"]
    if record.birth_date is not None:
        parts.append(record.birth_date.strftime("%d.%m.%Y"))
    if record.contract_number:
        parts.append(f"договор {record.contract_number}")
    if record.vehicle_plate:
        parts.append(record.vehicle_plate)
    if record.debt_amount is not None:
        parts.append(f"долг {format_amount(record.debt_amount)}")
    return ", ".join(parts)


def listing(records: tuple[InternalDebtorRecord, ...]) -> str:
    """Перечислить найденных, чтобы вопрос «уточните фамилию» был обоснован."""
    shown = [_short(record) for record in records[:NAMES_SHOWN]]
    if len(records) > NAMES_SHOWN:
        shown.append("…")
    return "; ".join(shown)


def _short(record: InternalDebtorRecord) -> str:
    if record.full_name:
        return record.full_name
    return record.contract_number or record.debtor_id or "без имени"


def _name_of(record: InternalDebtorRecord) -> PersonName | None:
    if not record.full_name:
        return None
    try:
        return parse_fio(record.full_name)
    except NameParseError:
        # Строка выгрузки бывает «ООО Ромашка» или «Иванов И.И.». Разбор строгий
        # намеренно (см. ``parse_fio``), и подсовывать карточке половину имени
        # хуже, чем не подставить ничего: следующий шаг спросит фамилию сам.
        return None


__all__ = ["Identified", "absorb", "describe", "identify", "listing", "probe"]
