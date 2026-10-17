"""Preserve append-only instrument attribute versions alongside current lookups."""

import hashlib
import json

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    versions = op.create_table(
        "instrument_versions",
        sa.Column(
            "instrument_id", sa.Uuid, sa.ForeignKey("instruments.instrument_id"), primary_key=True
        ),
        sa.Column("revision", sa.String(64), primary_key=True),
        sa.Column("payload", JSONB, nullable=False),
    )
    connection = op.get_bind()
    instruments = sa.table("instruments", sa.column("instrument_id"), sa.column("payload", JSONB))
    # Backfill existing identities without modifying any historical snapshot payloads.
    for row in connection.execute(sa.select(instruments)).mappings():
        payload = json.dumps(row["payload"], sort_keys=True, separators=(",", ":"))
        connection.execute(
            versions.insert().values(
                instrument_id=row["instrument_id"],
                revision=hashlib.sha256(payload.encode()).hexdigest(),
                payload=row["payload"],
            )
        )


def downgrade() -> None:
    op.drop_table("instrument_versions")
