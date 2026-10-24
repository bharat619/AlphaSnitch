"""Retain typed discovery/event evidence before report publication."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "research_records",
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("analysis_runs.run_id"), primary_key=True),
        sa.Column(
            "instrument_id", sa.Uuid, sa.ForeignKey("instruments.instrument_id"), primary_key=True
        ),
        sa.Column("payload", JSONB, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("research_records")
