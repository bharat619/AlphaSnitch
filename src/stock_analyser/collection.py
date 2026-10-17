"""Freeze raw price evidence before validation; never discard failed input."""

import hashlib
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import AwareDatetime, ValidationError

from stock_analyser.calendars import ResolutionReason, ResolvedHistory
from stock_analyser.contracts import (
    Contract,
    DataAvailability,
    EvidenceMetadata,
    FeedCoverage,
    Instrument,
    MarketContext,
    OperationalStatus,
    PriceBar,
    SourceRecord,
    Text,
)
from stock_analyser.persistence import Database
from stock_analyser.persistence.records import (
    ArtifactRecord,
    RunRecord,
    SnapshotReference,
    StageRecord,
)


class RawPriceResponse(Contract):
    provider: Text
    provider_symbol: Text
    retrieved_at: AwareDatetime
    adjustment: Literal["raw", "split", "total_return"]
    feed: FeedCoverage
    payload: bytes


class RawPriceProvider(Protocol):
    def fetch(
        self, instrument: Instrument, market: MarketContext, resolution: ResolvedHistory
    ) -> RawPriceResponse: ...


class QualityFinding(Contract):
    code: Text
    severity: Literal["warning", "error"] = "error"
    row: int | None = None
    session: date | None = None


class EvidencePacket(Contract):
    metadata: EvidenceMetadata
    resolution: ResolvedHistory
    findings: tuple[QualityFinding, ...]
    bars: tuple[PriceBar, ...]
    raw_reference: Text
    response: dict[str, object]
    source_version: dict[str, object]
    component_versions: dict[str, str]
    quality_policy: Literal["price-quality-1"] = "price-quality-1"


def encode(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def immutable_write(path: Path, content: bytes) -> str:
    """Publish a complete, read-only file without replacing an existing name."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{uuid4()}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o444)
        try:
            os.link(temporary, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ValueError("Conflicting immutable evidence file") from None
    finally:
        temporary.unlink(missing_ok=True)
    return hashlib.sha256(content).hexdigest()


def validate_prices(
    response: RawPriceResponse, resolution: ResolvedHistory
) -> tuple[tuple[PriceBar, ...], tuple[QualityFinding, ...], int]:
    findings = []

    def flag(code, row=None, session=None, severity="error"):
        findings.append(QualityFinding(code=code, row=row, session=session, severity=severity))

    expected = {p.session_end: p for p in resolution.periods}
    for reason in resolution.reasons:
        if reason in (
            ResolutionReason.INSUFFICIENT_HISTORY,
            ResolutionReason.PUBLICATION_UNVERIFIED,
        ):
            flag(reason.value)
    if response.adjustment != resolution.requirement.adjustment:
        flag("ADJUSTMENT_MISMATCH")
    if response.retrieved_at < resolution.requested_as_of:
        flag("RETRIEVED_BEFORE_REQUEST")
    try:
        payload = json.loads(response.payload)
        rows = payload["bars"]
        if not isinstance(rows, list):
            raise ValueError
    except (ValueError, TypeError, KeyError):
        flag("INVALID_PAYLOAD")
        return (), tuple(findings), 0
    bars, sessions = [], []
    for index, row in enumerate(rows):
        try:
            session = date.fromisoformat(row["session"])
        except (ValueError, TypeError, KeyError):
            flag("INVALID_SESSION", index)
            continue
        sessions.append(session)
        if session not in expected:
            flag("UNEXPECTED_SESSION", index, session)
        try:
            stamp = datetime.fromisoformat(row["timestamp"])
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError
            if session not in expected or stamp != expected[session].closes_at:
                raise ValueError
            if stamp > response.retrieved_at:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            flag("INVALID_TIMESTAMP", index, session)
        try:
            bar = PriceBar.model_validate(
                {k: row[k] for k in ("session", "open", "high", "low", "close", "volume")}
            )
        except (ValidationError, TypeError, KeyError):
            flag("INVALID_OHLCV", index, session)
            continue
        if bar.volume == 0:
            flag("ZERO_VOLUME", index, session, "warning")
        bars.append(bar)
    if len(sessions) != len(set(sessions)):
        flag("DUPLICATE_SESSION")
    if sessions != sorted(sessions):
        flag("UNORDERED_SESSIONS")
    missing = set(expected).difference(sessions)
    for session in sorted(missing):
        flag("MISSING_SESSION", session=session)
    if missing:
        flag("STALE_DATA")
    if len(set(expected).intersection(sessions)) < resolution.requirement.required_bars:
        flag("INSUFFICIENT_HISTORY")
    # Conservative heuristic, deliberately not an automatic split repair. Raw
    # splits and adjusted discontinuities both require review before use.
    for previous, current in zip(bars, bars[1:], strict=False):
        ratio = current.open / previous.close
        if ratio <= Decimal("0.6") or ratio >= Decimal("1.6"):
            flag("CORPORATE_ACTION_DISCONTINUITY", session=current.session)
    return tuple(bars), tuple(findings), len(rows)


class CollectionService:
    def __init__(self, database: Database, snapshots_dir: Path):
        self.database = database
        self.snapshots_dir = snapshots_dir.resolve()

    def collect(
        self, run_id: UUID, instrument: Instrument, provider: RawPriceProvider
    ) -> tuple[EvidencePacket, ...]:
        with self.database.write() as repo:
            run = repo.get_run(run_id)
            if run.request.instrument_ids != (instrument.instrument_id,):
                raise ValueError("Collection requires a one-instrument run")
            run.market.validate_instrument(instrument)
            if run.status != OperationalStatus.NOT_RUN or repo.list_snapshots(run_id):
                raise ValueError("Collection already started; create a new run for new evidence")
            repo.set_run_status(run_id, OperationalStatus.RUNNING)
            repo.put_stage(
                StageRecord(
                    run_id=run_id,
                    name="collection",
                    instrument_id=instrument.instrument_id,
                    status=OperationalStatus.RUNNING,
                )
            )
        try:
            return self._collect(run, instrument, provider)
        except Exception:
            # Do not put raw provider errors (which may contain credentials) into state.
            reason = "Collection could not finish; inspect retained evidence and retry in a new run"
            with self.database.write() as repo:
                repo.put_stage(
                    StageRecord(
                        run_id=run_id,
                        name="collection",
                        instrument_id=instrument.instrument_id,
                        status=OperationalStatus.FAILED,
                        reason=reason,
                    )
                )
                repo.set_run_status(run_id, OperationalStatus.FAILED, reason)
            raise

    def _collect(
        self, run: RunRecord, instrument: Instrument, provider: RawPriceProvider
    ) -> tuple[EvidencePacket, ...]:
        run_id = run.run_id
        snapshots = []
        for resolution in run.history_resolutions:
            if resolution.venue != instrument.venue:
                continue
            if not resolution.periods:
                raise ValueError("Collection requires a resolved history window")
            response = provider.fetch(instrument, run.market, resolution)
            route = run.market.route("prices")
            if response.provider != route.provider or response.feed != route.feed:
                raise ValueError("Provider response differs from configured route")
            if any(
                instrument.provider_symbol(response.provider, p.session_end)
                != response.provider_symbol
                for p in resolution.periods
            ):
                raise ValueError("Response symbol differs from effective instrument mapping")
            snapshot_id = uuid4()
            directory = self.snapshots_dir / str(snapshot_id)
            raw_path = directory / "source.json"
            digest = immutable_write(raw_path, response.payload)
            bars, findings, count = validate_prices(response, resolution)
            errors = any(f.severity == "error" for f in findings)
            availability = (
                DataAvailability.UNAVAILABLE
                if not count
                else DataAvailability.PARTIAL
                if errors
                else DataAvailability.AVAILABLE
            )
            metadata = EvidenceMetadata(
                snapshot_id=snapshot_id,
                instrument=instrument,
                market=run.market,
                currency=instrument.quote_currency,
                session_cutoff=resolution.session_cutoff,
                feed=response.feed,
                timeframe=resolution.requirement.timeframe,
                adjustment=resolution.requirement.adjustment,
                availability=availability,
                requested_bars=resolution.requirement.required_bars,
                returned_bars=count,
                sources=(
                    SourceRecord(
                        provider=response.provider,
                        provider_symbol=response.provider_symbol,
                        retrieved_at=response.retrieved_at,
                        reference=str(raw_path),
                        sha256=digest,
                        session_start=resolution.history_start,
                        session_end=resolution.session_date,
                    ),
                ),
                quality_flags=tuple(dict.fromkeys(f.code for f in findings)),
                reason="; ".join(dict.fromkeys(f.code for f in findings if f.severity == "error"))
                or ("No returned bars" if not count else None),
            )
            packet = EvidencePacket(
                metadata=metadata,
                resolution=resolution,
                findings=findings,
                bars=bars,
                raw_reference=str(raw_path),
                response=response.model_dump(mode="json", exclude={"payload"}),
                source_version=run.source_version,
                component_versions={**run.component_versions, "collection": "1"},
            )
            manifest_path = directory / "manifest.json"
            manifest_hash = immutable_write(manifest_path, encode(packet.model_dump(mode="json")))
            with self.database.write() as repo:
                repo.attach_snapshot(
                    run_id,
                    SnapshotReference(
                        metadata=metadata, reference=str(manifest_path), sha256=manifest_hash
                    ),
                )
            snapshots.append(packet)
        required = {h.timeframe for h in run.request.history if h.mandatory}
        successful = {
            p.metadata.timeframe
            for p in snapshots
            if p.metadata.availability == DataAvailability.AVAILABLE
        }
        status = OperationalStatus.SUCCEEDED if required <= successful else OperationalStatus.FAILED
        reason = (
            None if status == OperationalStatus.SUCCEEDED else "Mandatory evidence failed quality"
        )
        manifest_path = self.snapshots_dir / "runs" / str(run_id) / "manifest.json"
        content = encode(
            {
                "frozen_run": run.model_dump(mode="json"),
                "status": status.value,
                "reason": reason,
                "snapshots": [p.model_dump(mode="json") for p in snapshots],
            }
        )
        digest = immutable_write(manifest_path, content)
        with self.database.write() as repo:
            repo.put_artifact(
                ArtifactRecord(
                    artifact_id=uuid4(),
                    run_id=run_id,
                    name="manifest.json",
                    reference=str(manifest_path),
                    sha256=digest,
                    status="SAVED",
                    saved_at=datetime.now(UTC),
                )
            )
            repo.put_stage(
                StageRecord(
                    run_id=run_id,
                    name="collection",
                    instrument_id=instrument.instrument_id,
                    status=status,
                    reason=reason,
                )
            )
            repo.set_run_status(run_id, status, reason)
        return tuple(snapshots)
