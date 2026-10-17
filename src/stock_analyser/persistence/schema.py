"""Relational identities and searchable fields, with lossless versioned JSON contracts."""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

metadata = sa.MetaData()

securities = sa.Table(
    "securities",
    metadata,
    sa.Column("security_id", sa.Uuid, primary_key=True),
    sa.Column("payload", JSONB, nullable=False),
)
market_configurations = sa.Table(
    "market_configurations",
    metadata,
    sa.Column("market_id", sa.Text, primary_key=True),
    sa.Column("version", sa.Text, primary_key=True),
    sa.Column("payload", JSONB, nullable=False),
)
instruments = sa.Table(
    "instruments",
    metadata,
    sa.Column("instrument_id", sa.Uuid, primary_key=True),
    sa.Column("security_id", sa.Uuid, sa.ForeignKey("securities.security_id"), nullable=False),
    sa.Column("market_id", sa.Text, nullable=False),
    sa.Column("venue", sa.Text, nullable=False),
    sa.Column("symbol", sa.Text, nullable=False),
    sa.Column("currency", sa.String(3), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.Index("ix_instruments_lookup", "market_id", "symbol", "venue"),
)
instrument_versions = sa.Table(
    "instrument_versions",
    metadata,
    sa.Column(
        "instrument_id", sa.Uuid, sa.ForeignKey("instruments.instrument_id"), primary_key=True
    ),
    sa.Column("revision", sa.String(64), primary_key=True),
    sa.Column("payload", JSONB, nullable=False),
)
runs = sa.Table(
    "analysis_runs",
    metadata,
    sa.Column("run_id", sa.Uuid, primary_key=True),
    sa.Column("market_id", sa.Text, nullable=False),
    sa.Column("market_version", sa.Text, nullable=False),
    sa.Column("requested_as_of", sa.DateTime(timezone=True), nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.ForeignKeyConstraint(
        ["market_id", "market_version"],
        ["market_configurations.market_id", "market_configurations.version"],
    ),
    sa.CheckConstraint(
        "status IN ('NOT_RUN','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED')",
        name="ck_run_status",
    ),
)
stages = sa.Table(
    "analysis_stages",
    metadata,
    sa.Column("run_id", sa.Uuid, sa.ForeignKey("analysis_runs.run_id"), primary_key=True),
    sa.Column("name", sa.Text, primary_key=True),
    sa.Column("scope", sa.Text, primary_key=True),
    sa.Column("instrument_id", sa.Uuid, sa.ForeignKey("instruments.instrument_id")),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.CheckConstraint(
        "status IN ('NOT_RUN','RUNNING','SUCCEEDED','PARTIAL','FAILED','CANCELLED')",
        name="ck_stage_status",
    ),
)
snapshots = sa.Table(
    "snapshot_references",
    metadata,
    sa.Column("snapshot_id", sa.Uuid, primary_key=True),
    sa.Column("instrument_id", sa.Uuid, sa.ForeignKey("instruments.instrument_id"), nullable=False),
    sa.Column("market_id", sa.Text, nullable=False),
    sa.Column("market_version", sa.Text, nullable=False),
    sa.Column("session_cutoff", sa.DateTime(timezone=True), nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.ForeignKeyConstraint(
        ["market_id", "market_version"],
        ["market_configurations.market_id", "market_configurations.version"],
    ),
)
run_snapshots = sa.Table(
    "run_snapshots",
    metadata,
    sa.Column("run_id", sa.Uuid, sa.ForeignKey("analysis_runs.run_id"), primary_key=True),
    sa.Column(
        "snapshot_id", sa.Uuid, sa.ForeignKey("snapshot_references.snapshot_id"), primary_key=True
    ),
)
artifacts = sa.Table(
    "report_artifacts",
    metadata,
    sa.Column("artifact_id", sa.Uuid, primary_key=True),
    sa.Column("run_id", sa.Uuid, sa.ForeignKey("analysis_runs.run_id"), nullable=False),
    sa.Column("name", sa.Text, nullable=False),
    sa.Column("status", sa.Text, nullable=False),
    sa.Column("payload", JSONB, nullable=False),
    sa.UniqueConstraint("run_id", "name", name="uq_artifact_run_name"),
    sa.CheckConstraint("status IN ('PENDING','SAVED','FAILED')", name="ck_artifact_status"),
)
report_inputs = sa.Table(
    "report_inputs",
    metadata,
    sa.Column("run_id", sa.Uuid, sa.ForeignKey("analysis_runs.run_id"), primary_key=True),
    sa.Column("payload", JSONB, nullable=False),
)
