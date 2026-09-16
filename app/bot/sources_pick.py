"""Экран «какие источники спрашивать» — вид и клавиатура.

ЗАЧЕМ ОН НУЖЕН. Полная проверка спрашивает все подключённые источники и по
дороге покупает ИНН по паспорту. Владелец попросил обратного: «делать запросы
отдельными (которые не дипсерч) и не получить ИНН, чтобы не расходовать запросы
в NewDB». Обращение стоит 2 ₽; когда проверяешь, починился ли разбор одного
источника, четыре остальных — выброшенные деньги, а на демонстрации у заказчика
— кончившийся баланс.

ЧТО ЭКРАН ОБЯЗАН ПОКАЗАТЬ ДО НАЖАТИЯ. Сколько обращений и сколько рублей стоит
текущий выбор. Это то же правило «платит одна кнопка», которое уже действует в
массовом прогоне: цена называется ДО списания, а не после. Считается она по тем
же ``planned_calls``, по которым считает смету прогон, — вторая формула
разошлась бы с первой, и дешевле оказался бы тот экран, который просто плохо
считает.

ЧЕГО ЗДЕСЬ НЕТ. Моста по телефону (depsearch): без него проверка по номеру
невозможна вообще, у него свой поставщик и свой счёт. Владелец так и сказал —
«которые не дипсерч».
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.bot.keyboards import BACK_LABEL
from app.bot.markup import bold, esc
from app.config import Settings
from app.domain.enums import PROVIDER_TITLES, ProviderName
from app.domain.identity import SearchSubject
from app.domain.source_plan import SourcePlan
from app.providers.base import NO_CONTEXT, BaseProvider
from app.providers.registry import ProviderRegistry
from app.utils.formatting import pluralize_ru
from app.utils.money import format_amount

SP = "sp"
SP_OPEN = f"{SP}:open"
SP_TOGGLE = f"{SP}:t"
SP_INN = f"{SP}:inn"
SP_ALL = f"{SP}:all"
SP_NONE = f"{SP}:none"
SP_DONE = f"{SP}:done"

ON = "✓"
OFF = "○"

TITLE = "Какие источники спрашивать"
LEAD = (
    "Отметьте, что спросить по этому должнику. Снятое не спрашивается вовсе — "
    "в отчёте оно будет помечено «не опрашивался», а не «ничего не найдено»."
)
#: Подпись переключателя ИНН. Одна на оба состояния: меняется значок, а не
#: текст — «Добывать ИНН» и «Не добывать ИНН» на глаз две разные кнопки, и
#: глаз ищет их заново на каждом экране. То же правило, что у кнопок полей
#: карточки (``card_view._ask``).
INN_ON = "Добывать ИНН по паспорту"
INN_NOTE_ON = "ИНН покупается: без него молчат источники, ищущие только по ИНН."
INN_NOTE_OFF = "ИНН не покупается — источники, ищущие только по ИНН, скажут «нужен ИНН»."
NOTHING_PICKED = "Не выбрано ни одного источника: проверка покажет только то, что есть у вас."
FREE = "бесплатно"


def screen(
    *,
    plan: SourcePlan,
    registry: ProviderRegistry,
    subject: SearchSubject,
    settings: Settings,
) -> str:
    """Текст экрана: что выбрано и во что это обойдётся."""
    lines = [bold(esc(TITLE)), esc(LEAD), ""]
    chosen = [provider for provider in _pickable(registry) if plan.includes(provider.name)]
    if chosen:
        lines.append(esc("Спросим: " + ", ".join(_title(item.name) for item in chosen)))
    else:
        lines.append(esc(NOTHING_PICKED))
    lines.append(esc(INN_NOTE_ON if plan.buy_inn else INN_NOTE_OFF))
    lines.extend(("", bold(esc(price_line(plan, registry, subject, settings)))))
    return "\n".join(lines)


def price_line(
    plan: SourcePlan,
    registry: ProviderRegistry,
    subject: SearchSubject,
    settings: Settings,
) -> str:
    """«Стоимость: 3 обращения — до 6 ₽».

    Считается по ``planned_calls`` каждого выбранного источника, то есть по
    тому же счёту, которым пользуется смета массового прогона. Источник,
    которому не хватает данных, планирует ноль обращений и в цену не входит:
    обещать плату за отказ, который случится бесплатно, — врать в ту сторону,
    где оператор откажется от нужной проверки.

    Без тарифа (``PROVIDER_REQUEST_COST`` не задан) строка называет обращения и
    молчит о рублях, как и смета прогона: выдуманная цена хуже отсутствующей.
    """
    calls = planned_calls(plan, registry, subject)
    if not calls:
        return f"Стоимость: {FREE}"
    line = f"Стоимость: {calls} {_requests_noun(calls)}"
    cost = settings.provider_request_cost
    if cost > 0:
        line += f" — до {format_amount(cost * calls)}"
    return line


def planned_calls(plan: SourcePlan, registry: ProviderRegistry, subject: SearchSubject) -> int:
    """Сколько платных обращений сделает этот выбор.

    Мост к ИНН считается отдельным слагаемым: он не член ``external`` и в обход
    этого счёта уходил бы бесплатным в глазах оператора, стоя при этом ровно
    столько же, сколько источник.
    """
    total = sum(
        provider.planned_calls(subject, NO_CONTEXT)
        for provider in registry.external
        if plan.includes(provider.name)
    )
    bridge = registry.inn_bridge
    if plan.buy_inn and bridge is not None:
        total += bridge.planned_calls(subject, NO_CONTEXT)
    return total


def keyboard(*, plan: SourcePlan, registry: ProviderRegistry) -> InlineKeyboardMarkup:
    """Галочка на источник, переключатель ИНН и выходы.

    По две кнопки в ряд: подписи источников длинные, и в три столбца Telegram
    обрезает их до неузнаваемости — «Исполнительные произв…» и «Исполнение»
    выглядят одинаково.
    """
    rows: list[list[InlineKeyboardButton]] = []
    pickable = _pickable(registry)
    for left in range(0, len(pickable), 2):
        rows.append([_toggle(item, plan) for item in pickable[left : left + 2]])
    rows.append([_inn_button(plan)])
    rows.append(
        [
            InlineKeyboardButton(text="Отметить всё", callback_data=SP_ALL),
            InlineKeyboardButton(text="Снять всё", callback_data=SP_NONE),
        ]
    )
    rows.append([InlineKeyboardButton(text=BACK_LABEL, callback_data=SP_DONE)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def toggled(plan: SourcePlan, name: ProviderName, registry: ProviderRegistry) -> SourcePlan:
    """План после нажатия на источник.

    ``sources is None`` значит «все», и первое снятие обязано превратить это в
    явный список без снятого — иначе нажатие не изменило бы ничего.
    """
    current = (
        set(plan.sources)
        if plan.sources is not None
        else {item.name for item in _pickable(registry)}
    )
    if name in current:
        current.discard(name)
    else:
        current.add(name)
    return SourcePlan.only(current, buy_inn=plan.buy_inn)


def all_sources(plan: SourcePlan) -> SourcePlan:
    """Вернуться к «спросить всё», сохранив выбор про ИНН."""
    return SourcePlan(sources=None, buy_inn=plan.buy_inn)


def no_sources(plan: SourcePlan) -> SourcePlan:
    return SourcePlan.only((), buy_inn=plan.buy_inn)


def _pickable(registry: ProviderRegistry) -> list[BaseProvider]:
    """Источники, которые есть смысл выбирать: только подключённые.

    Неподключённый в списке обещал бы ответ, которого не будет, и занимал бы
    место на экране, где каждая кнопка стоит денег.
    """
    return [provider for provider in registry.external if provider.is_configured]


def _toggle(provider: BaseProvider, plan: SourcePlan) -> InlineKeyboardButton:
    mark = ON if plan.includes(provider.name) else OFF
    return InlineKeyboardButton(
        text=f"{mark} {_title(provider.name)}",
        callback_data=f"{SP_TOGGLE}:{provider.name.value}",
    )


def _inn_button(plan: SourcePlan) -> InlineKeyboardButton:
    mark = ON if plan.buy_inn else OFF
    return InlineKeyboardButton(text=f"{mark} {INN_ON}", callback_data=SP_INN)


def _title(name: ProviderName) -> str:
    return PROVIDER_TITLES.get(name, name.value)


def _requests_noun(count: int) -> str:
    return pluralize_ru(count, "обращение", "обращения", "обращений")


__all__ = [
    "SP",
    "SP_ALL",
    "SP_DONE",
    "SP_INN",
    "SP_NONE",
    "SP_OPEN",
    "SP_TOGGLE",
    "all_sources",
    "keyboard",
    "no_sources",
    "planned_calls",
    "price_line",
    "screen",
    "toggled",
]
