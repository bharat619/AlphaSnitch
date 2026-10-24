"""Immutable confirmed execution ledger and receipts; positions replay FIFO."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "executions",
        sa.Column("sequence", sa.BigInteger, sa.Identity(), primary_key=True),
        sa.Column("execution_id", sa.Uuid, nullable=False, unique=True),
        sa.Column(
            "instrument_id", sa.Uuid, sa.ForeignKey("instruments.instrument_id"), nullable=False
        ),
        sa.Column("idempotency_key", sa.Text, nullable=False, unique=True),
        sa.Column("payload", JSONB, nullable=False),
    )


def downgrade():
    op.drop_table("executions")
