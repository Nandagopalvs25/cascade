"""add processed_at to card_events

Revision ID: 0084b6d6a4c7
Revises: 2441e57acf08
Create Date: 2026-08-18 10:00:14.685519

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0084b6d6a4c7'
down_revision: Union[str, Sequence[str], None] = '2441e57acf08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "card_events",
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("card_events", "processed_at")
