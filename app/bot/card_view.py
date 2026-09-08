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
from app.bot.keyboards import BACK_LABEL, MENU_HOME
from app.bot.view import missing_reason
from app.domain.enums import SearchType
from app.domain.identity import (
    INN_INDIVIDUAL_LENGTH,
    PASSPORT_LENGTH,
    PersonName,
    SearchSubject,
)
from app.providers.registry import ProviderRegistry
from app.services import coverage
from app.services.query_card import FIELD_ORDER, FIELD_TITLES, OPTIONAL_ROWS, Card
from app.utils.dates import format_datetime
from app.utils.formatting import pluralize_ru

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

#: Заголовок экрана-вопроса: имя поля со значком правки. Ровно как в боте,
#: который владелица показала образцом: «✎ Фамилия» и одна строка под ним.
#: Значок стоит в тексте, а не в подписи кнопки, — там он ничего не решает.
#:
#: Первый шаг называется «Телефон», а не «Номер», и это дословное указание
#: владелицы: «сначала спрашиваем только номер телефона, номер либо пишется
#: либо не пишется». Прежний «Номер» перечислял под собой четыре вида номера, а
#: по кнопке «Телефон» на собранной карточке тот же экран принимал только
#: телефон и отвергал собственный пример-госномер. Одно имя — один смысл, из
#: какой бы двери в экран ни вошли; чем шаг ЗАПОЛНЯЕТСЯ, решает код (см.
#: :meth:`app.services.query_card.QueryCardService.apply`).
ASK_TITLES: dict[str, str] = {
    Field.PHONE.value: "Номер или ФИО",
    Field.LAST_NAME.value: "Фамилия",
    Field.FIRST_NAME.value: "Имя",
    Field.MIDDLE_NAME.value: "Отчество",
    Field.FIO.value: "ФИО",
    Field.BIRTH_DATE.value: "Дата рождения",
    Field.INN.value: "ИНН",
    Field.PASSPORT.value: "Паспорт",
    Field.AUTO.value: "Госномер или VIN",
    Field.CONTRACT.value: "Договор или заявка",
    Field.ADDRESS.value: "Адрес",
}

#: Одна строка под заголовком — что сделать. Одинаковая почти везде: экран,
#: который каждый раз объясняет заново, читают один раз, а потом перестают.
ASK_PROMPT = "Отправьте значение сообщением."
#: Строка под первым шагом. Просит две вещи, и обе работают: номер телефона и
#: ФИО. Дословное указание владелицы — «давай просить ввести номер или ФИО».
#:
#: Перечисления «госномер, телефон, ИНН или номер договора» здесь больше нет:
#: его она прислала скриншотом как то, что надо убрать. Но и строго «телефон»
#: писать нельзя было — по ФИО шаг находит должника сразу, и экран, зовущий
#: только номер, скрывал бы рабочий путь. Проверено прогоном: «Алдушин Михаил
#: Валерьевич» на первом шаге доходит до отчёта без единого лишнего вопроса.
ASK_PROMPT_NUMBER = "Отправьте номер телефона или фамилию с именем."

#: Пример на каждом экране: «везде показывается пример как заполнять, но ты
#: можешь просто нажать дальше».
#:
#: Телефон показан в привычной записи, с плюсом и дефисами, — так его прислала
#: владелица. Формата это не задаёт: номер принимается в любой записи, и это
#: проверено шестью формами одного номера.
ASK_EXAMPLES: dict[str, str] = {
    # Пример — ФИО, а не номер, и это не вкус. Телефона в выгрузке нет ни у
    # одного из 2052 должников, а ФИО есть у всех: пример обязан показывать то,
    # что сработает с первого раза.
    Field.PHONE.value: "Иванов Иван Иванович",
    Field.LAST_NAME.value: "Клочкова",
    Field.FIRST_NAME.value: "Елена",
    Field.MIDDLE_NAME.value: "Николаевна",
    Field.FIO.value: "Клочкова Елена Николаевна",
    Field.BIRTH_DATE.value: "24.11.1994",
    Field.INN.value: "770123456789",
    Field.PASSPORT.value: "4510 123456",
    Field.AUTO.value: "А123ВС777",
    Field.CONTRACT.value: "ЭВ-2026/000082",
    Field.ADDRESS.value: "Москва, Дмитровское шоссе, 27, кв. 20",
}

#: Короткое имя поля для фраз вроде «уточните фамилию». Отдельно от вопроса:
#: «Уточните напишите фамилию.» — не по-русски.
ASK_NOUNS: dict[str, str] = {
    Field.PHONE.value: "телефон",
    Field.LAST_NAME.value: "фамилию",
    Field.FIRST_NAME.value: "имя",
    Field.MIDDLE_NAME.value: "отчество",
    Field.FIO.value: "ФИО",
    Field.BIRTH_DATE.value: "дату рождения",
    Field.INN.value: "ИНН",
    Field.PASSPORT.value: "паспорт",
    Field.AUTO.value: "госномер или VIN",
    Field.CONTRACT.value: "номер договора",
    Field.ADDRESS.value: "адрес",
}

#: Короткая оговорка под вопросом — только там, где она меняет поведение
#: человека.
#:
#: Здесь пусто, и это решение владелицы, принятое дословно: «надо убрать это,
#: нам надо наоборот сохранять эти номера». Раньше под вопросом о паспорте
#: стояло «номер не сохраняю и сообщение удалю» — обещание, которое бот и
#: держал: сообщение удалял, в карточке печатал маску. Продукт с тех пор стал
#: другим: он закрыт, принадлежит одному владельцу, и добывает документы
#: РОВНО ЗАТЕМ, чтобы тот подал с ними в суд. Обещание не сохранять документ,
#: который ты специально добываешь и обязан показать, — это не приватность, а
#: неправда на экране.
#:
#: Строку не переписали мягче, а убрали: оговорка, которая ничего не меняет в
#: поведении человека, — это лишний текст на экране, который читают каждый день.
ASK_NOTES: dict[str, str] = {}

ASK_EXAMPLE = "Например: {example}"
ASK_SKIPPABLE = "Не знаете — «Пропустить»."
#: На трёх основных шагах кнопка называется «Дальше», на доборе поля —
#: «Пропустить». Подпись в тексте обязана совпадать с подписью на кнопке.
STEP_SKIPPABLE = f"Не знаете — «{NEXT_LABEL}»."
#: Заголовок формы. Первое, что читают на экране, где ничего не спрашивают.
FORM_HEAD = "Проверка должника"
#: Подпись под заголовком формы, курсивом её сделать нечем — разметки нет.
FORM_LEAD = "Заполните что знаете и нажмите «Проверить»."
#: Значок поля в заголовке вопроса.
ASK_MARK = "✎"
#: Значок заполненного поля на кнопке. Не эмодзи-украшение, а состояние формы:
#: «видно и в тексте, и на кнопках» — дословный ориентир владелицы.
FILLED_MARK = "✅"

#: Опознали в выгрузке ровно одного — вопросов больше нет.
FOUND_ONE = "Нашёл: {who}."
FOUND_ONE_FILLED = "Готовлю отчёт."
#: Опознали нескольких — следующий вопрос задаётся ЗАТЕМ, чтобы их различить.
FOUND_MANY = "Нашёл {count} записи с такими данными: {who}."
FOUND_MANY_ASK = "Уточните {field}, чтобы различить их."
FOUND_MANY_STUCK = "Различить их нечем: основные вопросы кончились. Добавьте поле из списка ниже."
#: Спросили выгрузку и не нашли. Это ответ, а не молчание: человек, приславший
#: номер и получивший следующий вопрос без единого слова, решает, что бот его не
#: понял. Названо ровно то, чем искали, — иначе непонятно, что именно не нашлось.
NOT_IN_EXPORT = "По {key} никого не нашёл."
#: И сразу чем пробовать дальше. Порядок по убыванию надёжности для этой
#: выгрузки: госномер есть у каждой строки, ФИО у большинства, договор реже.
NOT_IN_EXPORT_TRY = "Попробуйте фамилию с именем или госномер."
#: По номеру имя не определилось. Одной строкой: оператору важно, что делать
#: дальше, а не почему не вышло. Про источник тут не говорится ничего — его
#: молчание и его незнание для оператора одно и то же действие.
NAME_NOT_RESOLVED = "По номеру не определилось. Введите фамилию."

# ---------------------------------------------------------------- меню опций

MENU_HEAD = "Что ещё можно добавить и что это откроет:"
MENU_AFTER_EMPTY = (
    "Фактов по этим данным не пришло. Это не «должник чист»: часть источников по таким "
    "данным не ищет вовсе, часть могла не ответить — что именно, написано в отчёте выше. "
    "Другим ключом можно спросить снова:"
)
MENU_OPENS = "откроет ещё {sources}"


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
        "по нему поднимается ваша запись",
    ),
    _Option(
        Field.PHONE,
        "Телефон",
        "",
        "поднимет запись с ФИО, договором и машиной",
    ),
    _Option(Field.CONTRACT, "Договор", "", "поднимет вашу запись"),
    _Option(Field.ADDRESS, "Адрес", "", "поднимет вашу запись"),
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
        f"ИНН физлица, {INN_INDIVIDUAL_LENGTH} цифр. Откроет банкротство, статус ИП "
        "и арбитраж — больше их ничем не открыть. Десятизначный не подойдёт: он у организаций."
    ),
    Field.PHONE.value: ("Телефон. Поднимает вашу запись о должнике и находит ФИО с документами."),
    Field.AUTO.value: ("Госномер или VIN. VIN откроет залоги даже без даты рождения."),
    Field.FIO.value: "Фамилия, имя и, если есть, отчество — одной строкой или по одному слову.",
    Field.LAST_NAME.value: "Фамилия. Одним словом.",
    Field.FIRST_NAME.value: "Имя. Одним словом.",
    Field.MIDDLE_NAME.value: ("Отчество. Отсекает однофамильцев."),
    Field.CONTRACT.value: ("Номер договора или заявки. Поднимет запись с ФИО, долгом и машиной."),
    Field.ADDRESS.value: "Адрес. Поднимет вашу запись о должнике.",
}

#: Подсказка одна на оба режима хранения. Раньше их было две — с обещанием
#: удалить сообщение и без него, — и различал их ``STORE_SENSITIVE_IDENTIFIERS``.
#: Обещание убрано по прямому указанию владелицы, и вместе с ним исчезла
#: разница: бот сохраняет паспорт в обоих случаях ровно настолько, насколько
#: настроено развёртывание, и молчать об этом честнее, чем обещать обратное.
PASSPORT_HINT = (
    f"Серия и номер паспорта, {PASSPORT_LENGTH} цифр. Сам по себе не открывает ничего — "
    "по нему ФНС выдаёт ИНН, и тогда откроются те же три источника. Запрос платный."
)
PASSPORT_HINT_STORED = PASSPORT_HINT

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
    """Текст карточки. Пока чего-то ждём — только вопрос, и ничего больше.

    Сводка всех полей рядом с вопросом и была тем «полотном»: она печатала
    прочерки полей, которых никто не спрашивал, три одинаковые строки «жду —
    Фамилия Имя Отчество» подряд и телефон вместе с оговоркой, что телефон не
    хранится. Собранное показывается одной строкой сверху, полная карточка — на
    экране без вопроса, где ей и место.
    """
    # Конфликт имён и разбор десяти цифр — тоже вопросы, но со своими
    # вариантами ответа, поэтому у каждого свой короткий текст.
    if conflict is not None:
        return "\n".join(
            [
                CONFLICT.format(current=_current_name(card), incoming=conflict.full),
                CONFLICT_WHY,
            ]
        )

    if card.awaiting_field == _AWAITING_TEN:
        return "\n".join([TEN_QUESTION.format(digits=card.pending_ten or ""), TEN_WHY])

    pending = card.step.value if card.step is not None else card.awaiting_field
    if pending:
        lines: list[str] = []
        # Одной строкой — то, что уже принято. Без неё оператор набирает фамилию
        # и получает следующий вопрос, ничем не подтверждающий, что предыдущий
        # ответ дошёл.
        collected = _collected(card)
        if collected:
            lines.extend((collected, ""))
        if notice:
            lines.extend((notice, ""))
        skip_label = NEXT_LABEL if card.step is not None else "Пропустить"
        lines.extend(_ask_lines(pending, skip_label=skip_label))
        return "\n".join(lines)

    lines = [_head(card), FORM_LEAD, ""]
    lines.extend(_rows(card))
    lines.append("")
    if notice:
        lines.extend((notice, ""))
    lines.append(DASH_MEANING)
    lines.append("")
    lines.extend(_coverage_lines(card, registry))
    lines.append("")
    lines.extend(_menu_lines(card, registry))
    lines.append(INVITE_AFTER if card.checked_at else INVITE)
    return "\n".join(lines)


def _ask_lines(field_name: str, *, skip_label: str) -> list[str]:
    """Один вопрос: что написать, пример и что можно не писать.

    Экран один и тот же и для трёх основных шагов, и для добора поля кнопкой.
    Раньше это были два разных рендера: шаг спрашивал коротко, а добор поля
    печатал всю сводку карточки плюс абзац-объяснение — «Телефон: +7 (985)
    ***-**-45 — сам номер не храню, пришлите заново», три одинаковые строки
    «жду — Фамилия Имя Отчество» и прочерки полей, которых никто не спрашивал.
    Один вопрос на экран значит один вопрос на экран, из какой бы двери в него
    ни вошли.

    Пример и «пропустить» стоят всегда. Это про доверие: оператор, не знающий,
    можно ли пропустить, впишет что-нибудь наугад — и карточка получит мусор
    вместо прочерка, то есть «не нашли» вместо «не спрашивали».
    """
    prompt = ASK_PROMPT_NUMBER if field_name == Field.PHONE.value else ASK_PROMPT
    lines = [
        f"{ASK_MARK} {ASK_TITLES.get(field_name, 'Значение')}",
        "",
        prompt,
        ASK_EXAMPLE.format(example=ASK_EXAMPLES.get(field_name, "")),
        f"Не знаете — «{skip_label}».",
    ]
    note = ASK_NOTES.get(field_name)
    if note:
        lines.extend(("", note))
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
    if card.last_run_empty:
        lead = MENU_AFTER_EMPTY
    elif card.subject() is None:
        # Пустая карточка — та самая, на которую жаловались: человек прислал
        # номер и получил заголовок, подпись, шесть строк формы, объяснение
        # прочерка, строку про недостающие поля и следом каталог из восьми
        # «+ поле — что оно откроет». Каталог тут лишний дважды: те же восемь
        # полей стоят кнопками прямо под сообщением, и читать их проще там.
        # Пока не спросили ни одного источника, «что это откроет» — вопрос не
        # сегодняшнего дня; что делать дальше, говорит одна строка над кнопками.
        return []
    else:
        # На собранной карточке списка нет вовсе, и это главное сокращение
        # экрана: что заполнено и что нет, видно по галочкам на кнопках, а
        # восемь строк «+ поле — что оно откроет» под ними превращали форму в
        # то самое полотно, на которое жаловались. Список возвращается там, где
        # он отвечает на вопрос: проверка ничего не дала или собирать ещё нечего.
        return []
    options = [line for line in (_option(card, spec, registry) for spec in OPTIONS) if line]
    if not options:
        return []
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
    """Сколько источников откроет это поле — на нынешнем содержимом карточки.

    Числом, а не списком имён, по тому же правилу, что и строка покрытия:
    карточку видит любой допущенный сотрудник, и перечислять на ней, у кого мы
    покупаем данные, незачем. Считается по гейтам самих провайдеров, а не по
    написанному руками списку: захардкоженный разъезжается с провайдером при
    первой правке и начинает обещать источник, который откажется отвечать.
    """
    base = card.subject() or SearchSubject(search_type=SearchType.PERSON.value)
    opened = coverage.unlocked_by(probe, base, registry)
    return _count(len(opened)) if opened else ""


def _head(card: Card) -> str:
    if card.checked_at:
        return f"{FORM_HEAD} — проверено в {format_datetime(card.checked_at)}"
    return FORM_HEAD


def _current_name(card: Card) -> str:
    parts = [card.last_name, card.first_name, card.middle_name]
    return " ".join(part for part in parts if part) or EMPTY


def _collected(card: Card) -> str:
    """Что уже принято — одной строкой, только заполненное.

    Показывается на шагах вместо полной сводки. Полная сводка перечисляет все
    поля разом, включая те, которых никто не спрашивал, и на трёх шагах подряд
    давала три одинаковые строки «жду — Фамилия Имя Отчество». Здесь нужно
    только подтверждение приёма, поэтому пустые поля молчат.
    """
    parts = [
        f"{FIELD_TITLES[name]}: {card.shown(name)}" for name in FIELD_ORDER if card.shown(name)
    ]
    return f"Принял — {', '.join(parts)}" if parts else ""


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
    """«Сейчас спрошу» и «Не спрошу» — числами, а не списком источников.

    Числа посчитаны, а не написаны: разница между «кого спросим сейчас» и «кого
    спросили бы с этим полем» берётся у :mod:`app.services.coverage`.
    Захардкоженный список разъезжается с гейтом провайдера при первой же правке
    и, разъехавшись, обещает источник, который откажется отвечать.

    Имён источников здесь нет намеренно, и это правило владелицы: «не надо про
    это рассказывать всем». Карточка — главный экран, её видит любой допущенный
    сотрудник, и список агрегаторов на нём — это выдача поставщиков наружу.
    Причину нехватки бот при этом называет: она про то, что оператор может
    исправить, а не про то, у кого мы покупаем.

    Пустая карточка не показывает ни того, ни другого: «нужны ФИО или ИНН» под
    пустой формой читается как список неудач, тогда как проверки ещё и не было.
    """
    subject = card.subject()
    if subject is None:
        return [
            "Нужны фамилия с именем — или ИНН, или госномер. Остальное дописывается кнопками ниже."
        ]

    lines: list[str] = []
    answering = coverage.will_answer(subject, registry)
    gaps = coverage.blocked(subject, registry)
    blocked_count = sum(len(names) for names in gaps.values())
    total = len(answering) + blocked_count
    if answering:
        lines.append(f"{_lead(card)}: {_count(len(answering))} из {total}.")
    if gaps:
        reasons = "; ".join(
            f"{_count(len(names))} — {missing_reason(tuple(reason))}"
            for reason, names in gaps.items()
        )
        lines.append(f"{'Не спрошено' if card.checked_at else 'Не спрошу'}: {reasons}.")
        lines.append(NOT_ASKED)
    return lines


def _count(number: int) -> str:
    return f"{number} {pluralize_ru(number, 'источник', 'источника', 'источников')}"


def _lead(card: Card) -> str:
    return "Спросил" if card.checked_at else "Сейчас спрошу"


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
                *_run_row(card),
            ]
        )
    if card.awaiting_field == _AWAITING_TEN:
        return _rows_markup(
            [
                [
                    _button("Паспорт", QC_TEN_PASSPORT),
                    _button("Телефон", QC_TEN_PHONE),
                ],
                [_button("Назад", QC_CANCEL)],
            ]
        )
    if card.step is not None:
        # Ровно одна кнопка, и подпись у неё одна и та же на всех трёх шагах.
        # Второй кнопки здесь нет намеренно: шаг — это один вопрос, и выбор
        # «ответить или пропустить» не должен соревноваться с выбором «а не
        # нажать ли что-нибудь ещё». Меню опций придёт следом само.
        return _rows_markup([[_button(NEXT_LABEL, QC_NEXT)], [_button(BACK_LABEL, MENU_HOME)]])
    if card.awaiting_field:
        # Все ряды схлопываются в один: пока ждём поле, любая другая кнопка
        # означала бы «а нажми-ка вместо ответа что-нибудь ещё».
        return _rows_markup([[_button("Пропустить", QC_SKIP)], [_button("Назад", QC_CANCEL)]])

    return _rows_markup([*_field_rows(card), *_run_row(card)])


def _field_rows(card: Card) -> list[list[InlineKeyboardButton]]:
    """Сетка полей: три кнопки в ряд, позиции постоянные.

    Сотня должников в день делается мышечной памятью, и кнопка, переезжающая
    между рядами по мере заполнения, — это промах пальцем по «Очистить».
    Поэтому ряды фиксированы, включая заполненные поля: заполненное не
    исчезает, а получает галочку и правится там же, где добавлялось.

    Порядок рядов — по тому, как человека называют: сперва имя, потом даты и
    номера, потом всё, что поднимает строку из 1С.
    """
    return [
        [
            _ask(card, Field.LAST_NAME, "Фамилия"),
            _ask(card, Field.FIRST_NAME, "Имя"),
            _ask(card, Field.MIDDLE_NAME, "Отчество"),
        ],
        [
            _ask(card, Field.BIRTH_DATE, "Дата рождения"),
            _ask(card, Field.INN, "ИНН"),
            _ask(card, Field.PHONE, "Телефон"),
        ],
        [
            _ask(card, Field.PASSPORT, "Паспорт"),
            _ask(card, Field.AUTO, "Госномер"),
            _ask(card, Field.CONTRACT, "Договор"),
        ],
        [_ask(card, Field.ADDRESS, "Адрес")],
    ]


def _run_row(card: Card) -> list[list[InlineKeyboardButton]]:
    """Главное действие отдельной строкой, второстепенные — под ним.

    «Проверить» занимает всю ширину и стоит одно: это единственная кнопка,
    которая тратит деньги, и соседство с «Очистить» на одной строке делает её
    промахом пальца.
    """
    run = "Перепроверить" if card.checked_at else "Проверить"
    wipe = "Новая проверка" if card.checked_at else "Очистить"
    return [
        [_button(run, QC_RUN)],
        [_button(wipe, QC_WIPE), _button(BACK_LABEL, MENU_HOME)],
    ]


def _ask(card: Card, field: Field, title: str) -> InlineKeyboardButton:
    """Кнопка поля. Галочка значит «заполнено», её отсутствие — «пусто».

    Подпись при этом не меняется: «✎ Исправить Дата рождения» и «+ Дата
    рождения» — это две разные кнопки на глаз, и глаз ищет их заново на каждом
    экране. Меняется один значок в начале, как в боте-образце.
    """
    mark = f"{FILLED_MARK} " if _is_filled(card, field) else ""
    return _button(f"{mark}{title}", f"{QC_ASK}:{field.value}")


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
