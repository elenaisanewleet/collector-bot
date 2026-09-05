"""Ссылки на веб-отчёты.

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
    op.create_table(
        "share_links",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("expires_at", sa.String(length=32), nullable=False),
        sa.Column("opened_count", sa.Integer(), nullable=False),
        sa.Column("last_opened_at", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_share_links_token", "share_links", ["token"], unique=True)
    op.create_index("ix_share_links_kind", "share_links", ["kind"])
    op.create_index("ix_share_links_target_id", "share_links", ["target_id"])
    op.create_index("ix_share_links_telegram_user_id", "share_links", ["telegram_user_id"])
    op.create_index("ix_share_links_created_at", "share_links", ["created_at"])
    op.create_index("ix_share_links_expires_at", "share_links", ["expires_at"])


def downgrade() -> None:
    op.drop_table("share_links")
