"""Заявки на доступ и решения владельца по ним.

До этой таблицы список допущенных жил только в ``.env``: чтобы пустить человека,
владелице надо было открыть файл на сервере и перезапустить бота, а «открыть
всем» символом «*» было единственной альтернативой. Решение, принятое кнопкой,
обязано пережить перезапуск — иначе перезапуск сам по себе становится способом
вернуть отобранный доступ и обнулить суточную паузу отклонённому.

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "access_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=True),
        sa.Column("full_name", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        # Время хранится текстом ISO-8601 — тот же UtcDateTime, что у остальных
        # таблиц: SQLite не умеет timezone-aware, а наивное время сравнивается
        # с utcnow() неправильно, и суточная пауза поехала бы вместе с ним.
        sa.Column("requested_at", sa.String(length=32), nullable=False),
        sa.Column("decided_at", sa.String(length=32), nullable=True),
        sa.Column("decided_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    # Уникальность по telegram_user_id — не оптимизация, а инвариант: две строки
    # на одного человека означали бы два разных ответа на вопрос «пускать ли».
    op.create_index(
        "ix_access_requests_telegram_user_id",
        "access_requests",
        ["telegram_user_id"],
        unique=True,
    )
    op.create_index("ix_access_requests_status", "access_requests", ["status"])
    op.create_index("ix_access_requests_requested_at", "access_requests", ["requested_at"])


def downgrade() -> None:
    op.drop_table("access_requests")
