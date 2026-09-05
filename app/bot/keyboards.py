"""Inline and reply keyboards.

Callback payloads are short, namespaced strings; anything longer than a few
identifiers goes through the FSM context instead, because Telegram caps callback
data at 64 bytes.
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.domain.enums import REGION_TITLES, Region, SearchType

# ---------------------------------------------------------------- callbacks

MENU_PREFIX = "menu"
REGION_PREFIX = "region"
SKIP_CALLBACK = "skip"
CANCEL_CALLBACK = "cancel"
EXTERNAL_CHECK_PREFIX = "external"
REFRESH_PREFIX = "refresh"
REPEAT_PREFIX = "repeat"
BATCH_PREFIX = "batch"

REGION_COMBINED = "moscow_and_oblast"


def main_menu() -> InlineKeyboardMarkup:
    buttons = [
        # Массовая проверка стоит первой: это главный сценарий продукта,
        # а поиск одного человека — частный случай.
        [InlineKeyboardButton(text="Проверить всю базу", callback_data=f"{BATCH_PREFIX}:start")],
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
