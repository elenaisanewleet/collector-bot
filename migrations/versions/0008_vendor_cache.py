"""Кэш ответов, ключ которых — не субъект поиска.

Кэш отчёта строится по человеку, и для цепочки по юрлицам этого мало: два
должника из одного ООО оплатили бы один и тот же ответ дважды, а на выгрузке из
одного холдинга — кратно. Здесь ключ — то, по чему реально шёл вызов
(``newdb:arbitr_legal:<ИНН компании>``).

Колонки ``debtors.inn`` и ``search_results.notes_json`` в исходной редакции
этой миграции тоже были — их успели добавить ``0007`` и ``0004``, поэтому здесь
осталось только то, чего ещё нет.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
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
    op.drop_index("ix_vendor_cache_created_at", table_name="vendor_cache")
    op.drop_index("ix_vendor_cache_cache_key", table_name="vendor_cache")
    op.drop_table("vendor_cache")
