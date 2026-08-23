"""add jobs, decisions, artifacts and run lineage

Revision ID: c3f21a7b4d18
Revises: 0084b6d6a4c7
Create Date: 2026-08-21 09:12:44.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'c3f21a7b4d18'
down_revision: Union[str, Sequence[str], None] = '0084b6d6a4c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


job_state = postgresql.ENUM(
    "submitted", "running", "succeeded", "failed", name="job_state", create_type=False
)


def upgrade() -> None:
    op.add_column("runs", sa.Column("parent_run_id", sa.UUID(), nullable=True))
    op.create_foreign_key(
        op.f("runs_parent_run_id_fkey"), "runs", "runs", ["parent_run_id"], ["id"]
    )

    job_state.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "jobs",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("executor", sa.String(), nullable=False),
        sa.Column("external_id", sa.String(), nullable=True),
        sa.Column("state", job_state, nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column(
            "submitted_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("jobs_run_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("jobs_pkey")),
        sa.UniqueConstraint("run_id", "attempt", name="jobs_run_id_attempt_key"),
    )

    op.create_table(
        "decisions",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("agent", sa.String(), nullable=False),
        sa.Column("decision_kind", sa.String(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("decisions_run_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("decisions_pkey")),
    )

    op.create_table(
        "artifacts",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("gcs_uri", sa.String(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("artifacts_run_id_fkey")),
        sa.PrimaryKeyConstraint("id", name=op.f("artifacts_pkey")),
    )


def downgrade() -> None:
    op.drop_table("artifacts")
    op.drop_table("decisions")
    op.drop_table("jobs")
    job_state.drop(op.get_bind(), checkfirst=True)
    op.drop_constraint(op.f("runs_parent_run_id_fkey"), "runs", type_="foreignkey")
    op.drop_column("runs", "parent_run_id")
