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

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from app.domain.enums import REGION_TITLES, Region, SearchType

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
REPEAT_PREFIX = "repeat"
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
BUTTON_BATCH = "Проверить всю базу"
BUTTON_SEARCH = "Проверить человека"
BUTTON_HISTORY = "История проверок"
BUTTON_SOURCES = "Откуда данные"
BUTTON_HELP = "Как это работает"

# Нижняя клавиатура — только две кнопки, и это осознанное сокращение: всё, что
# читают один раз, ушло в меню. Остальные подписи остаются здесь, потому что
# кнопки с ними ещё висят у тех, кто не нажимал /start после правки.
REPLY_BUTTONS: tuple[str, ...] = (
    BUTTON_SEARCH,
    BUTTON_BATCH,
    BUTTON_HISTORY,
    BUTTON_SOURCES,
    BUTTON_HELP,
)

# То же без «Проверить всю базу». Прогон по всей выгрузке доступен только
# владельцу, а кнопка, которая отвечает «нельзя», — это не забота о безопасности,
# а обещание, которого бот не держит: допущенный сотрудник жмёт её первой, она
# стоит верхней.
ALLOWED_REPLY_BUTTONS: tuple[str, ...] = tuple(
    label for label in REPLY_BUTTONS if label != BUTTON_BATCH
)


def main_reply_keyboard(*, owner: bool) -> ReplyKeyboardMarkup:
    """Постоянная клавиатура под полем ввода.

    ``is_persistent=True`` — чтобы Telegram не сворачивал её в иконку: свёрнутая
    клавиатура ничем не лучше её отсутствия, а именно отсутствие и было
    жалобой. ``one_time_keyboard`` не выставляется вовсе (по умолчанию False):
    она обязана пережить нажатие, а не исчезнуть после первого.

    Две кнопки, и то у владельца. Было пять, четыре из них повторяли
    инлайн-меню: на экране одновременно висели две «Истории», две «Откуда
    данные» и две «Как это работает». Именно это и назвали «кучей кнопок».
    Справочные экраны читают один раз, поэтому им место в меню, а не под пальцем.

    Первой стоит проверка одного человека: заказчик начинает с неё, вводит
    телефон и получает отчёт. Прогон по всей базе — второй, он мощнее, но реже,
    и виден только владельцу: кнопка, которая отвечает «нельзя», — обещание,
    которого бот не держит, а нажимают её первой.

    ``owner`` без значения по умолчанию намеренно: забытый аргумент должен
    ломаться на mypy, а не показывать чужую кнопку живому человеку.
    """
    rows = [[KeyboardButton(text=BUTTON_SEARCH)]]
    if owner:
        rows.append([KeyboardButton(text=BUTTON_BATCH)])
    return ReplyKeyboardMarkup(
        keyboard=rows,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Номер телефона должника",
    )


MENU_MORE = f"{MENU_PREFIX}:more"


def main_menu(*, owner: bool) -> InlineKeyboardMarkup:
    """Главное меню: два действия и дверь во всё остальное.

    Было одиннадцать кнопок сразу, и это назвали кучей. Из одиннадцати каждый
    день нажимают две: проверить человека и проверить всю базу. Остальные девять
    — способы поиска на случай, когда телефона нет, — ушли за «Другие способы
    поиска» и никуда не делись.

    Порядок против прежнего: первым идёт человек, а не массовый прогон. Прогон
    мощнее, но заказчик начинает не с него — он вводит телефон должника, который
    только что звонил, и хочет отчёт.

    ``owner`` без значения по умолчанию: забытый аргумент должен ломаться на
    mypy, а не показывать чужую кнопку живому человеку.
    """
    buttons = [[_menu_button("Проверить человека", SearchType.PERSON)]]
    if owner:
        buttons.append(
            [InlineKeyboardButton(text="Проверить всю базу", callback_data=f"{BATCH_PREFIX}:start")]
        )
    buttons.append([InlineKeyboardButton(text="Другие способы поиска", callback_data=MENU_MORE)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def more_menu(*, owner: bool) -> InlineKeyboardMarkup:
    """Всё, что не нужно каждый день.

    Открывается отдельной кнопкой из главного меню. Здесь можно быть подробным:
    сюда приходят, когда телефона нет и надо искать иначе.

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
    fourth = [InlineKeyboardButton(text="История проверок", callback_data=f"{MENU_PREFIX}:history")]
    if owner:
        fourth.insert(
            0,
            InlineKeyboardButton(text="Загрузить выгрузку", callback_data=f"{MENU_PREFIX}:import"),
        )
    buttons.append(fourth)
    buttons.append(
        [
            InlineKeyboardButton(text="Откуда данные", callback_data=f"{MENU_PREFIX}:sources"),
            InlineKeyboardButton(text="Как это работает", callback_data=f"{MENU_PREFIX}:help"),
        ]
    )
    buttons.append([InlineKeyboardButton(text="Назад", callback_data=BACK_CALLBACK)])
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


def history_keyboard(tokens: list[tuple[int, str]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Повторить проверку #{index}",
                    callback_data=f"{REPEAT_PREFIX}:{token}",
                )
            ]
            for index, token in tokens
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
        export.append(InlineKeyboardButton(text="Таблицей", url=csv_url))
    if export:
        rows.append(export)
    rows.extend(base.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)
