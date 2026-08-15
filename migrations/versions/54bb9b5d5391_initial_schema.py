"""initial schema

Revision ID: 54bb9b5d5391
Revises: 
Create Date: 2026-08-15 19:25:30.176188

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '54bb9b5d5391'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('runs',
    sa.Column('id', sa.UUID(), autoincrement=False, nullable=False),
    sa.Column('trello_card_id', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('workload', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('state', postgresql.ENUM('received', 'planning', 'queued', 'running', 'succeeded', 'failed', 'needs_attention', name='run_state'), autoincrement=False, nullable=False),
    sa.Column('spec', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('created_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.Column('finished_at', postgresql.TIMESTAMP(timezone=True), autoincrement=False, nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('runs_pkey')),
    sa.UniqueConstraint('trello_card_id', name=op.f('runs_trello_card_id_key'), postgresql_include=[], postgresql_nulls_not_distinct=False)
    )
    op.create_table('events',
    sa.Column('id', sa.INTEGER(), autoincrement=True, nullable=False),
    sa.Column('run_id', sa.UUID(), autoincrement=False, nullable=True),
    sa.Column('trello_action_id', sa.VARCHAR(), autoincrement=False, nullable=True),
    sa.Column('kind', sa.VARCHAR(), autoincrement=False, nullable=False),
    sa.Column('payload', postgresql.JSONB(astext_type=sa.Text()), autoincrement=False, nullable=False),
    sa.Column('occurred_at', postgresql.TIMESTAMP(timezone=True), server_default=sa.text('now()'), autoincrement=False, nullable=False),
    sa.ForeignKeyConstraint(['run_id'], ['runs.id'], name=op.f('events_run_id_fkey')),
    sa.PrimaryKeyConstraint('id', name=op.f('events_pkey')),
    sa.UniqueConstraint('trello_action_id', name=op.f('events_trello_action_id_key'), postgresql_include=[], postgresql_nulls_not_distinct=False)
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('events')
    op.drop_table('runs')
