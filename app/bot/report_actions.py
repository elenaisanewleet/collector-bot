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

from app.bot.keyboards import BACK_CALLBACK, BACK_LABEL, MENU_HOME, REFRESH_PREFIX
from app.domain.enums import SearchType
from app.domain.identity import PASSPORT_LENGTH, SearchSubject
from app.providers.identity_bridge import InnBridgeProvider
from app.utils.formatting import pluralize_ru

#: ``padd:<поле>:<токен субъекта>``. Двадцать семь байт при лимите Telegram в 64.
PERSON_ADD_PREFIX = "padd"

#: Поля, которые предлагается дописать после отчёта.
FIELD_BIRTH_DATE = "birth_date"
FIELD_INN = "inn"
FIELD_PASSPORT = "passport"
FIELD_REGION = "region"
#: «Уточнить данные» — открыть карточку сбора по тому же человеку.
FIELD_REFINE = "refine"

#: Паспорт-заглушка, которым проверяется «а если бы паспорт был?». Наружу не
#: уходит никогда: :meth:`will_query` — чистая функция от полей субъекта.
_PROBE_PASSPORT = "0" * PASSPORT_LENGTH


def report_keyboard(
    *,
    url: str | None,
    refresh_token: str | None,
    subject: SearchSubject,
    bridge: InnBridgeProvider | None,
    records: int | None = None,
    narrowable: bool = False,
) -> InlineKeyboardMarkup:
    """Клавиатура под карточкой: одно главное действие, под ним второстепенные.

    Порядок здесь и есть решение. Первой и во всю ширину — ссылка на отчёт: за
    ней вся таблица, и это то, ради чего проверку запускали. Число записей стоит
    прямо в подписи, потому что оно отвечает на вопрос «а есть ли там что
    смотреть» до нажатия, а не после загрузки страницы.

    Кнопок было семь, стало пять, и убирались не «лишние на глаз».

    «Печать» и «Файлом» вели на ту же веб-страницу, у которой обе ссылки уже
    стоят в шапке (:func:`app.web.render._export_actions`). Два адреса до
    одного и того же места, один под другим, — это не выбор, а шум.

    Сужение региона показывается, только когда оно что-то изменит: регион
    сужает поиск по ФССП, и если производств не нашлось вовсе или регион уже
    выбран, кнопка обещает результат, которого не будет. Это то же правило,
    по которому здесь не показывают «Узнать ИНН по паспорту».

    ``records`` — сколько фактов в отчёте. ``None`` значит «не считали», и тогда
    подпись остаётся без числа: соврать нулём хуже, чем промолчать.
    """
    rows: list[list[InlineKeyboardButton]] = []
    if url:
        rows.append([InlineKeyboardButton(text=_report_label(records), url=url)])
    if refresh_token:
        rows.append(
            [
                # Карточка сбора больше не приезжает под каждый отчёт сама —
                # она за этой кнопкой. Двадцать строк с уже сказанным и
                # тринадцать кнопок под каждым ответом были главной жалобой на
                # бота: «а че опять за херня, че за текста».
                InlineKeyboardButton(
                    text="Уточнить данные",
                    callback_data=f"{PERSON_ADD_PREFIX}:{FIELD_REFINE}:{refresh_token}",
                ),
                InlineKeyboardButton(
                    text="Спросить заново",
                    callback_data=f"{REFRESH_PREFIX}:{refresh_token}",
                ),
            ]
        )
        if narrowable:
            rows.extend(_offers(subject, bridge, token=refresh_token))
    rows.append(
        [
            InlineKeyboardButton(text="Новая проверка", callback_data=BACK_CALLBACK),
            InlineKeyboardButton(text=BACK_LABEL, callback_data=MENU_HOME),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _report_label(records: int | None) -> str:
    if not records:
        return "Открыть отчёт"
    noun = pluralize_ru(records, "запись", "записи", "записей")
    return f"Открыть отчёт ({records} {noun})"


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
    "FIELD_REFINE",
    "FIELD_REGION",
    "PERSON_ADD_PREFIX",
    "passport_would_help",
    "report_keyboard",
]
