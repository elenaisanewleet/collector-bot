"""Паспорт и СНИЛС переживают перезапуск бота.

До сих пор в ``query_cards`` их не было ни под каким флагом: карточка
задумывалась черновиком на несколько минут, документы жили час в памяти
процесса, а в базу ехали только маски. Владелица это отменила дословно: «надо
убрать это, нам надо наоборот сохранять эти номера».

И она права, потому что продукт стал другим. Раньше бот документы только
ПРИНИМАЛ на вход — не хранить чужой паспорт было чистой выгодой. Теперь он их
НАХОДИТ по номеру телефона, платит за каждое обращение и обязан показать
найденное: заявление в суд подают с серией и номером. Черновик, который при
перезапуске теряет оплаченное, заставляет платить второй раз за то же самое.

Правило хранения — общее для всей базы и не мягче: маска пишется всегда, сам
документ только при поднятом ``STORE_SENSITIVE_IDENTIFIERS`` (на проде поднят).
Маска остаётся рядом с номером не для симметрии: по ней карточка отличает
«было, но не сохранилось» от «не спрашивали», а эта разница — главный инвариант
продукта.

Колонки ``phone`` здесь по-прежнему нет. Телефон оператор вводит сам и помнит,
хранить его незачем.

Revision ID: 0016
Revises: 0015
Create Date: 2026-09-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("query_cards", schema=None) as batch_op:
        batch_op.add_column(sa.Column("passport", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("snils", sa.String(length=16), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("query_cards", schema=None) as batch_op:
        batch_op.drop_column("snils")
        batch_op.drop_column("passport")
