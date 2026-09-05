"""Накопительная карточка запроса.

До этой таблицы каждое сообщение оператора разбиралось изолированно и сразу
уходило в платную проверку. «Клочкова Елена Николаевна» запускала прогон,
следующее сообщение «24 11 1994» было уже другим, ни к чему не привязанным
вводом — и бот честно отвечал, что не понимает, к чему это. Карточка и есть
недостающая память между сообщениями.

Живёт в базе, а не в FSM, по причине из докстринга модели: состояние забрало бы
себе весь свободный текст и сломало бы четыре других сценария ввода.

Паспорта и телефона здесь нет ни колонкой, ни под флагом — только маски. Сами
номера живут в памяти процесса час и переживать перезапуск не должны.

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "query_cards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False),
        sa.Column("chat_id", sa.Integer(), nullable=False),
        sa.Column("card_message_id", sa.Integer(), nullable=True),
        sa.Column("last_name", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=64), nullable=True),
        sa.Column("middle_name", sa.String(length=64), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("inn", sa.String(length=12), nullable=True),
        # Только маски. Полные значения в базу не попадают ни при каком флаге:
        # см. app/db/models.py::QueryCard.
        sa.Column("phone_masked", sa.String(length=32), nullable=True),
        sa.Column("passport_masked", sa.String(length=32), nullable=True),
        sa.Column("plate", sa.String(length=16), nullable=True),
        sa.Column("vin", sa.String(length=17), nullable=True),
        # Договор и адрес ищутся только в нашей выгрузке — и именно она главный
        # источник продукта, а не вспомогательный.
        sa.Column("contract_number", sa.String(length=64), nullable=True),
        sa.Column("address", sa.String(length=256), nullable=True),
        sa.Column("skipped_json", sa.Text(), nullable=False),
        sa.Column("awaiting_field", sa.String(length=16), nullable=True),
        # Идём ли по трём основным шагам: телефон, фамилия, имя.
        sa.Column("guided", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("last_run_hash", sa.String(length=64), nullable=True),
        # Время хранится текстом ISO-8601 — тот же UtcDateTime, что у остальных
        # таблиц: SQLite не умеет timezone-aware.
        sa.Column("checked_at", sa.String(length=32), nullable=True),
        # Последняя проверка не нашла ничего: тогда карточка предлагает меню
        # опций, а не оставляет оператора с пустым отчётом.
        sa.Column("last_run_empty", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        # Пара, а не один chat_id: в группе двое операторов не должны склеиться
        # в одного должника.
        sa.UniqueConstraint("telegram_user_id", "chat_id", name="uq_query_cards_user_chat"),
    )
    op.create_index("ix_query_cards_telegram_user_id", "query_cards", ["telegram_user_id"])


def downgrade() -> None:
    op.drop_table("query_cards")
