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

Про дубль. Его больше нет: временная копия в ``keyboards.report_keyboard``
жила только до слияния ветки веб-UI и удалена вместе с ним. Ссылка, ряд
выгрузки, «Обновить» и «Новая проверка» собираются здесь и только здесь —
двух клавиатур под одной карточкой быть не должно, они разъезжаются первой же
переименованной кнопкой.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards import REFRESH_PREFIX
from app.domain.enums import SearchType
from app.domain.identity import PASSPORT_LENGTH, SearchSubject
from app.providers.identity_bridge import InnBridgeProvider

#: ``padd:<поле>:<токен субъекта>``. Двадцать семь байт при лимите Telegram в 64.
PERSON_ADD_PREFIX = "padd"

#: Поля, которые предлагается дописать после отчёта.
FIELD_BIRTH_DATE = "birth_date"
FIELD_INN = "inn"
FIELD_PASSPORT = "passport"
FIELD_REGION = "region"

#: Паспорт-заглушка, которым проверяется «а если бы паспорт был?». Наружу не
#: уходит никогда: :meth:`will_query` — чистая функция от полей субъекта.
_PROBE_PASSPORT = "0" * PASSPORT_LENGTH


def report_keyboard(
    *,
    url: str | None,
    refresh_token: str | None,
    subject: SearchSubject,
    bridge: InnBridgeProvider | None,
    text_url: str | None = None,
    print_url: str | None = None,
) -> InlineKeyboardMarkup:
    """Клавиатура под карточкой: ссылка, выгрузка, чем добрать проверку, и повтор.

    При полном вводе (ФИО, дата, ИНН) остаются ровно три кнопки — столько же,
    сколько было до правки. Предложения появляются там, где чего-то не хватило,
    и исчезают, как только его дали.

    Ссылка идёт первой и отдельной строкой: за ней вся таблица, и это главное
    действие. Выгрузка стоит сразу за ней — отчёт чаще печатают и подшивают,
    чем дочитывают до конца.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text="Полный отчёт", url=url)])
    export: list[InlineKeyboardButton] = []
    if print_url:
        export.append(InlineKeyboardButton(text="Печать", url=print_url))
    if text_url:
        export.append(InlineKeyboardButton(text="Текстом", url=text_url))
    if export:
        rows.append(export)
    if refresh_token:
        rows.extend(_offers(subject, bridge, token=refresh_token))
        rows.append(
            [
                InlineKeyboardButton(
                    text="Обновить", callback_data=f"{REFRESH_PREFIX}:{refresh_token}"
                )
            ]
        )
    rows.append([InlineKeyboardButton(text="Новая проверка", callback_data="menu:back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _offers(
    subject: SearchSubject, bridge: InnBridgeProvider | None, *, token: str
) -> list[list[InlineKeyboardButton]]:
    """Что предложить под отчётом сверх того, что уже предлагает карточка.

    Предложений осталось одно, и это сокращение — суть правки. Добор ИНН, даты
    рождения и паспорта переехал в карточку запроса, которая теперь стоит
    сразу под отчётом: два ряда кнопок про одно и то же, один под другим, —
    это ровно та «сложновато», от которой карточку и заводили.

    Регион остаётся здесь, потому что он единственный относится к УЖЕ
    полученному отчёту, а не к тому, что собирают: сузить область поиска можно
    только после того, как увидел, сколько нашлось по всем.

    Старые кнопки ``padd:*`` под отчётами, отправленными до карточки,
    продолжают работать — обработчик их не удалён, он вливает субъект в
    карточку. Кнопка живёт в чате бесконечно, и молчащая кнопка хуже
    отсутствующей.
    """
    if subject.search_type != SearchType.PERSON.value:
        return []
    return [[_add(FIELD_REGION, token, "Сузить до одного региона")]]


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


def _add(field: str, token: str, text: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{PERSON_ADD_PREFIX}:{field}:{token}")


__all__ = [
    "FIELD_BIRTH_DATE",
    "FIELD_INN",
    "FIELD_PASSPORT",
    "FIELD_REGION",
    "PERSON_ADD_PREFIX",
    "passport_would_help",
    "report_keyboard",
]
