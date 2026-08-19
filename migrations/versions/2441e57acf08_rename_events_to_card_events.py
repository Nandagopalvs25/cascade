"""rename events to card_events

Revision ID: 2441e57acf08
Revises: 54bb9b5d5391
Create Date: 2026-08-18 08:35:27.247005

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2441e57acf08'
down_revision: Union[str, Sequence[str], None] = '54bb9b5d5391'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("events", "card_events")
    op.execute("ALTER INDEX IF EXISTS ix_events_trello_action_id RENAME TO ix_card_events_trello_action_id")


def downgrade() -> None:
    op.execute("ALTER INDEX IF EXISTS ix_card_events_trello_action_id RENAME TO ix_events_trello_action_id")
    op.rename_table("card_events", "events")
