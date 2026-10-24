"""Append-only corrections and corporate actions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ledger_events",
        sa.Column("sequence", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("event_id", sa.Uuid, nullable=False, unique=True),
        sa.Column(
            "instrument_id", sa.Uuid, sa.ForeignKey("instruments.instrument_id"), nullable=False
        ),
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
        sa.Column("payload", JSONB, nullable=False),
    )


def downgrade():
    op.drop_table("ledger_events")
