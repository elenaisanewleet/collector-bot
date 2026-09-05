"""Кнопки под карточкой отчёта.

Проверка больше не выпрашивает данные вперёд — она идёт с тем, что дали, и
показывает результат. Недостающее предлагается здесь, после отчёта, и только
тогда, когда оно действительно что-то откроет. Это и есть разница между
допросом и предложением: допрос обязателен и стоит пяти сообщений, предложение
стоит нуля и видно ровно тому, кому оно к месту.

Правило одно и жёсткое: **кнопка показывается, только если она не соврёт**.
«Узнать ИНН по паспорту» без ФИО или без даты рождения обещает проверку,
которой не будет: мост ``passport_fns`` требует все три поля и без них отвечает
``insufficient_query``, не сделав ни одного вызова. Поэтому условие проверяется
не по флагу и не по списку полей, переписанному из провайдера, а прогоном самого
провайдера: :meth:`InnBridgeProvider.will_query` на субъекте с подставленным
паспортом. Провайдер и кнопка не могут разойтись, потому что это один и тот же
код.

Про дубль. «📄 Открыть отчёт», «🔄 Обновить» и «🔍 Новая проверка» повторяют
:func:`app.bot.keyboards.report_keyboard`. Дубль сознательный и временный:
``keyboards.py`` правит другое слияние, и трогать его сейчас — гарантированный
конфликт.
"""

# TODO(merge): свести с keyboards.report_keyboard после слияния ветки веб-UI.

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards import CANCEL_CALLBACK, REFRESH_PREFIX
from app.domain.enums import SearchType
from app.domain.identity import PASSPORT_LENGTH, SearchSubject
from app.providers.identity_bridge import InnBridgeProvider
from app.providers.newdb import individual_inn

#: ``padd:<поле>:<токен субъекта>``. Двадцать семь байт при лимите Telegram в 64.
PERSON_ADD_PREFIX = "padd"

#: Поля, которые предлагается дописать после отчёта.
FIELD_BIRTH_DATE = "birth_date"
FIELD_INN = "inn"
FIELD_PASSPORT = "passport"
FIELD_REGION = "region"

#: «Проверить без даты рождения» — единственный способ уйти с вопроса про дату
#: вперёд, а не назад.
RUN_WITHOUT_DATE = "pskip:birth_date"

#: Ответы на вопрос «паспорт или телефон» про десять цифр с девятки.
TEN_AS_PASSPORT = "pten:passport"
TEN_AS_PHONE = "pten:phone"

#: Паспорт-заглушка, которым проверяется «а если бы паспорт был?». Наружу не
#: уходит никогда: :meth:`will_query` — чистая функция от полей субъекта.
_PROBE_PASSPORT = "0" * PASSPORT_LENGTH


def report_keyboard(
    *,
    url: str | None,
    refresh_token: str | None,
    subject: SearchSubject,
    bridge: InnBridgeProvider | None,
) -> InlineKeyboardMarkup:
    """Клавиатура под карточкой: ссылка, чем добрать проверку, и повтор.

    При полном вводе (ФИО, дата, ИНН) остаются ровно три кнопки — столько же,
    сколько было до правки. Предложения появляются там, где чего-то не хватило,
    и исчезают, как только его дали.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text="📄 Открыть отчёт", url=url)])
    if refresh_token:
        rows.extend(_offers(subject, bridge, token=refresh_token))
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔄 Обновить", callback_data=f"{REFRESH_PREFIX}:{refresh_token}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="🔍 Новая проверка", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _offers(
    subject: SearchSubject, bridge: InnBridgeProvider | None, *, token: str
) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    if subject.search_type != SearchType.PERSON.value:
        return rows

    if subject.birth_date is None and subject.name is not None:
        rows.append([_add(FIELD_BIRTH_DATE, token, "📅 Добавить дату рождения — ФССП и залоги")])
    if individual_inn(subject) is None:
        rows.append([_add(FIELD_INN, token, "➕ Добавить ИНН — банкротство, ИП, арбитраж")])
    if passport_would_help(subject, bridge):
        rows.append([_add(FIELD_PASSPORT, token, "🪪 Узнать ИНН по паспорту — платный запрос")])
    rows.append([_add(FIELD_REGION, token, "📍 Сузить до одного региона (сейчас — все)")])
    return rows


def passport_would_help(subject: SearchSubject, bridge: InnBridgeProvider | None) -> bool:
    """Даст ли паспорт то, чего сейчас не хватает.

    Ровно «добавление паспорта сделает :meth:`will_query` истинным», и никак
    иначе. Флаг вида ``resolves_inn_by_passport`` на провайдерах уже пробовали:
    его никто не проставлял, функция всегда возвращала False, и кнопка не
    появлялась бы вовсе. Здесь же ошибиться нельзя — отвечает сам мост.
    """
    if bridge is None or subject.passport:
        return False
    return bridge.will_query(subject.model_copy(update={"passport": _PROBE_PASSPORT}))


def add_keyboard() -> InlineKeyboardMarkup:
    """Под уточняющим вопросом — только отмена. Пропуска здесь нет: оператор уже
    получил отчёт и пришёл сюда сам."""
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=CANCEL_CALLBACK)]]
    )


def bad_date_keyboard() -> InlineKeyboardMarkup:
    """Дата не разобралась. Уйти вперёд можно, но названной ценой."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Проверить без даты рождения", callback_data=RUN_WITHOUT_DATE
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=CANCEL_CALLBACK)],
        ]
    )


def ten_digits_keyboard() -> InlineKeyboardMarkup:
    """Десять цифр с девятки — паспорт или телефон. Гадать нельзя."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🪪 Паспорт", callback_data=TEN_AS_PASSPORT),
                InlineKeyboardButton(text="📞 Телефон", callback_data=TEN_AS_PHONE),
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=CANCEL_CALLBACK)],
        ]
    )


def _add(field: str, token: str, text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{PERSON_ADD_PREFIX}:{field}:{token}")


__all__ = [
    "FIELD_BIRTH_DATE",
    "FIELD_INN",
    "FIELD_PASSPORT",
    "FIELD_REGION",
    "PERSON_ADD_PREFIX",
    "RUN_WITHOUT_DATE",
    "TEN_AS_PASSPORT",
    "TEN_AS_PHONE",
    "add_keyboard",
    "bad_date_keyboard",
    "passport_would_help",
    "report_keyboard",
    "ten_digits_keyboard",
]
