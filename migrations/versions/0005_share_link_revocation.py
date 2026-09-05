"""Отзыв ссылок на веб-отчёты.

До этой колонки утёкшую ссылку нельзя было закрыть ничем, кроме ручного DELETE:
она жила до истечения TTL, а перевыпуск возвращал тот же самый адрес.

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("share_links", sa.Column("revoked_at", sa.String(length=32), nullable=True))


def downgrade() -> None:
    op.drop_column("share_links", "revoked_at")
