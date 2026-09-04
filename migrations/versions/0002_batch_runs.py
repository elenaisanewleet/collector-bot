"""Массовая проверка: прогоны и очередь.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "batch_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("total", sa.Integer(), nullable=False),
        sa.Column("processed", sa.Integer(), nullable=False),
        sa.Column("failed", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.String(length=32), nullable=False),
        sa.Column("finished_at", sa.String(length=32), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_batch_runs_telegram_user_id", "batch_runs", ["telegram_user_id"])
    op.create_index("ix_batch_runs_status", "batch_runs", ["status"])
    op.create_index("ix_batch_runs_started_at", "batch_runs", ["started_at"])

    op.create_table(
        "batch_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_run_id", sa.Integer(), nullable=False),
        sa.Column("debtor_id", sa.Integer(), nullable=False),
        sa.Column("search_request_id", sa.Integer(), nullable=True),
        sa.Column("verdict", sa.String(length=16), nullable=False),
        sa.Column("verdict_order", sa.Integer(), nullable=False),
        sa.Column("headline", sa.String(length=512), nullable=False),
        sa.Column("reasons_json", sa.Text(), nullable=False),
        sa.Column("debt_amount", sa.String(length=32), nullable=True),
        sa.Column("debt_kopecks", sa.Integer(), nullable=False),
        sa.Column("state_fee", sa.String(length=32), nullable=True),
        sa.Column("fee_basis", sa.String(length=16), nullable=False),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("confidence", sa.Integer(), nullable=False),
        sa.Column("error", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(["batch_run_id"], ["batch_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["debtor_id"], ["debtors.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_batch_items_batch_run_id", "batch_items", ["batch_run_id"])
    op.create_index("ix_batch_items_verdict", "batch_items", ["verdict"])
    op.create_index("ix_batch_items_verdict_order", "batch_items", ["verdict_order"])
    op.create_index(
        "ix_batch_items_run_order",
        "batch_items",
        ["batch_run_id", "verdict_order", "debt_kopecks"],
    )


def downgrade() -> None:
    op.drop_table("batch_items")
    op.drop_table("batch_runs")
