"""ИНН должника, оговорки источников, кэш по ключу вендора.

Три независимых изменения, приехавших вместе с четырьмя новыми методами:

*   ``debtors.inn`` — ИНН физлица из выгрузки. Без него ЕГРИП, банкротство и
    арбитраж физлица не запрашиваются вовсе: все три ищут только по ``innfiz``.
*   ``search_results.notes_json`` — оговорки источника о полноте ответа
    («разобрано 10 из 47», «проверено 3 компании из 7»). Хранятся вместе с
    результатом: предел, о котором отчёт умолчал на повторе из кэша, — это то
    же самое умолчание, только отложенное.
*   ``vendor_cache`` — ответы методов, ключ которых не субъект поиска. Цепочка
    по юрлицам вызывает арбитраж по ИНН компании, и без отдельного ключа два
    должника из одного ООО оплачивают один ответ дважды.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("debtors", sa.Column("inn", sa.String(length=12), nullable=True))
    op.create_index("ix_debtors_inn", "debtors", ["inn"])

    op.add_column(
        "search_results",
        sa.Column("notes_json", sa.Text(), nullable=False, server_default="[]"),
    )

    op.create_table(
        "vendor_cache",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cache_key", sa.String(length=128), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_vendor_cache_cache_key", "vendor_cache", ["cache_key"], unique=True)
    op.create_index("ix_vendor_cache_created_at", "vendor_cache", ["created_at"])


def downgrade() -> None:
    op.drop_table("vendor_cache")
    op.drop_column("search_results", "notes_json")
    op.drop_index("ix_debtors_inn", table_name="debtors")
    op.drop_column("debtors", "inn")
