"""Freeze raw price evidence before validation; never discard failed input."""

import hashlib
import json
import os
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal, Protocol
from uuid import UUID, uuid4

from pydantic import AwareDatetime, Field, ValidationError, model_serializer

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
    quality_flags: tuple[Text, ...] = ()


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
    cache_refresh: dict[str, object] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def preserve_legacy_packets(self, handler):
        data = handler(self)
        if not self.cache_refresh:
            data.pop("cache_refresh", None)
        return data


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

    for code in response.quality_flags:
        flag(code)
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
        payload = json.loads(response.payload, parse_float=Decimal)
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


class CollectionRetryExhausted(ValueError):
    """The persisted request budget is exhausted; evidence policy needs a new run."""


class CollectionService:
    def __init__(self, database: Database, snapshots_dir: Path, *, cache=None):
        self.database = database
        self.snapshots_dir = snapshots_dir.resolve()
        self.cache = cache

    def _collect_locked(
        self,
        run_id: UUID,
        instrument: Instrument,
        provider: RawPriceProvider | None,
        *,
        cache_mode: Literal["reuse", "refresh", "replay", "off"] = "reuse",
        resume: bool = False,
    ) -> tuple[EvidencePacket, ...]:
        with self.database.write() as repo:
            run = repo.get_run(run_id)
            if run.request.instrument_ids != (instrument.instrument_id,):
                raise ValueError("Collection requires a one-instrument run")
            run.market.validate_instrument(instrument)
            stages = [s for s in repo.list_stages(run_id) if s.name == "collection"]
            previous = stages[0] if stages else None
            if (
                run.status != OperationalStatus.NOT_RUN or repo.list_snapshots(run_id)
            ) and not resume:
                raise ValueError("Collection already started; create a new run for new evidence")
            if previous and (
                previous.frozen_instrument != instrument or previous.cache_mode != cache_mode
            ):
                raise ValueError("Changed identity or evidence policy requires a new run")
            stage = StageRecord(
                run_id=run_id,
                name="collection",
                instrument_id=instrument.instrument_id,
                status=OperationalStatus.RUNNING,
                frozen_instrument=instrument,
                cache_mode=cache_mode,
                attempts=previous.attempts if previous else {},
            )
            if resume and run.status == OperationalStatus.SUCCEEDED:
                return self._saved_packets(repo.list_snapshots(run_id))
            if resume:
                repo.reset_unfinished_report(run_id)
            repo.set_run_status(run_id, OperationalStatus.RUNNING)
            repo.put_stage(stage)
        try:
            if cache_mode not in ("reuse", "refresh", "replay", "off"):
                raise ValueError("Unknown cache mode")
            if cache_mode == "replay" and self.cache is None:
                raise ValueError("Replay requires an evidence cache")
            return self._collect(run, instrument, provider, cache_mode)
        except BaseException as exc:
            cancelled = isinstance(exc, (KeyboardInterrupt, SystemExit))
            status = OperationalStatus.CANCELLED if cancelled else OperationalStatus.FAILED
            reason = (
                "Collection cancelled; unfinished work can be resumed against frozen inputs"
                if cancelled
                else "Collection retry budget exhausted; unfinished work requires a new run"
                if isinstance(exc, CollectionRetryExhausted)
                else run.price_routing.reason
                if run.price_routing is not None and run.price_routing.route is None
                else "Collection could not finish; resume retained evidence with bounded retries"
            )
            with self.database.write() as repo:
                stage = next(s for s in repo.list_stages(run_id) if s.name == "collection")
                repo.put_stage(stage.model_copy(update={"status": status, "reason": reason}))
                repo.set_run_status(run_id, status, reason)
            raise

    def collect(self, run_id, instrument, provider, *, cache_mode="reuse", resume=False):
        with self.database.collection_lock(run_id):
            return self._collect_locked(
                run_id, instrument, provider, cache_mode=cache_mode, resume=resume
            )

    @staticmethod
    def _saved_packets(references):
        from stock_analyser.cache import load_snapshot

        return tuple(load_snapshot(reference) for reference in references)

    def _collect(
        self,
        run: RunRecord,
        instrument: Instrument,
        provider: RawPriceProvider | None,
        cache_mode: str,
    ) -> tuple[EvidencePacket, ...]:
        run_id = run.run_id
        with self.database.read() as repo:
            snapshots = list(self._saved_packets(repo.list_snapshots(run_id)))
        rejected_responses = []
        if run.price_routing is not None and run.price_routing.route is None:
            raise ValueError(run.price_routing.reason)
        route = (
            run.price_routing.route if run.price_routing is not None else run.market.route("prices")
        )
        for resolution in run.history_resolutions:
            if resolution.venue != instrument.venue:
                continue
            if any(p.resolution == resolution for p in snapshots):
                continue
            if not resolution.periods:
                raise ValueError("Collection requires a resolved history window")
            cache = self.cache if cache_mode != "off" else None
            identity = cache.identity(run, instrument, resolution, route) if cache else None
            if cache and cache_mode in ("reuse", "replay"):
                hit = cache.lookup(identity, resolution, cache_mode)
                if hit:
                    reference, packet = hit
                    with self.database.write() as repo:
                        repo.attach_snapshot(run_id, reference)
                    snapshots.append(packet)
                    continue
            with self.database.write() as repo:
                stage = next(s for s in repo.list_stages(run_id) if s.name == "collection")
                key = resolution.requirement.timeframe
                attempts = dict(stage.attempts)
                if attempts.get(key, 0) >= 3:
                    raise CollectionRetryExhausted(
                        "Collection retry budget exhausted; start a new run"
                    )
                attempts[key] = attempts.get(key, 0) + 1
                repo.put_stage(stage.model_copy(update={"attempts": attempts}))
            if provider is None:
                raise ValueError("Collection requires a provider on a cache miss")
            if cache:
                response, retained_sources, lineage = cache.fetch(
                    identity, instrument, run.market, resolution, provider
                )
            else:
                fetch_with_sources = getattr(provider, "fetch_with_sources", None)
                if fetch_with_sources:
                    response, retained_sources = fetch_with_sources(
                        instrument, run.market, resolution
                    )
                else:
                    response = provider.fetch(instrument, run.market, resolution)
                    retained_sources = ()
                lineage = {}
            snapshot_id = uuid4()
            directory = self.snapshots_dir / str(snapshot_id)
            raw_path = directory / "source.json"
            digest = immutable_write(raw_path, response.payload)
            provenance_findings = []
            if response.provider != route.provider:
                provenance_findings.append(QualityFinding(code="PROVIDER_MISMATCH"))
            if response.feed != route.feed:
                provenance_findings.append(QualityFinding(code="FEED_MISMATCH"))
            try:
                symbols_match = all(
                    instrument.provider_symbol(route.provider, p.session_end)
                    == response.provider_symbol
                    for p in resolution.periods
                )
            except ValueError:
                symbols_match = False
            if not symbols_match:
                provenance_findings.append(QualityFinding(code="SYMBOL_MISMATCH"))
            if provenance_findings:
                # Invalid provenance cannot satisfy EvidenceMetadata's trusted-source
                # contract. Retain the actual labels in a separate failed diagnostic.
                diagnostic = {
                    "run_id": str(run_id),
                    "snapshot_id": str(snapshot_id),
                    "status": OperationalStatus.FAILED.value,
                    "instrument": instrument.model_dump(mode="json"),
                    "resolution": resolution.model_dump(mode="json"),
                    "response": response.model_dump(mode="json", exclude={"payload"}),
                    "raw_reference": str(raw_path),
                    "raw_sha256": digest,
                    "findings": [f.model_dump(mode="json") for f in provenance_findings],
                    "source_version": run.source_version,
                    "component_versions": {**run.component_versions, "collection": "1"},
                }
                diagnostic_path = directory / "manifest.json"
                diagnostic_hash = immutable_write(diagnostic_path, encode(diagnostic))
                rejected_responses.append(
                    {
                        "reference": str(diagnostic_path),
                        "sha256": diagnostic_hash,
                        "diagnostic": diagnostic,
                    }
                )
                continue
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
                )
                + retained_sources,
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
                cache_refresh=lineage,
            )
            manifest_path = directory / "manifest.json"
            manifest_hash = immutable_write(manifest_path, encode(packet.model_dump(mode="json")))
            reference = SnapshotReference(
                metadata=metadata, reference=str(manifest_path), sha256=manifest_hash
            )
            with self.database.write() as repo:
                repo.attach_snapshot(run_id, reference)
            if cache:
                cache.commit(identity, reference, packet)
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
        with self.database.read() as repo:
            stage = next(s for s in repo.list_stages(run_id) if s.name == "collection")
        attempt = sum(stage.attempts.values())
        manifest_path = (
            self.snapshots_dir / "runs" / str(run_id) / f"{attempt}-{uuid4()}" / "manifest.json"
        )
        content = encode(
            {
                "frozen_run": run.model_dump(mode="json"),
                "status": status.value,
                "reason": reason,
                "snapshots": [p.model_dump(mode="json") for p in snapshots],
                "rejected_responses": rejected_responses,
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
            repo.put_stage(stage.model_copy(update={"status": status, "reason": reason}))
            repo.set_run_status(run_id, status, reason)
        return tuple(snapshots)
