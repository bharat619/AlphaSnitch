"""Immutable sector selection inputs, exclusions and ordered results."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "universe_snapshots",
        sa.Column("snapshot_id", sa.Uuid, primary_key=True),
        sa.Column("payload", JSONB, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("universe_snapshots")
