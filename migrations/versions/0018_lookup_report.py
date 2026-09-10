"""Находка по номеру помнит отчёт, которым она закончилась.

Страница проверок по номеру была тупиком: человека нашли по телефону, проверку
оплатили и провели, отчёт сформировали — а со страницы к нему было не перейти.
Владелица описала это ровно так: «перейти по ФИО я не могу, хотя мы формировали
отчёт, надо сохранять эти проверки и оставлять в веб-интерфейсе».

Хранится идентификатор запроса, а не готовый адрес. Ссылки на отчёты живут
ограниченный срок и отзываются (``/revoke``), и записанный адрес через сутки
показывал бы 404 из собственной таблицы. Запрос же лежит в истории столько,
сколько живёт история, а адрес страницы собирается при показе — подписью от той
ссылки, по которой открыт сам список.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("phone_lookups", schema=None) as batch_op:
        batch_op.add_column(sa.Column("search_request_id", sa.Integer(), nullable=True))
        batch_op.create_index(
            "ix_phone_lookups_search_request_id", ["search_request_id"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("phone_lookups", schema=None) as batch_op:
        batch_op.drop_index("ix_phone_lookups_search_request_id")
        batch_op.drop_column("search_request_id")
