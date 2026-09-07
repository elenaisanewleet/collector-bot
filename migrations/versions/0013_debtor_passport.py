"""Паспорт должника из выгрузки — ради ИНН, а не ради отчёта.

Банкротство, статус ИП и арбитраж физлица ищут ТОЛЬКО по двенадцатизначному ИНН
физлица. В выгрузке заказчика ИНН нет ни у одного из 2052 должников, поэтому три
источника из шести молчали по всей базе — и молчали бы всегда.

Паспорт есть у 1474 из них, а мост «паспорт → ИНН» через ФНС написан и включён.
Не хватало ровно одного: импорт выбрасывал колонку, потому что её не было в
схеме. Эта миграция её заводит.

Правило хранения — то же, что у телефона, и не мягче: маска записывается всегда,
сам номер — только при поднятом STORE_SENSITIVE_IDENTIFIERS. Маска мосту
бесполезна, поэтому при выключенном флаге три источника продолжают молчать. Это
осознанный выбор развёртывания, а не умолчание кода: паспорт — самое
чувствительное, что обрабатывает продукт, и включать его хранение молча нельзя.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-07
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("debtors", schema=None) as batch_op:
        batch_op.add_column(sa.Column("passport", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("passport_masked", sa.String(length=32), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("debtors", schema=None) as batch_op:
        batch_op.drop_column("passport_masked")
        batch_op.drop_column("passport")
