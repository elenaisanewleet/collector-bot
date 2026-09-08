"""Находки по номеру телефона: строка на каждый поиск и своя страница в вебе.

Две вещи одной миграцией, потому что это одна работа.

``query_cards.snils_masked``. Паспорт, ИНН и СНИЛС приезжают одним и тем же
оплаченным ответом моста «телефон → ФИО», но до карточки доезжало только имя с
датой рождения: перенос был написан на два поля. Паспорту колонка уже была,
СНИЛСу — нет.

``phone_lookups``. Владелец вводит номер и получает личность; до сих пор она жила
ровно до следующего должника — карточка это черновик, она стирается. А вопрос
«кого я вообще пробил и кого из них в базе нет» задаётся не по одному человеку,
а по всем сразу, и отвечать на него перепиской нельзя. Отсюда таблица и
отдельная страница «новые клиенты».

Правило хранения — то же, что у ``debtors`` (миграция 0013), и не мягче: маска
пишется всегда, сам документ — только при поднятом ``STORE_SENSITIVE_IDENTIFIERS``.
Радиус здесь даже уже: в ``debtors`` две тысячи человек из выгрузки, здесь —
те, кого владелец пробил руками.

``base_matches`` — сколько строк выгрузки нашлось по фамилии НА МОМЕНТ ПРОВЕРКИ.
Число, а не флаг «новый клиент»: флаг устареет на следующем импорте и будет
врать молча, а число честно остаётся замером своего дня. «Новый клиент» — это
ноль, и считается он при показе.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("query_cards", schema=None) as batch_op:
        batch_op.add_column(sa.Column("snils_masked", sa.String(length=32), nullable=True))

    op.create_table(
        "phone_lookups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_user_id", sa.Integer(), nullable=False, index=True),
        sa.Column("phone_masked", sa.String(length=32), nullable=True),
        sa.Column("last_name", sa.String(length=64), nullable=True),
        sa.Column("first_name", sa.String(length=64), nullable=True),
        sa.Column("middle_name", sa.String(length=64), nullable=True),
        sa.Column("birth_date", sa.Date(), nullable=True),
        sa.Column("inn", sa.String(length=12), nullable=True),
        sa.Column("passport", sa.String(length=16), nullable=True),
        sa.Column("passport_masked", sa.String(length=32), nullable=True),
        sa.Column("snils", sa.String(length=16), nullable=True),
        sa.Column("snils_masked", sa.String(length=32), nullable=True),
        sa.Column("base_matches", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("phone_lookups")
    with op.batch_alter_table("query_cards", schema=None) as batch_op:
        batch_op.drop_column("snils_masked")
