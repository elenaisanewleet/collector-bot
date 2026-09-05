"""Что бот показывает в чате.

Отчёт живёт на веб-странице, а в чате остаётся карточка: вердикт, две-три
цифры и кнопка. Так и задумано — в сообщении Telegram нет ни таблиц, ни
навигации, и попытка уместить туда весь отчёт даёт ровно ту простыню, которую
никто не читает.

Эмодзи здесь только на кнопках, где они работают как иконки и помогают
выцепить нужную глазами. В тексте их нет: маркеры списка из смайликов — это
не оформление, а шум.
"""

from __future__ import annotations

from collections.abc import Sequence

from app.domain.enums import (
    MISSING_INPUT_TITLES,
    PROVIDER_TITLES,
    MissingInput,
    SearchType,
)
from app.domain.identity import SearchSubject
from app.domain.models import DebtorReport
from app.domain.verdict import FeeBasis, Verdict, VerdictDecision
from app.services.reporting import DEMO_BANNER, SourceStateCode, source_state
from app.utils.dates import format_date, format_datetime
from app.utils.masking import mask_passport, mask_phone, mask_vin
from app.utils.money import format_amount

# Полоса прогресса рисуется символами: Telegram правит сообщение на месте, и
# меняющаяся полоса читается как движение, а не как новое сообщение.
BAR_WIDTH = 18
BAR_FULL = "▰"
BAR_EMPTY = "▱"

VERDICT_LEAD = {
    Verdict.FILE: "Можно подавать иск",
    Verdict.ORDER: "Можно подавать заявление о судебном приказе",
    Verdict.REVIEW: "Нужна ручная проверка",
    Verdict.DROP: "Подавать не стоит",
}

STAGES = (
    "Ищу во внутренней базе",
    "Опрашиваю источники",
    "Сопоставляю записи",
    "Считаю перспективу",
)


def progress_bar(fraction: float, *, width: int = BAR_WIDTH) -> str:
    filled = max(0, min(width, round(fraction * width)))
    return BAR_FULL * filled + BAR_EMPTY * (width - filled)


def searching(
    stage_index: int, *, subject_name: str, accepted: str | None = None, note: str | None = None
) -> str:
    """Сообщение о ходе проверки. Правится на месте, а не шлётся заново.

    ``accepted`` — что бот понял из строки. Показывается сразу, до результата:
    разбор свободной строки обязан быть виден там же, где начинается ожидание, —
    иначе ошибка распознавания всплывёт только в отчёте, за деньги.

    ``note`` — чего не хватило и что это закрывает. Стоит здесь, а не только под
    отчётом, потому что ничего не стоит и учит: оператор читает её, пока ждёт.
    """
    stage_index = max(0, min(len(STAGES) - 1, stage_index))
    fraction = (stage_index + 1) / (len(STAGES) + 1)
    lines = [f"Проверяю: {subject_name}"]
    if accepted:
        lines.append(accepted)
    lines.extend(("", progress_bar(fraction), STAGES[stage_index] + "…"))
    if note:
        lines.append(note)
    return "\n".join(lines)


def accepted_line(subject: SearchSubject) -> str | None:
    """«Принял: ФИО · дата рождения 15.03.1980».

    Эхо разбора. Оператор пишет одной строкой в любом порядке, бот угадывает по
    форме — и обязан показать, что именно он угадал: «77091234560» (ИНН с
    потерянной цифрой) неотличим от мобильного, и единственное, что спасает от
    молчаливой подмены, — эта строка.

    Паспорт и телефон маскируются: в истории чата им делать нечего. ИНН
    показывается целиком — он и так поедет в источники и напечатается в отчёте.
    """
    if subject.search_type != SearchType.PERSON.value:
        return None
    parts: list[str] = []
    if subject.name:
        parts.append("ФИО")
    if subject.birth_date:
        parts.append(f"дата рождения {format_date(subject.birth_date)}")
    if subject.inn:
        parts.append(f"ИНН {subject.inn}")
    if subject.passport:
        parts.append(f"паспорт {mask_passport(subject.passport)}")
    if subject.phone:
        parts.append(f"телефон {mask_phone(subject.phone)}")
    if subject.vehicle and subject.vehicle.plate:
        parts.append(f"госномер {subject.vehicle.plate}")
    if subject.vehicle and subject.vehicle.vin:
        parts.append(f"VIN {mask_vin(subject.vehicle.vin)}")
    if not parts:
        return None
    return "Принял: " + " · ".join(parts)


def batch_progress(processed: int, total: int, failed: int) -> str:
    fraction = processed / total if total else 0.0
    lines = [
        "Проверяю базу",
        "",
        f"{progress_bar(fraction)}  {round(fraction * 100)}%",
        f"{processed} из {total}",
    ]
    if failed:
        lines.append(f"Не удалось проверить: {failed}")
    return "\n".join(lines)


def report_card(
    report: DebtorReport,
    decision: VerdictDecision,
    *,
    notes: Sequence[str] = (),
    demo_mode: bool = False,
) -> str:
    """Короткая карточка в чат. Подробности — на странице по кнопке.

    ``notes`` — то, что бот отбросил при разборе строки. Стоит рядом с эхом
    «Принял:» и по той же причине: отчёт обязан показывать не только то, что он
    учёл, но и то, чего он не учёл, — иначе «проверено» и «нечем было
    проверить» сливаются в одну бодрую карточку.

    Порядок первых строк — баннер, имя, эхо, оговорки — не произвольный:
    баннер относится ко всему тексту и обязан стоять до него, а эхо и оговорки
    относятся к субъекту и идут за его именем.
    """
    lines: list[str] = []
    if demo_mode:
        # Тот же баннер, что в текстовом отчёте: карточка с выдуманными данными
        # не должна быть неотличима от настоящей проверки.
        lines.extend((DEMO_BANNER, ""))
    lines.append(report.subject.display_name)
    echo = accepted_line(report.subject)
    if echo:
        lines.append(echo)
    lines.extend(notes)
    lines.extend(("", VERDICT_LEAD[decision.verdict], decision.headline, ""))

    if decision.debt_amount is not None:
        lines.append(f"Наш долг: {format_amount(decision.debt_amount)}")
    if decision.state_fee is not None:
        basis = "не платится" if decision.fee_basis is FeeBasis.NONE else "к уплате"
        lines.append(f"Пошлина: {format_amount(decision.state_fee)} — {basis}")

    score = report.recovery_score
    if score is not None:
        lines.append(
            f"Recovery Score: {score.score} / 100, "
            f"уверенность данных {round(score.confidence * 100)}%"
        )

    # Состояние источника определяет общая таблица (``reporting.source_state``);
    # карточка решает только, как сгруппировать и назвать. Плоского списка «Не
    # проверено: ФССП, Авто» здесь быть не должно: три беды с тремя разными
    # действиями оператора он схлопывает в одну строку.
    gaps = _gaps(report)
    if gaps:
        lines.append("")
        lines.extend(gaps)

    if report.from_cache:
        lines.append("")
        lines.append(f"Данные проверки от {format_datetime(report.cached_at)}")

    # Карточка — это выжимка, и она обязана сказать, что за кнопкой лежит
    # остальное. Иначе оператор читает пять строк и считает, что это всё, что
    # система знает о человеке: производства, дела, залоги и связи с юрлицами
    # в чат не помещаются и живут только на странице.
    lines.append("")
    lines.append("По кнопке ниже — всё, что собрано об этом человеке:")
    lines.append("производства, банкротство, залоги, суды, связи с юрлицами.")

    return "\n".join(lines)


def _gaps(report: DebtorReport) -> list[str]:
    """Почему в отчёте пусто — по причинам, а не одним списком.

    «Не проверено» было одной строкой на три разные беды, и оператор не мог
    отличить своё упущение от нашего. Теперь их три, и чинятся они по-разному:

    ``Нечем спросить``  источнику не хватило данных — это чинит оператор, и
                        строка называет, чем именно.
    ``Не подключено``   источника у нас нет — оператор не сделает ничего.
    ``Не ответили``     источник упал — поможет «Обновить».

    Группировка по причине, а не по источнику: «ФССП, Залоги — нужна дата
    рождения» это одно действие, а пять строк подряд про одно и то же читаются
    как пять проблем. Причина сравнивается машинно
    (:attr:`ProviderResult.missing_input`), потому что группировать по подстроке
    сообщения — значит развалить карточку от первой правки формулировки.

    Само состояние источника определяется не здесь: его называет общая таблица
    :func:`app.services.reporting.source_state`, та же, что кормит список
    источников на странице и в текстовом отчёте. Второй разбор статусов в чате
    уже был, и он ровно так и разъезжается: порядок «сначала insufficient, потом
    unavailable» правится в одном месте и забывается в другом. Карточка решает
    только, как сгруппировать и какими словами позвать оператора к действию.
    """
    unqueried: dict[tuple[str, ...], list[str]] = {}
    unspecified: dict[str, list[str]] = {}
    not_configured: list[str] = []
    unavailable: list[str] = []

    for result in report.provider_results:
        state = source_state(result)
        if state.answered:
            continue
        title = PROVIDER_TITLES.get(result.provider, result.provider.value)
        match state.code:
            case SourceStateCode.NOT_CONFIGURED:
                not_configured.append(title)
            case SourceStateCode.INSUFFICIENT:
                if result.missing_input:
                    unqueried.setdefault(tuple(result.missing_input), []).append(title)
                else:
                    # Записи из кэша поля не несут — колонки под него нет. Откат
                    # на текст провайдера: группировка теряется, честность нет.
                    unspecified.setdefault(
                        result.error_message or "нечем было спросить", []
                    ).append(title)
            case _:
                unavailable.append(title)

    lines = [
        f"Нечем спросить: {', '.join(titles)} — {missing_reason(missing)}"
        for missing, titles in unqueried.items()
    ]
    lines.extend(
        f"Нечем спросить: {', '.join(titles)} — {reason}" for reason, titles in unspecified.items()
    )
    if not_configured:
        lines.append(f"Не подключено: {', '.join(not_configured)}")
    if unavailable:
        lines.append(f"Не ответили: {', '.join(unavailable)}")
    return lines


def missing_reason(missing: tuple[str, ...]) -> str:
    """Почему источник не спросили — теми же словами, что и везде.

    Публичная, потому что этих слов теперь два потребителя: карточка отчёта и
    накопительная карточка запроса (:mod:`app.bot.card_view`). Четвёртой
    формулировки «нужна дата рождения» в проекте появиться не должно — от того,
    совпадают ли слова до отчёта и в отчёте, зависит, поверит ли оператор, что
    это про одно и то же.

    Порядок полей — как их назвал провайдер, а не отсортированный.

    «нужно ФИО и нужна дата рождения» читается в том порядке, в каком их
    спрашивают. Неизвестное значение (запись из будущей версии) молча
    выбрасывать нельзя, поэтому оно печатается как есть.
    """
    titles = [
        MISSING_INPUT_TITLES[MissingInput(item)] if item in _KNOWN_MISSING else item
        for item in missing
    ]
    return " и ".join(titles) if titles else "нечем было спросить"


_KNOWN_MISSING = {item.value for item in MissingInput}
