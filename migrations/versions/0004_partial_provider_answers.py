"""Неполный ответ источника: признак и текст оговорки.

Отчёт пересобирается из кэша в течение CACHE_TTL_HOURS. Без этих двух колонок
пересборка теряла бы то, что источник сказал о полноте собственного ответа, и
вчерашнее «в реестре ФНП найдено 13 уведомлений, ни одно не сопоставлено»
сегодня превращалось бы в «залогов не найдено» — вместе с плюсом к оценке
взыскиваемости, который этот ответ снимает. Кэш не имеет права быть добрее
исходного ответа.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "search_results",
        sa.Column("is_partial", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "search_results",
        sa.Column("notes_json", sa.Text(), nullable=False, server_default="[]"),
    )


def downgrade() -> None:
    op.drop_column("search_results", "notes_json")
    op.drop_column("search_results", "is_partial")
