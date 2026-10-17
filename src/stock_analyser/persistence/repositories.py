"""Repositories operate only inside Database read/write transaction scopes."""

from datetime import UTC, datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

from stock_analyser.contracts import Instrument, MarketContext, OperationalStatus, Security
from stock_analyser.persistence import schema as s
from stock_analyser.persistence.records import (
    ArtifactRecord,
    RunRecord,
    SnapshotReference,
    StageRecord,
)


class Repositories:
    def __init__(self, connection):
        self.connection = connection

    def _get(self, table, predicate, model):
        payload = self.connection.scalar(sa.select(table.c.payload).where(predicate))
        if payload is None:
            raise LookupError(f"Record not found in {table.name}")
        return model.model_validate(payload)

    def _immutable(self, table, keys, record, **columns):
        payload = record.model_dump(mode="json")
        self.connection.execute(
            insert(table).values(**keys, **columns, payload=payload).on_conflict_do_nothing()
        )
        predicate = sa.and_(*(table.c[key] == value for key, value in keys.items()))
        if self.connection.scalar(sa.select(table.c.payload).where(predicate)) != payload:
            raise ValueError(f"Conflicting immutable record in {table.name}")

    def put_security(self, security: Security) -> None:
        self._immutable(s.securities, {"security_id": security.security_id}, security)

    def get_security(self, security_id: UUID) -> Security:
        return self._get(s.securities, s.securities.c.security_id == security_id, Security)

    def put_instrument(self, instrument: Instrument) -> None:
        self._immutable(
            s.instruments,
            {"instrument_id": instrument.instrument_id},
            instrument,
            security_id=instrument.security_id,
            market_id=instrument.market_id,
            venue=instrument.venue,
            symbol=instrument.symbol,
            currency=instrument.quote_currency,
        )

    def get_instrument(self, instrument_id: UUID) -> Instrument:
        return self._get(s.instruments, s.instruments.c.instrument_id == instrument_id, Instrument)

    def resolve_instrument(
        self, market_id: str, symbol: str, venue: str | None = None
    ) -> Instrument:
        query = sa.select(s.instruments.c.payload).where(
            s.instruments.c.market_id == market_id,
            s.instruments.c.symbol == symbol,
        )
        if venue is not None:
            query = query.where(s.instruments.c.venue == venue)
        matches = self.connection.scalars(query).all()
        if len(matches) != 1:
            raise ValueError("Instrument is unknown or ambiguous; specify market and venue")
        return Instrument.model_validate(matches[0])

    def put_market(self, market: MarketContext) -> None:
        self._immutable(
            s.market_configurations,
            {"market_id": market.market_id, "version": market.configuration_version},
            market,
        )

    def get_market(self, market_id: str, version: str) -> MarketContext:
        return self._get(
            s.market_configurations,
            sa.and_(
                s.market_configurations.c.market_id == market_id,
                s.market_configurations.c.version == version,
            ),
            MarketContext,
        )

    def create_run(self, run: RunRecord) -> None:
        self.put_market(run.market)
        for instrument_id in run.request.instrument_ids:
            run.market.validate_instrument(self.get_instrument(instrument_id))
        self._immutable(
            s.runs,
            {"run_id": run.run_id},
            run,
            market_id=run.market.market_id,
            market_version=run.market.configuration_version,
            requested_as_of=run.request.requested_as_of,
            status=run.status.value,
        )

    def get_run(self, run_id: UUID) -> RunRecord:
        return self._get(s.runs, s.runs.c.run_id == run_id, RunRecord)

    def set_run_status(
        self,
        run_id: UUID,
        status: OperationalStatus,
        reason: str | None = None,
    ) -> None:
        payload = self.get_run(run_id).model_dump(mode="python")
        payload.update(status=status, reason=reason, updated_at=datetime.now(UTC))
        record = RunRecord.model_validate(payload)
        self.connection.execute(
            s.runs.update()
            .where(s.runs.c.run_id == run_id)
            .values(
                status=record.status.value,
                payload=record.model_dump(mode="json"),
            )
        )

    def _validate_scope(self, run: RunRecord, instrument_id: UUID) -> None:
        run.market.validate_instrument(self.get_instrument(instrument_id))
        if run.request.instrument_ids and instrument_id not in run.request.instrument_ids:
            raise ValueError("Instrument is outside requested run scope")

    def put_stage(self, stage: StageRecord) -> None:
        run = self.get_run(stage.run_id)
        if stage.instrument_id is not None:
            self._validate_scope(run, stage.instrument_id)
        keys = {
            "run_id": stage.run_id,
            "name": stage.name,
            "scope": str(stage.instrument_id) if stage.instrument_id else "",
        }
        statement = insert(s.stages).values(
            **keys,
            instrument_id=stage.instrument_id,
            status=stage.status.value,
            payload=stage.model_dump(mode="json"),
        )
        self.connection.execute(
            statement.on_conflict_do_update(
                index_elements=list(keys),
                set_={"status": statement.excluded.status, "payload": statement.excluded.payload},
            )
        )

    def list_stages(self, run_id: UUID) -> tuple[StageRecord, ...]:
        values = self.connection.scalars(
            sa.select(s.stages.c.payload)
            .where(
                s.stages.c.run_id == run_id,
            )
            .order_by(s.stages.c.name, s.stages.c.scope)
        )
        return tuple(StageRecord.model_validate(value) for value in values)

    def attach_snapshot(self, run_id: UUID, snapshot: SnapshotReference) -> None:
        run = self.get_run(run_id)
        evidence = snapshot.metadata
        self._validate_scope(run, evidence.instrument.instrument_id)
        if evidence.instrument != self.get_instrument(evidence.instrument.instrument_id):
            raise ValueError("Snapshot instrument differs from stored instrument")
        if evidence.market != run.market:
            raise ValueError("Snapshot configuration differs from frozen run configuration")
        if evidence.session_cutoff > run.request.requested_as_of:
            raise ValueError("Snapshot cutoff exceeds requested cutoff")
        self._immutable(
            s.snapshots,
            {"snapshot_id": evidence.snapshot_id},
            snapshot,
            instrument_id=evidence.instrument.instrument_id,
            market_id=evidence.market.market_id,
            market_version=evidence.market.configuration_version,
            session_cutoff=evidence.session_cutoff,
        )
        self.connection.execute(
            insert(s.run_snapshots)
            .values(
                run_id=run_id,
                snapshot_id=evidence.snapshot_id,
            )
            .on_conflict_do_nothing()
        )

    def list_snapshots(self, run_id: UUID) -> tuple[SnapshotReference, ...]:
        values = self.connection.scalars(
            sa.select(s.snapshots.c.payload)
            .join(
                s.run_snapshots,
                s.run_snapshots.c.snapshot_id == s.snapshots.c.snapshot_id,
            )
            .where(s.run_snapshots.c.run_id == run_id)
            .order_by(s.snapshots.c.snapshot_id)
        )
        return tuple(SnapshotReference.model_validate(value) for value in values)

    def put_artifact(self, artifact: ArtifactRecord) -> None:
        self.get_run(artifact.run_id)
        existing = (
            self.connection.execute(
                sa.select(s.artifacts).where(
                    sa.or_(
                        s.artifacts.c.artifact_id == artifact.artifact_id,
                        sa.and_(
                            s.artifacts.c.run_id == artifact.run_id,
                            s.artifacts.c.name == artifact.name,
                        ),
                    )
                )
            )
            .mappings()
            .first()
        )
        if existing and (
            existing["artifact_id"] != artifact.artifact_id
            or existing["run_id"] != artifact.run_id
            or existing["name"] != artifact.name
        ):
            raise ValueError("Artifact identity cannot change")
        statement = insert(s.artifacts).values(
            artifact_id=artifact.artifact_id,
            run_id=artifact.run_id,
            name=artifact.name,
            status=artifact.status,
            payload=artifact.model_dump(mode="json"),
        )
        self.connection.execute(
            statement.on_conflict_do_update(
                index_elements=["artifact_id"],
                set_={"status": statement.excluded.status, "payload": statement.excluded.payload},
            )
        )

    def list_artifacts(self, run_id: UUID) -> tuple[ArtifactRecord, ...]:
        values = self.connection.scalars(
            sa.select(s.artifacts.c.payload)
            .where(
                s.artifacts.c.run_id == run_id,
            )
            .order_by(s.artifacts.c.name)
        )
        return tuple(ArtifactRecord.model_validate(value) for value in values)
