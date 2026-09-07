"""Ссылка назад, в систему заказчика.

Выгрузка приходит с колонкой «ИД» — номером записи о задержании. Ключом
дедупликации он быть не может: один должник стоит в выгрузке до пяти раз с
разными ИД, и взять его ключом значило бы разбить 2052 человека обратно на 2631
эпизод и оплатить 579 лишних проверок одних и тех же людей.

Но и выбрасывать его нельзя. Это единственная ссылка из отчёта в учётную систему
заказчика — по ней оператор находит эпизод, — и это тот ключ, по которому мы
свяжем суммы долга, когда их выгрузят отдельной колонкой. Сейчас суммы в
выгрузке нет вовсе, и без неё вердикт по всем должникам звучит «считать не из
чего».

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("debtors", schema=None) as batch_op:
        batch_op.add_column(sa.Column("source_record_ids", sa.String(length=512), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("debtors", schema=None) as batch_op:
        batch_op.drop_column("source_record_ids")
