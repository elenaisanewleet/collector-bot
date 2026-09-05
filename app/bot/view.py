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

from app.domain.models import DebtorReport
from app.domain.verdict import FeeBasis, Verdict, VerdictDecision
from app.services.reporting import DEMO_BANNER, unchecked_titles
from app.utils.dates import format_datetime
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


def searching(stage_index: int, *, subject_name: str) -> str:
    """Сообщение о ходе проверки. Правится на месте, а не шлётся заново."""
    stage_index = max(0, min(len(STAGES) - 1, stage_index))
    fraction = (stage_index + 1) / (len(STAGES) + 1)
    lines = [
        f"Проверяю: {subject_name}",
        "",
        progress_bar(fraction),
        STAGES[stage_index] + "…",
    ]
    return "\n".join(lines)


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


def report_card(report: DebtorReport, decision: VerdictDecision, *, demo_mode: bool = False) -> str:
    """Короткая карточка в чат. Подробности — на странице по кнопке."""
    lines: list[str] = []
    if demo_mode:
        # Тот же баннер, что в текстовом отчёте: карточка с выдуманными данными
        # не должна быть неотличима от настоящей проверки.
        lines.extend((DEMO_BANNER, ""))
    lines += [
        report.subject.display_name,
        "",
        VERDICT_LEAD[decision.verdict],
        decision.headline,
        "",
    ]

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

    # Названия непроверенных источников берутся из общего места: строка «не
    # проверено» в чате и на странице обязана совпадать слово в слово.
    unchecked = unchecked_titles(report)
    if unchecked:
        lines.append("")
        lines.append(f"Не проверено: {', '.join(unchecked)}")

    if report.from_cache:
        lines.append("")
        lines.append(f"Данные проверки от {format_datetime(report.cached_at)}")

    return "\n".join(lines)
