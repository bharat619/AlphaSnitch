"""Freeze report inputs independently of report filesystem writes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_inputs",
        sa.Column("run_id", sa.Uuid, sa.ForeignKey("analysis_runs.run_id"), primary_key=True),
        sa.Column("payload", JSONB, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("report_inputs")
