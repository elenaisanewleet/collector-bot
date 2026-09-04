"""Вердикт: платить ли госпошлину.

Recovery Score отвечает на вопрос «насколько долг выглядит взыскиваемым».
Пользователю нужен другой ответ — что делать с конкретным должником прямо
сейчас, когда таких должников восемьсот. Вердикт переводит отчёт в одно из
четырёх действий и всегда несёт причину.

Порядок правил важен и зафиксирован в :mod:`app.services.verdict`: сначала
факты, которые закрывают вопрос (банкротство), затем нехватка данных, и только
потом экономика.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import ProviderName


class Verdict(StrEnum):
    FILE = "file"
    ORDER = "order"
    REVIEW = "review"
    DROP = "drop"


VERDICT_TITLES: dict[Verdict, str] = {
    Verdict.FILE: "Подавать иск",
    Verdict.ORDER: "Судебный приказ",
    Verdict.REVIEW: "Проверить руками",
    Verdict.DROP: "Не подавать",
}

# Порядок в очереди: сначала то, что можно нести в суд сегодня.
VERDICT_ORDER: dict[Verdict, int] = {
    Verdict.FILE: 0,
    Verdict.ORDER: 1,
    Verdict.REVIEW: 2,
    Verdict.DROP: 3,
}


class FeeBasis(StrEnum):
    CLAIM = "claim"
    COURT_ORDER = "court_order"
    NONE = "none"


FEE_BASIS_TITLES: dict[FeeBasis, str] = {
    FeeBasis.CLAIM: "исковое заявление",
    FeeBasis.COURT_ORDER: "судебный приказ",
    FeeBasis.NONE: "пошлина не платится",
}


class VerdictReason(BaseModel):
    """Одна названная причина. Вердикт без причин не показывается."""

    model_config = ConfigDict(frozen=True)

    code: str
    text: str
    source: ProviderName | None = None


class VerdictDecision(BaseModel):
    model_config = ConfigDict(frozen=True)

    verdict: Verdict
    headline: str
    reasons: tuple[VerdictReason, ...] = Field(default_factory=tuple)
    debt_amount: Decimal | None = None
    state_fee: Decimal | None = None
    fee_basis: FeeBasis = FeeBasis.NONE
    # Переносится из RecoveryScore: вердикт на неполных данных — тоже неполный.
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)

    @property
    def title(self) -> str:
        return VERDICT_TITLES[self.verdict]

    @property
    def is_actionable(self) -> bool:
        """Можно нести в суд без дополнительной ручной проверки."""
        return self.verdict in {Verdict.FILE, Verdict.ORDER}

    @property
    def sort_key(self) -> int:
        return VERDICT_ORDER[self.verdict]
