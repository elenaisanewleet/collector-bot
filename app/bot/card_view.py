"""Как выглядит накопительная карточка запроса.

Текст и клавиатура собираются здесь и только здесь, одной функцией
:func:`screen`. Раздельные рендеры уже пробовали в этом проекте — они
разъезжаются: подпись меняют в одном месте, условие показа в другом, и под
строкой «Дата рождения: жду» остаются кнопки, которых там быть не должно.

Разметки нет: ``parse_mode=None`` выставлен глобально (``app/main.py``), поэтому
точечные линейки из макета («Фамилия ····· Клочкова») не выровнялись бы, а
markdown приехал бы в чат звёздочками. Строки рисуются как «Поле: значение».
Эмодзи — только на кнопках, где они работают иконками; в тексте их нет, это
общее правило :mod:`app.bot.view`.

Чего здесь нет намеренно: **счётчика полноты**. Ни «заполнено 4 из 7», ни
процентов, ни полоски. Полей семь, а связок три ({ФИО + дата}, {ИНН}, {паспорт
+ ФИО + дата}) — линейная шкала соврала бы, а карточка не имеет права создавать
впечатление, что чем больше полей, тем полнее проверка «в целом».
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.identifiers import Field
from app.bot.view import missing_reason
from app.domain.enums import PROVIDER_TITLES, ProviderName, SearchType
from app.domain.identity import (
    INN_INDIVIDUAL_LENGTH,
    PASSPORT_LENGTH,
    PersonName,
    SearchSubject,
)
from app.providers.registry import ProviderRegistry
from app.services import coverage
from app.services.query_card import FIELD_ORDER, FIELD_TITLES, OPTIONAL_ROWS, STEPS, Card
from app.utils.dates import format_datetime

# ---------------------------------------------------------------- callbacks

#: Префикс карточки. Свободен: заняты ``menu``, ``padd``, ``pten``, ``pskip``,
#: ``region``, ``refresh``, ``repeat``, ``batch``, ``access``, ``external``,
#: ``cancel``, ``skip``.
QC = "qc"
QC_ASK = f"{QC}:ask"
QC_RUN = f"{QC}:run"
QC_WIPE = f"{QC}:wipe"
QC_SKIP = f"{QC}:skip"
QC_CANCEL = f"{QC}:cancel"
QC_TEN_PASSPORT = f"{QC}:ten:p"
QC_TEN_PHONE = f"{QC}:ten:t"
QC_NEW_PERSON = f"{QC}:new"
QC_FIX_NAME = f"{QC}:keep"
#: «Дальше» на каждом из трёх основных шагов.
QC_NEXT = f"{QC}:next"

# ---------------------------------------------------------------- тексты

HEAD = "Собираю проверку"
DASH_MEANING = "Прочерк — это «я не спрашивал», а не «не нашли»."
INVITE = "Пришлите сообщением — допишу сюда же."
INVITE_AFTER = "Дошлите поле сообщением или кнопкой — перепроверю того же человека."
NOT_ASKED = "Это «не спрашивали», а не «не найдено»."

# ---------------------------------------------------------------- три шага

#: Подпись кнопки «дальше». Одна и та же на всех трёх шагах — дословное
#: требование владелицы: её не должны искать глазами заново на каждом экране.
#: И стоит она под каждым шагом без исключений: «то есть ты можешь не написать
#: ничего».
NEXT_LABEL = "Дальше"

STEP_TOTAL = len(STEPS)

#: Как называется шаг и чем он заполняется. Коротко, по одному полю: длинное
#: «Пришлите одной строкой всё, что знаете» — ровно та фраза, на которую
#: жаловались.
STEP_TITLES: dict[str, str] = {
    Field.PHONE.value: "Телефон",
    Field.LAST_NAME.value: "Фамилия",
    Field.FIRST_NAME.value: "Имя",
}

#: Пример показывается на каждом шаге: «везде показывается пример как заполнять,
#: но ты можешь просто нажать дальше».
STEP_EXAMPLES: dict[str, str] = {
    Field.PHONE.value: "+7 916 000-00-00",
    Field.LAST_NAME.value: "Клочкова",
    Field.FIRST_NAME.value: "Елена",
}

#: Зачем этот шаг вообще. У телефона причина не косметическая — он один
#: разворачивается в целую строку выгрузки, и оператор должен знать, что тратит
#: на него меньше, чем сэкономит.
STEP_WHY: dict[str, str] = {
    Field.PHONE.value: (
        "По нему найду человека в вашей выгрузке — там уже есть и ФИО, "
        "и дата рождения, и договор. Тогда остальное спрашивать не буду."
    ),
    Field.LAST_NAME.value: "",
    Field.FIRST_NAME.value: "",
}

STEP_HEAD = "Шаг {number} из {total}. {title}"
STEP_EXAMPLE = "Например: {example}"
STEP_SKIPPABLE = f"Не знаете — нажмите «{NEXT_LABEL}», пропущу."

#: Опознали в выгрузке ровно одного — вопросов больше нет.
FOUND_ONE = "Нашёл в вашей базе: {who}."
FOUND_ONE_FILLED = "Заполнил из выгрузки: {fields}. Больше ничего не спрашиваю, готовлю отчёт."
#: Опознали нескольких — следующий вопрос задаётся ЗАТЕМ, чтобы их различить.
FOUND_MANY = "Нашёл {count} записи с такими данными: {who}."
FOUND_MANY_ASK = "Уточните {field}, чтобы различить их."
FOUND_MANY_STUCK = (
    "Различить их нечем: основные вопросы кончились. Добавьте поле из списка ниже."
)

# ---------------------------------------------------------------- меню опций

MENU_HEAD = "Что ещё можно добавить и что это откроет:"
MENU_AFTER_EMPTY = (
    "Ничего не нашлось по тем данным, что были. Это не «должника нет» — это «мы спросили "
    "не тем ключом». Попробуйте другой:"
)
MENU_NOTHING = (
    "Искать пока не по чему: все три вопроса пропущены. Ничего страшного — "
    "начать можно с любого поля ниже."
)
MENU_OPENS = "откроет {sources}"


@dataclass(frozen=True, slots=True)
class _Option:
    """Строка меню опций: поле, подпись и чем она кончается.

    ``probe`` — имя поля для :func:`coverage.unlocked_by`; пустое значит «этот
    ход внешние реестры не открывает вовсе», и тогда остаётся один ``tail``.
    Так и должно быть у телефона, госномера, договора и адреса: они работают на
    выгрузку 1С — главный источник, — а не на реестры, и притворяться иначе
    карточке нельзя.

    ``tail`` пишется словами, потому что описывает не список источников, а
    ПОСЛЕДСТВИЕ отсутствия поля, и посчитать его неоткуда: «раздел останется
    пустым не потому, что производств нет» — это про инвариант, а не про гейт
    провайдера.
    """

    field: Field
    title: str
    probe: str
    tail: str


#: Порядок — по убыванию пользы, а не по алфавиту: дата рождения и ИНН стоят
#: первыми, потому что открывают больше всего, а пропускают их чаще всего.
OPTIONS: tuple[_Option, ...] = (
    _Option(
        Field.BIRTH_DATE,
        "Дата рождения",
        "birth_date",
        "без неё раздел исполнительных производств останется пустым не потому, "
        "что производств нет, а потому, что их не спрашивали",
    ),
    _Option(Field.INN, "ИНН", "inn", "больше эти три ничем не открываются"),
    _Option(Field.MIDDLE_NAME, "Отчество", "", "точнее сопоставление, меньше однофамильцев"),
    _Option(
        Field.PASSPORT,
        "Паспорт",
        "passport",
        "по нему ФНС выдаёт ИНН, и тогда откроются те же три источника",
    ),
    _Option(
        Field.AUTO,
        "Госномер или VIN",
        "vin",
        "по госномеру ищет только ваша выгрузка, внешние реестры по нему не ищут",
    ),
    _Option(Field.PHONE, "Телефон", "", "поднимет строку из 1С: там ФИО, договор и машина"),
    _Option(Field.CONTRACT, "Договор", "", "поднимет строку из 1С"),
    _Option(Field.ADDRESS, "Адрес", "", "поднимет строку из 1С"),
)

EMPTY = "—"
SKIPPED = "пропустили"
FORGOTTEN = "сам номер не храню, пришлите заново"

#: Что говорит строка ожидания. Каждая называет не «требуется», а «откроет»:
#: оператор решает, стоит ли поле того, и обязан знать цену.
HINTS: dict[str, str] = {
    Field.BIRTH_DATE.value: (
        "Дата рождения, ДД.ММ.ГГГГ. Откроет ФССП и залоги ФНП — без неё оба не ищут вовсе."
    ),
    Field.INN.value: (
        f"ИНН физлица, {INN_INDIVIDUAL_LENGTH} цифр. Откроет банкротство (ЕФРСБ), "
        "статус ИП (ФНС) и арбитраж — эти три больше ничем не открываются. "
        "Десятизначный ИНН — организации, он не подойдёт."
    ),
    Field.PHONE.value: (
        "Телефон. Во внешние реестры он не уходит: находит запись в вашей базе "
        "и укрепляет сопоставление. Номер не сохраняю, только маску."
    ),
    Field.AUTO.value: (
        "Госномер или VIN. VIN откроет залоги ФНП даже без даты рождения. "
        "Госномер ищет только по вашей базе."
    ),
    Field.FIO.value: "Фамилия, имя и, если есть, отчество — одной строкой или по одному слову.",
    Field.LAST_NAME.value: "Фамилия. Одним словом.",
    Field.FIRST_NAME.value: "Имя. Одним словом.",
    Field.MIDDLE_NAME.value: (
        "Отчество. Внешних источников не открывает — делает сопоставление точнее "
        "и отсекает однофамильцев."
    ),
    Field.CONTRACT.value: (
        "Номер договора или заявки. Ищет только по вашей выгрузке — "
        "поднимет строку из 1С с ФИО, долгом и машиной."
    ),
    Field.ADDRESS.value: (
        "Адрес. Ищет только по вашей выгрузке; внешние реестры по адресу не ищут."
    ),
}

PASSPORT_HINT = (
    f"Серия и номер паспорта, {PASSPORT_LENGTH} цифр. Сам по себе не открывает ничего — "
    "по нему ФНС выдаёт ИНН, и тогда откроются те же три источника. Запрос платный. "
    "Ваше сообщение с номером я удалю, номер не сохраняю."
)
PASSPORT_HINT_STORED = (
    f"Серия и номер паспорта, {PASSPORT_LENGTH} цифр. Сам по себе не открывает ничего — "
    "по нему ФНС выдаёт ИНН, и тогда откроются те же три источника. Запрос платный. "
    "Ваше сообщение с номером я удалю; сам номер уйдёт в базу, потому что включён "
    "STORE_SENSITIVE_IDENTIFIERS."
)

TEN_QUESTION = "{digits} — паспорт или телефон?"
TEN_WHY = (
    "Десять цифр с девятки бывают и серией паспорта (90xx, 92xx), "
    "и мобильным без кода страны. Угадать нельзя: угаданный телефон закрывает "
    "единственный вход в мост «паспорт → ИНН»."
)

CONFLICT = "В карточке сейчас {current}. «{incoming}» — это другой человек или исправление?"
CONFLICT_WHY = (
    "Сам решить не могу: перепутать двух должников дороже одного нажатия. "
    "Пока вопрос открыт, остальные поля дописываются как обычно."
)

#: Всплывающие ответы на нажатия.
NOTHING_TO_RUN = "Пока нечего спросить: нужны фамилия с именем, или ИНН, или госномер."
NOTHING_CHANGED = (
    "С прошлой проверки ничего не добавилось. Добавьте поле или нажмите «Обновить» под отчётом."
)
SKIPPED_ANSWER = "Пропустил. Это «не спрашивали», а не «не найдено»."
WIPED = "Карточка пуста."

#: Подсказки формата в строке ожидания.
_WAITING_FORMATS: dict[str, str] = {
    Field.BIRTH_DATE.value: "ДД.ММ.ГГГГ",
    Field.INN.value: f"{INN_INDIVIDUAL_LENGTH} цифр",
    Field.PHONE.value: "+7 916 000 00 00",
    Field.PASSPORT.value: f"{PASSPORT_LENGTH} цифр",
    Field.AUTO.value: "О123АА777 или VIN",
    Field.FIO.value: "Фамилия Имя Отчество",
    Field.LAST_NAME.value: "Клочкова",
    Field.FIRST_NAME.value: "Елена",
    Field.MIDDLE_NAME.value: "Николаевна",
    Field.CONTRACT.value: "номер договора",
    Field.ADDRESS.value: "город, улица, дом",
}

#: Какая строка карточки светится при ожидании поля. ``fio`` светит три сразу,
#: ``auto`` — ту, что уже занята, иначе госномер.
_WAITING_ROWS: dict[str, tuple[str, ...]] = {
    Field.BIRTH_DATE.value: ("birth_date",),
    Field.INN.value: ("inn",),
    Field.PHONE.value: ("phone",),
    Field.PASSPORT.value: ("passport",),
    Field.AUTO.value: ("plate",),
    Field.FIO.value: ("last_name", "first_name", "middle_name"),
    Field.LAST_NAME.value: ("last_name",),
    Field.FIRST_NAME.value: ("first_name",),
    Field.MIDDLE_NAME.value: ("middle_name",),
    Field.CONTRACT.value: ("contract_number",),
    Field.ADDRESS.value: ("address",),
}

_AWAITING_TEN = "ten"


@dataclass(frozen=True, slots=True)
class Screen:
    """Вид карточки целиком. Текст и клавиатура неразделимы."""

    text: str
    markup: InlineKeyboardMarkup

    @property
    def markup_key(self) -> str:
        """Сравнимая форма клавиатуры — чтобы не звать ``edit_text`` впустую."""
        return json.dumps(self.markup.model_dump(exclude_none=True), sort_keys=True)


def screen(
    card: Card,
    *,
    registry: ProviderRegistry,
    store_sensitive: bool = False,
    notice: str | None = None,
    conflict: PersonName | None = None,
) -> Screen:
    """Собрать карточку: текст и кнопки под ним.

    ``notice`` — ответ на последнее сообщение: «записал в фамилию», «не понял»,
    «на дату не похоже». Он стоит В карточке, а не отдельным сообщением, и это
    то же решение, что и вся карточка: ответ, уехавший вверх чата, теряется
    ровно так же, как терялся прежний ввод.
    """
    return Screen(
        text=_text(
            card, registry, notice=notice, conflict=conflict, store_sensitive=store_sensitive
        ),
        markup=keyboard(card, conflict=conflict),
    )


# ---------------------------------------------------------------- текст


def _text(
    card: Card,
    registry: ProviderRegistry,
    *,
    notice: str | None,
    conflict: PersonName | None,
    store_sensitive: bool,
) -> str:
    lines = [_head(card), ""]
    lines.extend(_rows(card))
    lines.append("")

    step = card.step
    if step is not None:
        # Один вопрос на экран. Всё, что уже собрано, стоит выше — «Клочкова»,
        # потом «Елена», потом «24 11 1994» это один человек, и видно, что один.
        if notice:
            lines.extend((notice, ""))
        lines.extend(_step_lines(step))
        return "\n".join(lines)

    if conflict is not None:
        lines.append(CONFLICT.format(current=_current_name(card), incoming=conflict.full))
        lines.append(CONFLICT_WHY)
        return "\n".join(lines)

    if card.awaiting_field == _AWAITING_TEN:
        lines.append(TEN_QUESTION.format(digits=card.pending_ten or ""))
        lines.append(TEN_WHY)
        return "\n".join(lines)

    if notice:
        lines.extend((notice, ""))

    if card.awaiting_field:
        lines.append(_hint(card.awaiting_field, store_sensitive=store_sensitive))
        return "\n".join(lines)

    lines.append(DASH_MEANING)
    lines.append("")
    lines.extend(_coverage_lines(card, registry))
    lines.append("")
    lines.extend(_menu_lines(card, registry))
    lines.append(INVITE_AFTER if card.checked_at else INVITE)
    return "\n".join(lines)


def _step_lines(step: Field) -> list[str]:
    """Один шаг: как называется, пример, зачем и что «Дальше» тоже ответ.

    Пример и «Дальше» стоят на каждом шаге без исключений. Это дословное
    требование, и оно про доверие: оператор, который не знает, можно ли
    пропустить, впишет что-нибудь наугад — и карточка получит мусор вместо
    прочерка, то есть «не нашли» вместо «не спрашивали».
    """
    number = STEPS.index(step) + 1
    lines = [
        STEP_HEAD.format(number=number, total=STEP_TOTAL, title=STEP_TITLES[step.value]),
        STEP_EXAMPLE.format(example=STEP_EXAMPLES[step.value]),
    ]
    why = STEP_WHY.get(step.value)
    if why:
        lines.extend(("", why))
    lines.extend(("", STEP_SKIPPABLE))
    return lines


def _menu_lines(card: Card, registry: ProviderRegistry) -> list[str]:
    """Меню опций: что ещё можно добавить и что каждое поле откроет.

    Формулировка обязана быть именно такой — не «введите поле», а что оно даст.
    Иначе оператор жмёт «Дальше» на самом ценном поле, не зная цены.

    Список источников не написан словами, а посчитан
    :func:`coverage.unlocked_by`: разница между «кого спросим сейчас» и «кого
    спросили бы с этим полем». Захардкоженный список разъезжается с гейтом
    провайдера при первой же правке и, разъехавшись, обещает источник, который
    откажется отвечать. Поэтому у телефона в этом списке источников нет вовсе —
    ни один внешний реестр по номеру не ищет, и обещать иное было бы враньём
    формой.
    """
    options = [line for line in (_option(card, spec, registry) for spec in OPTIONS) if line]
    if not options:
        return []
    if card.last_run_empty:
        lead = MENU_AFTER_EMPTY
    elif card.subject() is None:
        lead = MENU_NOTHING
    else:
        lead = MENU_HEAD
    return [lead, *options, ""]


def _option(card: Card, spec: _Option, registry: ProviderRegistry) -> str | None:
    """Строка меню про одно поле. ``None`` — поле уже заполнено, предлагать нечего."""
    if _is_filled(card, spec.field):
        return None
    tail = spec.tail
    sources = _unlocked(card, spec.probe, registry) if spec.probe else ""
    if sources:
        tail = f"{MENU_OPENS.format(sources=sources)}{f'; {tail}' if tail else ''}"
    return f"+ {spec.title} — {tail}" if tail else None


def _unlocked(card: Card, probe: str, registry: ProviderRegistry) -> str:
    """Кого откроет это поле — на нынешнем содержимом карточки, а не вообще."""
    base = card.subject() or SearchSubject(search_type=SearchType.PERSON.value)
    return _titles(coverage.unlocked_by(probe, base, registry))


def _head(card: Card) -> str:
    if card.checked_at:
        return f"Проверено в {format_datetime(card.checked_at)}"
    return HEAD


def _current_name(card: Card) -> str:
    parts = [card.last_name, card.first_name, card.middle_name]
    return " ".join(part for part in parts if part) or EMPTY


def _rows(card: Card) -> list[str]:
    """Строки полей. Порядок постоянный, необязательные появляются по делу."""
    waiting = _WAITING_ROWS.get(card.awaiting_field or "", ())
    rows: list[str] = []
    for name in FIELD_ORDER:
        shown = card.shown(name)
        if name in OPTIONAL_ROWS and not shown and name not in waiting:
            continue
        rows.append(f"{FIELD_TITLES[name]}: {_value(card, name, waiting)}")
    return rows


def _value(card: Card, name: str, waiting: tuple[str, ...]) -> str:
    shown = card.shown(name)
    if shown and card.forgotten(name):
        return f"{shown} — {FORGOTTEN}"
    if shown:
        return shown
    if name in waiting:
        hint = _WAITING_FORMATS.get(card.awaiting_field or "", "")
        return f"жду — {hint}" if hint else "жду"
    if name in card.skipped or _skipped_via(card, name):
        return SKIPPED
    return EMPTY


def _skipped_via(card: Card, name: str) -> bool:
    """Кнопка «+ Госномер или VIN» пропускает обе строки разом."""
    return name in {"plate", "vin"} and Field.AUTO.value in card.skipped


def _hint(field_name: str, *, store_sensitive: bool) -> str:
    if field_name == Field.PASSPORT.value:
        return PASSPORT_HINT_STORED if store_sensitive else PASSPORT_HINT
    return HINTS.get(field_name, "")


def _coverage_lines(card: Card, registry: ProviderRegistry) -> list[str]:
    """«Сейчас спрошу» и «Не спрошу» — посчитанные, а не написанные.

    Пустая карточка не показывает ни того, ни другого: список источников,
    которым «нужно ФИО», под пустой формой читается как список неудач, тогда
    как никакой проверки ещё и не начиналось.
    """
    subject = card.subject()
    if subject is None:
        return [
            "Нужны фамилия с именем — или ИНН, или госномер. Остальное дописывается кнопками ниже."
        ]

    lines: list[str] = []
    answering = coverage.will_answer(subject, registry)
    if answering:
        lines.append(f"{_lead(card)}: {_titles(answering)}.")
    gaps = coverage.blocked(subject, registry)
    if gaps:
        reasons = "; ".join(
            f"{_titles(names)} — {missing_reason(tuple(reason))}" for reason, names in gaps.items()
        )
        lines.append(f"{'Не спрошено' if card.checked_at else 'Не спрошу'}: {reasons}.")
        lines.append(NOT_ASKED)
    return lines


def _lead(card: Card) -> str:
    return "Спросил" if card.checked_at else "Сейчас спрошу"


def _titles(names: tuple[ProviderName, ...]) -> str:
    return ", ".join(PROVIDER_TITLES.get(name, name.value) for name in names)


# ---------------------------------------------------------------- кнопки


def keyboard(card: Card, *, conflict: PersonName | None = None) -> InlineKeyboardMarkup:
    """Кнопки под карточкой.

    Позиции постоянны: сотня должников в день делается мышечной памятью, и
    кнопка, переезжающая между рядами по мере заполнения, — это промах пальцем
    по «Очистить». Меняется только префикс подписи: ``+`` у пустого поля, ``✎``
    у заполненного.
    """
    if conflict is not None:
        # Вопрос стоит первым рядом, но остальные кнопки остаются. Модальный
        # экран здесь был бы ловушкой: пока оператор думает, чей это человек,
        # он не может ни доввести поле, ни запустить проверку, ни очистить
        # карточку — а вопрос сам собой не рассосётся.
        return _rows_markup(
            [
                [
                    _button("Новый человек — очистить", QC_NEW_PERSON),
                    _button("Это исправление", QC_FIX_NAME),
                ],
                *_field_rows(card),
                _run_row(card),
            ]
        )
    if card.awaiting_field == _AWAITING_TEN:
        return _rows_markup(
            [
                [
                    _button("🪪 Паспорт", QC_TEN_PASSPORT),
                    _button("📞 Телефон", QC_TEN_PHONE),
                ],
                [_button("Отмена", QC_CANCEL)],
            ]
        )
    if card.step is not None:
        # Ровно одна кнопка, и подпись у неё одна и та же на всех трёх шагах.
        # Второй кнопки здесь нет намеренно: шаг — это один вопрос, и выбор
        # «ответить или пропустить» не должен соревноваться с выбором «а не
        # нажать ли что-нибудь ещё». Меню опций придёт следом само.
        return _rows_markup([[_button(NEXT_LABEL, QC_NEXT)]])
    if card.awaiting_field:
        # Все ряды схлопываются в один: пока ждём поле, любая другая кнопка
        # означала бы «а нажми-ка вместо ответа что-нибудь ещё».
        return _rows_markup([[_button("Пропустить", QC_SKIP), _button("Отмена", QC_CANCEL)]])

    return _rows_markup([*_field_rows(card), _run_row(card)])


def _field_rows(card: Card) -> list[list[InlineKeyboardButton]]:
    """Кнопки полей. Ряды фиксированы, включая те, что уже заполнены.

    Заполненное поле не исчезает, а меняет префикс на «✎»: «любое поле правится
    кнопкой, не начиная сначала» — и правится там же, где добавлялось, иначе
    исправление приходится искать.

    Договор и адрес стоят последним рядом и вместе: они не открывают ни одного
    внешнего реестра и нужны ровно за тем, чтобы поднять строку из 1С.
    """
    return [
        [_ask(card, Field.BIRTH_DATE, "Дата рождения"), _ask(card, Field.INN, "ИНН")],
        [_ask(card, Field.PHONE, "Телефон"), _ask(card, Field.PASSPORT, "Паспорт")],
        [_ask(card, Field.AUTO, "Госномер или VIN"), _ask(card, Field.FIO, "ФИО")],
        [_ask(card, Field.MIDDLE_NAME, "Отчество"), _ask(card, Field.CONTRACT, "Договор")],
        [_ask(card, Field.ADDRESS, "Адрес")],
    ]


def _run_row(card: Card) -> list[InlineKeyboardButton]:
    run = "🔍 Перепроверить" if card.checked_at else "🔍 Проверить"
    wipe = "Новая проверка" if card.checked_at else "Очистить"
    return [_button(run, QC_RUN), _button(wipe, QC_WIPE)]


def _ask(card: Card, field: Field, title: str) -> InlineKeyboardButton:
    filled = _is_filled(card, field)
    prefix = "✎ Исправить" if filled else "+"
    return _button(f"{prefix} {title}", f"{QC_ASK}:{field.value}")


def _is_filled(card: Card, field: Field) -> bool:
    """Заполнено ли поле за кнопкой.

    Делегирует :meth:`Card.has`, а не повторяет разбор: имя поля в кнопке
    («last», «middle», «contract») намеренно не совпадает с именем колонки
    («last_name», «middle_name», «contract_number»), и вторая копия этого
    соответствия разошлась бы с первой — кнопка «+ Отчество» осталась бы с
    плюсом над заполненной строкой.
    """
    return card.has(field)


def _button(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def _rows_markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


__all__ = [
    "QC",
    "QC_ASK",
    "QC_CANCEL",
    "QC_FIX_NAME",
    "QC_NEW_PERSON",
    "QC_NEXT",
    "QC_RUN",
    "QC_SKIP",
    "QC_TEN_PASSPORT",
    "QC_TEN_PHONE",
    "QC_WIPE",
    "Screen",
    "keyboard",
    "screen",
]
