"""Полный телефон в карточке и в списке находок.

В обеих таблицах до сих пор жила одна маска. Обоснование было записано в 0016
дословно: «телефон оператор вводит сам и помнит, хранить его незачем». Для
карточки это почти верно — она живёт минуты и стоит перед глазами того, кто
только что набрал номер. Для ``phone_lookups`` неверно совсем: это список новых
клиентов, найденных по номеру, и существует он ровно ради звонка. По
``+7 (987) ***-**-20`` не позвонишь.

Владелица сняла это правило целиком: «зачем мы маскируем телефон, если мы и так
его вводим — вообще нам маскирование особо не нужно в боте, так как это
закрытый бот». Бот действительно закрыт списком допуска, веб-отчёты лежат за
неугадываемыми токенами, а маска в интерфейсе мешала работе, ничего при этом не
защищая: скрывать от человека то, что он сам минуту назад ввёл, — не
приватность.

Правило хранения при этом НЕ смягчается и остаётся общим для всей базы: маска
пишется всегда, сам номер только при поднятом ``STORE_SENSITIVE_IDENTIFIERS``
(на проде поднят). Маска рядом с номером нужна не для симметрии — по ней
интерфейс отличает «было, но не сохранилось» от «не спрашивали», и эта разница
остаётся главным инвариантом продукта.

Revision ID: 0017
Revises: 0016
Create Date: 2026-09-10
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("query_cards", "phone_lookups"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(sa.Column("phone", sa.String(length=20), nullable=True))


def downgrade() -> None:
    for table in ("query_cards", "phone_lookups"):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_column("phone")
