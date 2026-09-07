"""Inline and reply keyboards.

Callback payloads are short, namespaced strings; anything longer than a few
identifiers goes through the FSM context instead, because Telegram caps callback
data at 64 bytes.

Клавиатур две, и они не конкурируют. Инлайн живёт под конкретным сообщением и
относится к нему: выбор типа проверки, кнопки под отчётом, подтверждение
прогона. Нижняя (:func:`main_reply_keyboard`) висит под полем ввода всегда и
относится к боту целиком — четыре-пять самых частых действий, которые до этого
можно было вызвать только командой со слешем.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import NamedTuple

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.domain.enums import REGION_TITLES, Region, SearchType
from app.utils.formatting import pluralize_ru
from app.utils.money import format_compact_amount

# ---------------------------------------------------------------- callbacks

MENU_PREFIX = "menu"
REGION_PREFIX = "region"
SKIP_CALLBACK = "skip"
CANCEL_CALLBACK = "cancel"
# «Новая проверка» под отчётом. Значение то же, что стояло в кнопке раньше:
# менять его нельзя, иначе кнопки в уже отправленных сообщениях перестанут
# работать во второй раз.
BACK_CALLBACK = f"{MENU_PREFIX}:back"
EXTERNAL_CHECK_PREFIX = "external"
# Заявки на доступ. Значение payload — числовой Telegram ID просящего, а не
# индекс в каком-нибудь списке: кнопка живёт в чате владельца сколько угодно
# долго, и после перезапуска бота «одобрить третью заявку» одобрило бы уже
# другого человека.
ACCESS_PREFIX = "access"
ACCESS_ALLOW = f"{ACCESS_PREFIX}:allow"
ACCESS_DENY = f"{ACCESS_PREFIX}:deny"
ACCESS_REVOKE = f"{ACCESS_PREFIX}:revoke"
REFRESH_PREFIX = "refresh"
# Подтверждение платного повтора. Отдельный префикс, а не флаг в старом:
# кнопки ``refresh:*`` висят в чате под каждым прошлым отчётом, и все они
# обязаны теперь спрашивать, а не списывать.
REFRESH_CONFIRM_PREFIX = "refreshgo"
REPEAT_PREFIX = "repeat"
REPEAT_CONFIRM_PREFIX = "repeatgo"
BATCH_PREFIX = "batch"

REGION_COMBINED = "moscow_and_oblast"

# ------------------------------------------------------- нижняя клавиатура

# Подписи кнопок — они же ключи обработчиков (:mod:`app.bot.handlers.buttons`),
# потому что нажатие нижней кнопки приходит обычным текстовым сообщением: ни
# callback_data, ни какого-либо иного признака «это кнопка» Telegram не шлёт.
#
# Отсюда два правила, которые нельзя нарушать.
#
# 1. Подпись — многословная фраза. Совпадение обработчика точное и по всему
#    тексту целиком, поэтому подпись обязана быть такой, какую никто не введёт
#    как данные: «Проверить человека» не бывает ни фамилией, ни номером
#    договора, ни адресом. Раньше эту роль играли эмодзи в начале подписи, но
#    от них отказались — бот должен выглядеть ненавязчиво, — и держится
#    различение теперь на длине и осмысленности фразы.
# 2. Менять подпись — значит менять ключ. У старых клиентов клавиатура
#    остаётся прежней до следующего /start, и переименованная кнопка на день-два
#    перестанет работать. Если менять, то вместе с приветствием.
BUTTON_MENU = "Главное меню"
BUTTON_BATCH = "Проверить всю базу"
BUTTON_SEARCH = "Проверить человека"
BUTTON_HISTORY = "История проверок"
BUTTON_SOURCES = "Откуда данные"
BUTTON_HELP = "Как это работает"
BUTTON_MORE = "Другие способы поиска"

# Подписи, которые стояли на нижней клавиатуре в прошлых версиях. Обработчики у
# них остаются навсегда, и это не аккуратность, а необходимость: нижняя
# клавиатура живёт на стороне Telegram и меняется только на следующем /start.
# У всех, кто /start после выкладки не нажал (в том числе у заказчика с его
# скриншотом), кнопки остаются старыми — и без этих строк каждое нажатие
# попадало бы в разбор свободного текста и отвечало простынёй «не понял».
LEGACY_BUTTON_BATCH = "📊 Проверить всю базу"
LEGACY_BUTTON_SEARCH = "🔍 Проверить одного"
LEGACY_BUTTON_HISTORY = "🕘 История"
LEGACY_BUTTON_SOURCES = "ℹ️ Откуда данные"
LEGACY_BUTTON_HELP = "❓ Как это работает"

# Нижняя клавиатура — ровно две кнопки, одинаково широкие, в один ряд.
#
# «Главное меню» и «Проверить человека»: вернуться и начать. Всё остальное —
# инлайн, под тем сообщением, к которому относится. Это прямо списано с бота,
# который владелица показала как образец: «две кнопки внизу, простота».
#
# Прогона по всей базе здесь нет намеренно, хотя он и главный по мощности:
# кнопка нужна одному человеку и раз в неделю, а место под пальцем — всем и
# каждый день. Он первой строкой в главном меню у владельца.
REPLY_BUTTONS: tuple[str, ...] = (
    BUTTON_MENU,
    BUTTON_SEARCH,
    BUTTON_BATCH,
    BUTTON_MORE,
    BUTTON_HISTORY,
    BUTTON_SOURCES,
    BUTTON_HELP,
    LEGACY_BUTTON_BATCH,
    LEGACY_BUTTON_SEARCH,
    LEGACY_BUTTON_HISTORY,
    LEGACY_BUTTON_SOURCES,
    LEGACY_BUTTON_HELP,
)

# То же без прогона по базе: он только владельцу. Кнопка, которая отвечает
# «нельзя», — это не забота о безопасности, а обещание, которого бот не держит.
ALLOWED_REPLY_BUTTONS: tuple[str, ...] = tuple(
    label for label in REPLY_BUTTONS if label not in {BUTTON_BATCH, LEGACY_BUTTON_BATCH}
)


def main_reply_keyboard(*, owner: bool) -> ReplyKeyboardMarkup:
    """Постоянная клавиатура под полем ввода: две кнопки в один ряд.

    ``is_persistent=True`` — чтобы Telegram не сворачивал её в иконку: свёрнутая
    клавиатура ничем не лучше её отсутствия, а именно отсутствие и было
    жалобой. ``one_time_keyboard`` не выставляется вовсе (по умолчанию False):
    она обязана пережить нажатие, а не исчезнуть после первого.

    Две кнопки, и обе — про навигацию, а не про справку: «Главное меню» —
    вернуться откуда угодно, «Проверить человека» — начать следующего должника.
    Справочные экраны читают один раз, им место в меню, а не под пальцем.

    Подсказка в поле ввода говорит главное про этот бот: номер можно просто
    написать, ничего перед этим не нажимая.

    ``owner`` пока не меняет состав — обе кнопки видны всем, — но остаётся в
    сигнатуре: у владельца и у сотрудника разные меню, и клавиатура обязана
    уметь различать их без правки всех вызовов.
    """
    del owner  # состав одинаковый; см. докстринг
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BUTTON_MENU), KeyboardButton(text=BUTTON_SEARCH)]],
        resize_keyboard=True,
        is_persistent=True,
        # Не «телефона»: телефона нет ни у одного из должников в выгрузке, а
        # госномер есть почти у всех. Подсказка, зовущая ввести то, чего в базе
        # не бывает, — самый дорогой вид лишнего текста: она уводит в тупик.
        input_field_placeholder="Напишите номер: госномер, телефон, ИНН",
    )


MENU_MORE = f"{MENU_PREFIX}:more"
#: «В меню» — возврат на главный экран. Есть под каждым экраном без исключений:
#: экран без выхода это тупик, из которого человек уходит нажатием /start.
MENU_HOME = f"{MENU_PREFIX}:home"
#: Одна подпись на все «назад», и это дословное требование: её не должны
#: искать глазами заново на каждом экране.
BACK_LABEL = "В меню"
MENU_HISTORY = f"{MENU_PREFIX}:history"


class BaseListing(NamedTuple):
    """Ссылка на веб-список должников и то, что на ней написано.

    Собирается в обработчике (нужны запрос к базе и выпуск ссылки), а сюда
    приходит готовой: клавиатура в базу не ходит.
    """

    url: str
    total: int
    amount: Decimal


def main_menu(*, owner: bool, base: BaseListing | None = None) -> InlineKeyboardMarkup:
    """Главное меню: пять рядов у владельца, три у остальных.

    Порядок — по частоте, а не по мощности. Первой стоит проверка одного
    человека: заказчик начинает с неё, вводит номер и получает сводку. Второй —
    ссылка на весь список: «кто у меня вообще есть» спрашивают не реже, чем
    «проверь этого», и сегодня за этим лезут в 1С.

    Рядов стало меньше вдвое, и не перестановкой: парами стоят кнопки, которые
    и по смыслу пара. Прогон по базе и загрузка выгрузки — обе про базу целиком
    и обе только у владельца; история и редкие способы поиска — обе про «найти
    уже сделанное или найти иначе»; справка и источники — обе про «объясни».
    Прежний столбик из семи кнопок был списком всего, что бот умеет, а меню
    должно быть списком того, зачем сюда пришли.

    Прежний довод против пар — «подписи разной длины, короткая рядом с длинной
    читается как менее важная» — здесь соблюдён: в парах подписи соседней
    длины.

    ``owner`` без значения по умолчанию: забытый аргумент должен ломаться на
    mypy, а не показывать чужую кнопку живому человеку.
    """
    buttons = [[_menu_button("Проверить человека", SearchType.PERSON)]]
    if base is not None:
        # URL-кнопка, а не callback: Telegram открывает её сам, без похода в
        # бота. На приветствии та же ссылка стоит текстом — там места под
        # инлайн-кнопку нет, оно занято нижней клавиатурой.
        noun = pluralize_ru(base.total, "должник", "должника", "должников")
        money = f", {format_compact_amount(base.amount)}" if base.amount else ""
        buttons.append(
            [InlineKeyboardButton(text=f"Вся база — {base.total} {noun}{money}", url=base.url)]
        )
    if owner:
        buttons.append(
            [
                InlineKeyboardButton(
                    text="Проверить всю базу", callback_data=f"{BATCH_PREFIX}:start"
                ),
                InlineKeyboardButton(
                    text="Загрузить выгрузку", callback_data=f"{MENU_PREFIX}:import"
                ),
            ]
        )
    buttons.append(
        [
            InlineKeyboardButton(text="История проверок", callback_data=f"{MENU_PREFIX}:history"),
            InlineKeyboardButton(text="Другие способы поиска", callback_data=MENU_MORE),
        ]
    )
    buttons.append(
        [
            InlineKeyboardButton(text="Откуда данные", callback_data=f"{MENU_PREFIX}:sources"),
            InlineKeyboardButton(text="Как это работает", callback_data=f"{MENU_PREFIX}:help"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def more_menu(*, owner: bool) -> InlineKeyboardMarkup:
    """Способы поиска, когда телефона нет.

    Открывается отдельной кнопкой из главного меню и содержит ровно их: справка,
    история и загрузка выгрузки переехали в само меню, и держать их ещё и здесь
    значило бы иметь по две кнопки на каждое действие — ровно та куча, на
    которую жаловались.

    Загрузка выгрузки — только владельцу: чужой файл, подмешанный в базу, меняет
    решения о взыскании по чужим строкам.
    """
    buttons: list[list[InlineKeyboardButton]] = [
        [_menu_button("Госномер", SearchType.VEHICLE_PLATE), _menu_button("VIN", SearchType.VIN)],
        [
            _menu_button("Автомобиль", SearchType.VEHICLE),
            _menu_button("Адрес", SearchType.ADDRESS),
        ],
        [
            _menu_button("Паспорт", SearchType.PASSPORT),
            _menu_button("Договор или заявка", SearchType.CONTRACT),
        ],
    ]
    if owner:
        buttons.append(
            [InlineKeyboardButton(text="Загрузить выгрузку", callback_data=f"{MENU_PREFIX}:import")]
        )
    buttons.append([InlineKeyboardButton(text=BACK_LABEL, callback_data=MENU_HOME)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _menu_button(text: str, search_type: SearchType) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=f"{MENU_PREFIX}:{search_type.value}")


def skip_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Пропустить", callback_data=SKIP_CALLBACK),
                InlineKeyboardButton(text="Отмена", callback_data=CANCEL_CALLBACK),
            ]
        ]
    )


def cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=CANCEL_CALLBACK)]]
    )


def region_keyboard(token: str) -> InlineKeyboardMarkup:
    """Регионы под отчётом. Токен субъекта едет прямо в ``callback_data``.

    Раньше он лежал в состоянии FSM, а состояний в поиске по человеку больше
    нет: их место заняла карточка запроса, которая сознательно не состояние.
    Класть токен в саму карточку незачем — выбор региона относится к уже
    полученному отчёту, а не к тому, что собирают.

    Payload менять безопасно: клавиатура строится в момент нажатия, старых
    кнопок с этим префиксом в чате не остаётся.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            *(
                [
                    InlineKeyboardButton(
                        text=REGION_TITLES[region],
                        callback_data=f"{REGION_PREFIX}:{region.value}:{token}",
                    )
                ]
                for region in (Region.MOSCOW, Region.MOSCOW_OBLAST)
            ),
            [
                InlineKeyboardButton(
                    text="Москва + МО",
                    callback_data=f"{REGION_PREFIX}:{REGION_COMBINED}:{token}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=REGION_TITLES[Region.OTHER],
                    callback_data=f"{REGION_PREFIX}:{Region.OTHER.value}:{token}",
                )
            ],
        ]
    )


def external_check_keyboard(token: str) -> InlineKeyboardMarkup:
    """Offered after an internal-only hit, so the operator opts into external
    calls explicitly rather than every contract lookup spending API quota."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Проверить по внешним источникам",
                    callback_data=f"{EXTERNAL_CHECK_PREFIX}:{token}",
                )
            ]
        ]
    )


def history_keyboard() -> InlineKeyboardMarkup:
    """Две кнопки под историей: назад и обновить.

    Кнопок «Повторить проверку #N» здесь больше нет. Каждая из них — платный
    прогон мимо кэша, и десять таких кнопок под списком стоят ровно столько,
    сколько по ним нажмут; повтор остался под самим отчётом, где рядом видно,
    что именно повторяется.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=BACK_LABEL, callback_data=MENU_HOME),
                InlineKeyboardButton(text="Обновить", callback_data=MENU_HISTORY),
            ]
        ]
    )


def spend_confirm_keyboard(prefix: str, token: str) -> InlineKeyboardMarkup:
    """Подтверждение перед повторным платным прогоном.

    «Обновить» выглядит как чтение, а стоит как проверка: кнопка идёт мимо
    кэша — в этом её назначение — и каждое нажатие обращается к платным
    источникам заново. Массовый прогон подтверждения требует давно; одиночный
    повтор ничем от него не отличается, кроме масштаба.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, спросить источники заново", callback_data=f"{prefix}:{token}"
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=CANCEL_CALLBACK)],
        ]
    )


def batch_confirm_keyboard(label: str, debtors: int) -> InlineKeyboardMarkup:
    """Прогон тратит платные запросы, поэтому запускается только по подтверждению.

    Подпись приходит снаружи и называет сумму, а не действие: под пальцем у
    оператора в этот момент списание, и подпись обязана говорить о нём.

    ``debtors`` уезжает в callback, и это не украшение. Смета живёт в сообщении,
    сообщения в чате живут вечно, а база меняется: без числа в кнопке вчерашняя
    смета молча запускает сегодняшний прогон на другие деньги.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=f"{BATCH_PREFIX}:run:{debtors}")],
            [InlineKeyboardButton(text="Отмена", callback_data=CANCEL_CALLBACK)],
        ]
    )


def access_decision_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Две кнопки под заявкой на доступ, в чате владельца.

    «Разрешить» стоит первой, но не выделена: решение здесь не бывает
    очевидным, и подталкивать к одному из двух — не дело клавиатуры.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Разрешить", callback_data=f"{ACCESS_ALLOW}:{user_id}"
                ),
                InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"{ACCESS_DENY}:{user_id}"),
            ]
        ]
    )


def access_list_keyboard(
    *, pending: Sequence[tuple[int, str]], approved: Sequence[tuple[int, str]]
) -> InlineKeyboardMarkup | None:
    """Кнопки под списком доступа: решить по ждущим, отозвать у допущенных.

    Подпись несёт имя, потому что кнопка «Отозвать» без имени рядом со списком
    из восьми человек — это лотерея. Список кнопок обрезается вызывающим: у
    Telegram нет жёсткого предела на число рядов, но экран есть у человека.
    """
    rows: list[list[InlineKeyboardButton]] = []
    for user_id, title in pending:
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"✅ Разрешить {title}", callback_data=f"{ACCESS_ALLOW}:{user_id}"
                ),
                InlineKeyboardButton(text="🚫", callback_data=f"{ACCESS_DENY}:{user_id}"),
            ]
        )
    rows.extend(
        [
            InlineKeyboardButton(
                text=f"🚪 Отозвать у {title}", callback_data=f"{ACCESS_REVOKE}:{user_id}"
            )
        ]
        for user_id, title in approved
    )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def batch_running_keyboard(url: str | None) -> InlineKeyboardMarkup | None:
    """Ссылка на очередь под сообщением о прогрессе.

    Очередь заполняется на ходу, и ждать полчаса до конца прогона, чтобы в неё
    заглянуть, незачем: страница сама знает, что прогон ещё идёт, и говорит это.
    """
    if not url:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Очередь (заполняется)", url=url)],
        ]
    )


def batch_result_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Подавать", callback_data=f"{BATCH_PREFIX}:list:file"),
                InlineKeyboardButton(text="Приказ", callback_data=f"{BATCH_PREFIX}:list:order"),
            ],
            [
                InlineKeyboardButton(
                    text="Проверить руками", callback_data=f"{BATCH_PREFIX}:list:review"
                ),
                InlineKeyboardButton(text="Не подавать", callback_data=f"{BATCH_PREFIX}:list:drop"),
            ],
            [
                InlineKeyboardButton(
                    text="Выгрузить таблицей", callback_data=f"{BATCH_PREFIX}:export"
                )
            ],
        ]
    )


def batch_result_keyboard_with_link(
    url: str | None, *, csv_url: str | None = None, print_url: str | None = None
) -> InlineKeyboardMarkup:
    """Та же клавиатура, но ссылкой на веб-очередь первой строкой.

    Таблицу на восемьсот строк в сообщении не показать, поэтому ссылка — это
    главное действие, а фильтры по вердикту остаются как быстрый просмотр.
    """
    base = batch_result_keyboard()
    if not url:
        return base
    rows = [[InlineKeyboardButton(text="Открыть очередь", url=url)]]
    export: list[InlineKeyboardButton] = []
    if print_url:
        export.append(InlineKeyboardButton(text="Печать", url=print_url))
    if csv_url:
        # Файл по ссылке повторяет страницу: ФИО маскировано, даты рождения и
        # госномера нет. Подпись обязана это называть — рядом стоит «Выгрузить
        # таблицей», которая присылает полный файл владельцу в чат, и разницу
        # между ними надо видеть до нажатия, а не после.
        export.append(InlineKeyboardButton(text="Таблицей (без ФИО)", url=csv_url))
    if export:
        rows.append(export)
    rows.extend(base.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)
