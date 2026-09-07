"""Долг, посчитанный по тарифу, и даты, из которых он посчитан.

Выгрузка взыскателя-эвакуатора приходит без суммы долга: в учёте её нет, она
считается по тарифу. Без суммы вердикт по каждому должнику звучит «цену иска и
пошлину посчитать не из чего» — то есть продукт не отвечает на свой
единственный вопрос, а очередь из двух тысяч строк выглядит одинаково пустой.

Считается то же, что взыскатель и выставляет: перемещение плюс хранение за
полные сутки (почасовую оплату отменили, неполные сутки не тарифицируются).

Признак ``debt_is_estimated`` едет вместе с суммой до самого отчёта. Расчётная
сумма не имеет права выглядеть подтверждённой: в цену иска идёт документ, а не
оценка, и это то же правило проекта — «не спрашивали» не равно «не нашли», —
только про деньги.

Обе даты хранятся, чтобы пересчитать другим тарифом, не перезаливая выгрузку.

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("debtors", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("debt_is_estimated", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(sa.Column("impounded_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("released_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("debtors", schema=None) as batch_op:
        batch_op.drop_column("released_at")
        batch_op.drop_column("impounded_at")
        batch_op.drop_column("debt_is_estimated")
