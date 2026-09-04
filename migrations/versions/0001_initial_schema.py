"""Initial schema.

Revision ID: 0001
Revises:
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "debtors",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dedup_key", sa.String(length=64), nullable=False),
        sa.Column("external_debtor_id", sa.String(length=64), nullable=True),
        sa.Column("fio", sa.String(length=255), nullable=True),
        sa.Column("fio_normalized", sa.String(length=255), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("phone", sa.String(length=20), nullable=True),
        sa.Column("phone_masked", sa.String(length=32), nullable=True),
        sa.Column("phone_hash", sa.String(length=64), nullable=True),
        sa.Column("contract_number", sa.String(length=64), nullable=True),
        sa.Column("claim_number", sa.String(length=64), nullable=True),
        sa.Column("debt_amount", sa.String(length=32), nullable=True),
        sa.Column("address", sa.String(length=512), nullable=True),
        sa.Column("vehicle_plate", sa.String(length=16), nullable=True),
        sa.Column("vin", sa.String(length=17), nullable=True),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_debtors_dedup_key", "debtors", ["dedup_key"], unique=True)
    op.create_index("ix_debtors_external_debtor_id", "debtors", ["external_debtor_id"])
    op.create_index("ix_debtors_fio", "debtors", ["fio"])
    op.create_index("ix_debtors_fio_normalized", "debtors", ["fio_normalized"])
    op.create_index("ix_debtors_phone_hash", "debtors", ["phone_hash"])
    op.create_index("ix_debtors_contract_number", "debtors", ["contract_number"])
    op.create_index("ix_debtors_claim_number", "debtors", ["claim_number"])
    op.create_index("ix_debtors_vehicle_plate", "debtors", ["vehicle_plate"])
    op.create_index("ix_debtors_vin", "debtors", ["vin"])
    op.create_index("ix_debtors_fio_birth", "debtors", ["fio_normalized", "birth_date"])

    op.create_table(
        "search_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False),
        sa.Column("search_type", sa.String(length=32), nullable=False),
        sa.Column("normalized_query_hash", sa.String(length=64), nullable=False),
        sa.Column("masked_query", sa.String(length=255), nullable=False),
        sa.Column("subject_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_search_requests_telegram_user_id", "search_requests", ["telegram_user_id"]
    )
    op.create_index(
        "ix_search_requests_normalized_query_hash",
        "search_requests",
        ["normalized_query_hash"],
    )
    op.create_index("ix_search_requests_created_at", "search_requests", ["created_at"])
    op.create_index(
        "ix_search_requests_user_created",
        "search_requests",
        ["telegram_user_id", "created_at"],
    )
    op.create_index(
        "ix_search_requests_hash_created",
        "search_requests",
        ["normalized_query_hash", "created_at"],
    )

    op.create_table(
        "search_results",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("search_request_id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("provider_status", sa.String(length=32), nullable=False),
        sa.Column("fetched_at", sa.String(length=32), nullable=False),
        sa.Column("normalized_json", sa.Text(), nullable=False),
        sa.Column("raw_response", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.String(length=512), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["search_request_id"], ["search_requests.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_search_results_search_request_id", "search_results", ["search_request_id"]
    )

    op.create_table(
        "debtor_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("search_request_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("score_confidence", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("factors_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(
            ["search_request_id"], ["search_requests.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("search_request_id", name="uq_report_request"),
    )
    op.create_index(
        "ix_debtor_reports_search_request_id", "debtor_reports", ["search_request_id"]
    )

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("detail", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_telegram_user_id", "audit_events", ["telegram_user_id"])
    op.create_index("ix_audit_events_action", "audit_events", ["action"])
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("debtor_reports")
    op.drop_table("search_results")
    op.drop_table("search_requests")
    op.drop_table("debtors")
