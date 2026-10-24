"""Versioned, immutable cache commits. Uncommitted files are never cache hits."""

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import AwareDatetime, Field

from stock_analyser.collection import (
    EvidencePacket,
    RawPriceResponse,
    encode,
    immutable_write,
    validate_prices,
)
from stock_analyser.contracts import Contract, HistoryRequirement, SourceRecord
from stock_analyser.persistence.records import SnapshotReference


class CacheMode(StrEnum):
    REUSE = "reuse"
    REFRESH = "refresh"
    REPLAY = "replay"
    OFF = "off"


class CacheMiss(ValueError):
    pass


class CachePolicy(Contract):
    overlap_periods: Annotated[int, Field(strict=True, ge=1)] = 5
    price_ttl_seconds: Annotated[int, Field(strict=True, ge=0)] = 86400
    metadata_ttl_seconds: Annotated[int, Field(strict=True, ge=0)] = 86400
    version: Literal["evidence-cache-1"] = "evidence-cache-1"


class CacheEntry(Contract):
    identity: dict[str, object]
    snapshot: SnapshotReference
    committed_at: AwareDatetime
    checked_at: AwareDatetime


class MetadataEntry(Contract):
    identity: dict[str, object]
    value: dict[str, object]
    observed_at: AwareDatetime


def digest(value: object) -> str:
    return hashlib.sha256(encode(value)).hexdigest()


def verified(reference: str, sha256: str) -> bytes:
    content = Path(reference).read_bytes()
    if hashlib.sha256(content).hexdigest() != sha256:
        raise ValueError("Cache evidence hash mismatch")
    return content


def load_snapshot(reference: SnapshotReference) -> EvidencePacket:
    """Verify the entire source chain, including inputs retained by a refresh."""
    packet = EvidencePacket.model_validate_json(verified(reference.reference, reference.sha256))
    if packet.metadata != reference.metadata:
        raise ValueError("Cache snapshot metadata mismatch")
    for source in packet.metadata.sources:
        verified(source.reference, source.sha256)
    if packet.cache_refresh:
        verified(packet.cache_refresh["parent_manifest"], packet.cache_refresh["parent_sha256"])
    return packet


def reusable(packet: EvidencePacket) -> bool:
    # Publication uncertainty remains PARTIAL in live reports. It does not mean
    # a transport or cache write was incomplete, and must never be upgraded here.
    return bool(packet.bars) and not any(
        f.severity == "error" and f.code != "PUBLICATION_UNVERIFIED" for f in packet.findings
    )


class EvidenceCache:
    def __init__(self, directory: Path, policy: CachePolicy | None = None, *, clock=None):
        self.directory = directory.resolve()
        self.policy = policy or CachePolicy()
        self.clock = clock or (lambda: datetime.now(UTC))

    def identity(self, run, instrument, resolution, route) -> dict[str, object]:
        return {
            "market": run.market.model_dump(mode="json"),
            "instrument": instrument.model_dump(mode="json"),
            "provider": route.provider,
            "feed": route.feed.model_dump(mode="json"),
            "timeframe": resolution.requirement.timeframe,
            "adjustment": resolution.requirement.adjustment,
            "parameters": run.configuration.get("cache_parameters", run.configuration),
            "calendar": resolution.calendar.model_dump(mode="json"),
            "calendar_data_version": resolution.calendar_data_version,
            "cutoff_policy": resolution.policy.model_dump(mode="json"),
            "implementation": {
                "source": run.source_version,
                "components": run.component_versions,
                "collection": "2",
                "quality": "price-quality-1",
                "cache": self.policy.version,
            },
        }

    def _publish(self, kind, identity, value):
        content = encode(value.model_dump(mode="json"))
        path = (
            self.directory / kind / digest(identity) / f"{hashlib.sha256(content).hexdigest()}.json"
        )
        immutable_write(path, content)

    def _entries(self, kind, identity, model):
        for path in (self.directory / kind / digest(identity)).glob("*.json"):
            try:
                entry = model.model_validate_json(verified(str(path), path.stem))
                if entry.identity == identity:
                    yield entry
            except (OSError, ValueError):
                continue

    def metadata(self, identity, loader, *, refresh=False):
        """Cache caller-supplied reference metadata independently of price sessions.

        The loader must return current metadata from the declared source. Smoke
        listings use explicit local definitions, not live reference discovery.
        """
        now = self.clock()
        entries = sorted(
            self._entries("metadata", identity, MetadataEntry),
            key=lambda e: e.observed_at,
            reverse=True,
        )
        if not refresh and entries:
            age = (now - entries[0].observed_at).total_seconds()
            if 0 <= age < self.policy.metadata_ttl_seconds:
                return entries[0].value
        value = loader()
        self._publish(
            "metadata", identity, MetadataEntry(identity=identity, value=value, observed_at=now)
        )
        return value

    def candidates(self, identity, resolution):
        entries = sorted(
            self._entries("prices", identity, CacheEntry),
            key=lambda e: (e.checked_at, e.committed_at, str(e.snapshot.metadata.snapshot_id)),
            reverse=True,
        )
        for entry in entries:
            try:
                packet = load_snapshot(entry.snapshot)
                if (
                    reusable(packet)
                    and packet.resolution.session_cutoff <= resolution.session_cutoff
                    and packet.resolution.requested_as_of <= resolution.requested_as_of
                ):
                    yield entry, packet
            except (OSError, ValueError):
                continue

    def lookup(self, identity, resolution, mode):
        for entry, packet in self.candidates(identity, resolution):
            age = (self.clock() - entry.checked_at).total_seconds()
            if (
                packet.resolution.periods == resolution.periods
                and packet.resolution.requirement == resolution.requirement
                and (mode == "replay" or 0 <= age < self.policy.price_ttl_seconds)
            ):
                return entry.snapshot, packet
        if mode == "replay":
            raise CacheMiss("No verified snapshot for this history; replay never calls a provider")
        return None

    def commit(self, identity, reference, packet):
        if not reusable(packet):
            return
        load_snapshot(reference)
        self._publish(
            "prices",
            identity,
            CacheEntry(
                identity=identity,
                snapshot=reference,
                committed_at=self.clock(),
                checked_at=RawPriceResponse.model_validate(
                    {**packet.response, "payload": b""}
                ).retrieved_at,
            ),
        )

    def fetch(self, identity, instrument, market, resolution, provider):
        """Fetch missing periods plus overlap; retain both source generations."""
        candidate = next(self.candidates(identity, resolution), None)
        if candidate is None:
            return provider.fetch(instrument, market, resolution), (), {}
        entry, previous = candidate
        old = {b.session: b for b in previous.bars}
        old_periods = {p.session_end: p for p in previous.resolution.periods}
        periods = resolution.periods
        overlap = [i for i, p in enumerate(periods) if p.session_end in old]
        missing = [i for i, p in enumerate(periods) if old_periods.get(p.session_end) != p]
        # If history does not overlap, or starts earlier, fetch the whole window.
        start = (
            min(missing + [overlap[max(0, len(overlap) - self.policy.overlap_periods)]])
            if overlap
            else 0
        )
        selected = periods[start:]
        partial = resolution.model_copy(
            update={
                "periods": selected,
                "requirement": HistoryRequirement(
                    timeframe=resolution.requirement.timeframe,
                    minimum_bars=len(selected),
                    adjustment=resolution.requirement.adjustment,
                    mandatory=resolution.requirement.mandatory,
                ),
            }
        )
        fetch = getattr(provider, "fetch_window", provider.fetch)
        response = fetch(instrument, market, partial)
        # Keep the exact provider bytes before interpreting or composing anything.
        raw = self.directory / "refreshes" / str(uuid4()) / "source.json"
        sha = immutable_write(raw, response.payload)
        fresh, findings, _ = validate_prices(response, partial)
        valid_source = (
            response.provider == identity["provider"]
            and response.feed.model_dump(mode="json") == identity["feed"]
            and all(
                instrument.provider_symbol(response.provider, p.session_end)
                == response.provider_symbol
                for p in selected
            )
        )
        if not valid_source or any(
            f.severity == "error" and f.code != "PUBLICATION_UNVERIFIED" for f in findings
        ):
            # Do not fill missing/invalid refresh rows from old data. Collection
            # retains the failed response and marks full-window coverage partial.
            return response, (), {}
        changed = tuple(b.session for b in fresh if b.session in old and b != old[b.session])
        combined = {**old, **{b.session: b for b in fresh}}
        lineage = {
            "parent_snapshot_id": str(previous.metadata.snapshot_id),
            "parent_manifest": entry.snapshot.reference,
            "parent_sha256": entry.snapshot.sha256,
            "refresh_start": str(selected[0].session_start),
            "refresh_end": str(selected[-1].session_end),
            "corrected_sessions": [str(s) for s in changed],
            "overlap_periods": self.policy.overlap_periods,
        }
        sources = previous.metadata.sources + (
            SourceRecord(
                provider=response.provider,
                provider_symbol=response.provider_symbol,
                retrieved_at=response.retrieved_at,
                reference=str(raw),
                sha256=sha,
                session_start=partial.history_start,
                session_end=partial.session_date,
            ),
        )
        payload = encode(
            {
                "bars": [
                    {
                        **combined[p.session_end].model_dump(mode="json"),
                        "timestamp": p.closes_at.isoformat(),
                    }
                    for p in periods
                ],
                "cache_refresh": lineage,
                "source_references": [s.model_dump(mode="json") for s in sources],
            }
        )
        flags = response.quality_flags
        if start:
            flags = tuple(dict.fromkeys((*flags, *previous.response.get("quality_flags", ()))))
        return (
            response.model_copy(update={"payload": payload, "quality_flags": flags}),
            sources,
            lineage,
        )
