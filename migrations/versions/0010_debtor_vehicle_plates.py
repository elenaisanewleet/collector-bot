"""Все машины должника, а не одна.

Выгрузка взыскателя-эвакуатора — это список задержаний, а не список людей: один
и тот же должник встречается в ней до пяти раз, каждый раз с другой машиной.
Схлопывать такие строки в одного должника правильно — платная проверка человека
нужна одна, — но до этой колонки при схлопывании выживал только последний
госномер, а остальные исчезали молча. Именно из них складывается требование, с
которым идут в суд.

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("debtors", schema=None) as batch_op:
        batch_op.add_column(sa.Column("vehicle_plates", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("debtors", schema=None) as batch_op:
        batch_op.drop_column("vehicle_plates")
