"""Дата выдачи паспорта — там же, где сам паспорт.

Поставщик отдаёт её тем же ответом, что ФИО, дату рождения, ИНН, паспорт и
СНИЛС, и владелице она нужна там же, где они: в заявлении паспорт указывают
целиком, а не одной серией с номером.

Ключом поиска она не является и не станет: ФНС ищет ИНН по серии и номеру и
даты не спрашивает (``identity_bridge.missing_input_for``). Поэтому в
``search_requests`` она не едет — вычёркивается вместе с паспортом
(``redact_subject``).

Хранится как есть, а не маской, и это не послабление к паспорту. Опознаёт
человека НОМЕР, а он по-прежнему живёт только в памяти процесса и попадает в
базу лишь при поднятом ``STORE_SENSITIVE_IDENTIFIERS``. Дата без номера не
опознаёт никого, а маска над датой ничего бы не скрыла — только завела бы ещё
один формат.

Revision ID: 0015
Revises: 0014
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("query_cards", "phone_lookups"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("passport_issued", sa.Date(), nullable=True))


def downgrade() -> None:
    for table in ("query_cards", "phone_lookups"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("passport_issued")
