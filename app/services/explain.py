"""Чем именно бот посчитал долг, пошлину и вердикт — словами, для владельца.

Зачем это отдельным экраном. Владелец подписывает решение о пошлине деньгами и
имеет право знать, как оно получено: по какому тарифу сложился долг, по какой
ступени вышла пошлина, где проходит граница «не подавать». Без этого продукт
предлагает заплатить, не объясняя за что, и проверить его нечем.

**Числа здесь посчитаны, а не переписаны.** Тарифы берутся из настроек, ступени
— из той же таблицы, по которой считает :mod:`app.domain.fees`, примеры
прогоняются через настоящие ``claim_fee`` и ``court_order_fee``. Справка,
набранная руками, разъезжается с кодом на первой же правке и начинает врать —
причём врать убедительно, потому что выглядит как документация.

Экран для владельца, а не для оператора: он раскрывает пороги и тариф, то есть
внутреннюю кухню решения. Оператору она не нужна, а конкурентам полезна.
"""

from __future__ import annotations

from decimal import Decimal

from app.config import Settings
from app.domain.fees import COURT_ORDER_FEE_RATIO, FEE_BRACKETS, MAX_FEE, claim_fee, court_order_fee
from app.utils.money import format_amount

__all__ = ["explain_calculation"]

#: Суммы, на которых показываются примеры. Первая — медиана живой выгрузки:
#: большинство должников должны ровно за одну эвакуацию, и владелец должен
#: видеть, как выглядит пошлина именно там, а не только на круглых числах.
_EXAMPLES = (Decimal("5000"), Decimal("25000"), Decimal("100000"), Decimal("300000"))


def explain_calculation(settings: Settings) -> str:
    """Как считается долг, пошлина и вердикт — с нынешними настройками."""
    return "\n".join(
        [
            *_debt_part(settings),
            "",
            *_fee_part(),
            "",
            *_verdict_part(settings),
            "",
            *_examples_part(settings),
        ]
    )


def _debt_part(settings: Settings) -> list[str]:
    tow = settings.tow_fee
    per_day = settings.storage_fee_per_day
    if not tow and not per_day:
        return [
            "СУММА ДОЛГА",
            "Тариф не задан — сумма берётся только из выгрузки.",
            "Где её нет, бот говорит «считать не из чего» и пошлину не выводит.",
        ]
    return [
        "СУММА ДОЛГА",
        "Если в выгрузке есть сумма — берётся она. Документ сильнее расчёта.",
        "Если нет — считается по тарифу, из двух дат:",
        "",
        f"   перемещение          {format_amount(tow)} за каждое задержание",
        f"   хранение             {format_amount(per_day)} за каждые ПОЛНЫЕ сутки",
        "",
        "Неполные сутки не тарифицируются: почасовую оплату отменили.",
        "Машина, простоявшая 10 часов, стоит только перемещения.",
        "",
        "Задержания складываются все. Три эвакуации одного человека —",
        "три перемещения и три хранения, а не одно.",
        "",
        "Посчитанная сумма помечена расчётной и в этом виде доходит до отчёта:",
        "в цену иска идёт документ, а не оценка.",
    ]


def _percent(rate: Decimal) -> str:
    """Ставка в процентах по-русски: «3%», «2,5%», «0,35%».

    ``:g`` на Decimal хвост нулей не убирает — печатает «2.500%» и «0.3500%».
    Экран читает человек, а не отладчик.
    """
    text = format((rate * 100).normalize(), "f")
    if "." in text:
        # Хвост нулей срезается ТОЛЬКО после точки. Без этой проверки «50»
        # превращалось в «5», и экран уверенно объявлял пошлину по приказу
        # десятикратно меньше настоящей.
        text = text.rstrip("0").rstrip(".")
    return text.replace(".", ",") + "%"


def _fee_part() -> list[str]:
    lines = [
        "ГОСПОШЛИНА",
        "Ступени по цене иска (ст. 333.19 НК РФ):",
        "",
    ]
    previous: Decimal | None = None
    for upper, base, rate, over in FEE_BRACKETS:
        edge = (
            "свыше " + format_amount(previous)
            if previous is not None
            else "до " + format_amount(upper or Decimal(0))
        )
        if upper is not None and previous is not None:
            edge = f"{format_amount(previous)} — {format_amount(upper)}"
        elif upper is None:
            edge = f"свыше {format_amount(over)}"
        tail = f" + {_percent(rate)} свыше {format_amount(over)}" if rate else ""
        lines.append(f"   {edge:<28} {format_amount(base)}{tail}")
        previous = upper
    lines.extend(
        [
            "",
            f"Потолок пошлины — {format_amount(MAX_FEE)}.",
            f"Судебный приказ — {_percent(COURT_ORDER_FEE_RATIO)} от пошлины по иску.",
            "Округление до полного рубля.",
        ]
    )
    return lines


def _verdict_part(settings: Settings) -> list[str]:
    return [
        "КАК ВЫБИРАЕТСЯ РЕШЕНИЕ",
        "",
        f"   до {format_amount(Decimal(settings.court_order_max_amount))}"
        " — судебный приказ, пошлина вдвое ниже",
        "   свыше — иск",
        "",
        f"Не подавать, если долг меньше пошлины в {settings.min_debt_to_fee_ratio:g} раза:",
        "процесс не окупается.",
        "",
        "Отдельно проверяется, есть ли препятствия ко взысканию: завершённое",
        "банкротство, оконченные производства, залоги, чужие иски. Найденное",
        "снижает балл и попадает в причины решения.",
    ]


def _examples_part(settings: Settings) -> list[str]:
    lines = ["ПРИМЕРЫ", ""]
    ratio = Decimal(str(settings.min_debt_to_fee_ratio))
    for amount in _EXAMPLES:
        order = court_order_fee(amount)
        claim = claim_fee(amount)
        fee = order if amount <= settings.court_order_max_amount else claim
        share = (fee / amount * 100) if amount else Decimal(0)
        worth = "окупается" if fee and amount >= fee * ratio else "не окупается"
        lines.append(
            f"   долг {format_amount(amount):>12}   пошлина {format_amount(fee):>10}"
            f"   {share:.0f}% — {worth}"
        )
    lines.extend(
        [
            "",
            "Проценты по ст. 395 ГК в цену иска бот не добавляет: их считает",
            "юрист на дату подачи, и от них зависит ступень.",
        ]
    )
    return lines
