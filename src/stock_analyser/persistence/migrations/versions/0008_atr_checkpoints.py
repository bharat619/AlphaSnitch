"""Immutable, evidence-bound fixed-anchor ATR checkpoints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "atr_checkpoints",
        sa.Column("checkpoint_id", sa.String(64), primary_key=True),
        sa.Column("series_sha256", sa.String(64), nullable=False),
        sa.Column(
            "snapshot_id", sa.Uuid, sa.ForeignKey("snapshot_references.snapshot_id"), nullable=False
        ),
        sa.Column("observed_session", sa.Date, nullable=False),
        sa.Column("payload", JSONB, nullable=False),
    )
    op.create_index(
        "ix_atr_checkpoints_series_session",
        "atr_checkpoints",
        ["series_sha256", "observed_session"],
    )


def downgrade():
    op.drop_table("atr_checkpoints")
