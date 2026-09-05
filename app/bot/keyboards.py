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
# 1. Эмодзи — часть подписи, а не украшение. Совпадение обработчика точное, и
#    держится оно именно на эмодзи: «📊 Проверить всю базу» не наберёт руками
#    никто, а «Проверить всю базу» — вполне, и такой текст обязан достаться
#    сценарию, в котором человек находится, а не кнопке.
# 2. Менять подпись — значит менять ключ. У старых клиентов клавиатура
#    остаётся прежней до следующего /start, и переименованная кнопка на день-два
#    перестанет работать. Если менять, то вместе с приветствием.
BUTTON_BATCH = "📊 Проверить всю базу"
BUTTON_SEARCH = "🔍 Проверить одного"
BUTTON_HISTORY = "🕘 История"
BUTTON_SOURCES = "ℹ️ Откуда данные"
BUTTON_HELP = "❓ Как это работает"

# Порядок тот же, что в клавиатуре, и он же порядок частоты: массовый прогон —
# главный сценарий продукта, одиночная проверка — частный случай, дальше то,
# что читают, а не запускают.
REPLY_BUTTONS: tuple[str, ...] = (
    BUTTON_BATCH,
    BUTTON_SEARCH,
    BUTTON_HISTORY,
    BUTTON_SOURCES,
    BUTTON_HELP,
)


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура под полем ввода.

    ``is_persistent=True`` — чтобы Telegram не сворачивал её в иконку: свёрнутая
    клавиатура ничем не лучше её отсутствия, а именно отсутствие и было
    жалобой. ``one_time_keyboard`` не выставляется вовсе (по умолчанию False):
    она обязана пережить нажатие, а не исчезнуть после первого.

    Пять кнопок в три ряда, а не десять пунктов инлайн-меню: нижняя клавиатура
    занимает место на экране всегда, поэтому в ней только то, что нажимают
    каждый день. Всё остальное — импорт, госномер, VIN, адрес, паспорт —
    осталось в инлайн-меню за «🔍 Проверить одного».
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BUTTON_BATCH)],
            [KeyboardButton(text=BUTTON_SEARCH), KeyboardButton(text=BUTTON_HISTORY)],
            [KeyboardButton(text=BUTTON_SOURCES), KeyboardButton(text=BUTTON_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Нажмите кнопку внизу или введите команду",
    )


def main_menu() -> InlineKeyboardMarkup:
    buttons = [
        # Массовая проверка стоит первой: это главный сценарий продукта,
        # а поиск одного человека — частный случай.
        [InlineKeyboardButton(text="📊 Проверить всю базу", callback_data=f"{BATCH_PREFIX}:start")],
        [_menu_button("👤 Физлицо", SearchType.PERSON)],
        [
            _menu_button("🚘 Госномер", SearchType.VEHICLE_PLATE),
            _menu_button("🔢 VIN", SearchType.VIN),
        ],
        [
            _menu_button("🚗 Автомобиль", SearchType.VEHICLE),
            _menu_button("📍 Адрес", SearchType.ADDRESS),
        ],
        [
            _menu_button("🪪 Паспорт", SearchType.PASSPORT),
            _menu_button("📄 Договор / заявка", SearchType.CONTRACT),
        ],
        [
            InlineKeyboardButton(text="📥 Импорт CSV", callback_data=f"{MENU_PREFIX}:import"),
            InlineKeyboardButton(text="🕘 История", callback_data=f"{MENU_PREFIX}:history"),
        ],
        # Последним рядом — то, что читают, а не запускают. До этих двух экранов
        # раньше можно было добраться только командой, и владелица описала это
        # как «у нас только через слеш».
        [
            InlineKeyboardButton(text="ℹ️ Откуда данные", callback_data=f"{MENU_PREFIX}:sources"),
            InlineKeyboardButton(text="❓ Как это работает", callback_data=f"{MENU_PREFIX}:help"),
        ],
    ]
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


def region_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=REGION_TITLES[Region.MOSCOW],
                    callback_data=f"{REGION_PREFIX}:{Region.MOSCOW.value}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=REGION_TITLES[Region.MOSCOW_OBLAST],
                    callback_data=f"{REGION_PREFIX}:{Region.MOSCOW_OBLAST.value}",
                )
            ],
            [
                InlineKeyboardButton(
                    text="Москва + МО",
                    callback_data=f"{REGION_PREFIX}:{REGION_COMBINED}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=REGION_TITLES[Region.OTHER],
                    callback_data=f"{REGION_PREFIX}:{Region.OTHER.value}",
                )
            ],
            [InlineKeyboardButton(text="Отмена", callback_data=CANCEL_CALLBACK)],
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


def batch_confirm_keyboard() -> InlineKeyboardMarkup:
    """Прогон тратит платные запросы, поэтому запускается только по подтверждению."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Запустить проверку", callback_data=f"{BATCH_PREFIX}:run")],
            [InlineKeyboardButton(text="Отмена", callback_data=CANCEL_CALLBACK)],
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
                    text="📥 Выгрузить в CSV", callback_data=f"{BATCH_PREFIX}:export"
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
    rows = [[InlineKeyboardButton(text="📊 Открыть очередь", url=url)]]
    export: list[InlineKeyboardButton] = []
    if print_url:
        export.append(InlineKeyboardButton(text="🖨 PDF / печать", url=print_url))
    if csv_url:
        export.append(InlineKeyboardButton(text="⬇️ Таблицей", url=csv_url))
    if export:
        rows.append(export)
    rows.extend(base.inline_keyboard)
    return InlineKeyboardMarkup(inline_keyboard=rows)
