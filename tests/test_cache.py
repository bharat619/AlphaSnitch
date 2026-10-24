"""Offline cache identity, validity, correction and atomic-publication contracts."""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from stock_analyser.cache import CacheMiss, CachePolicy, EvidenceCache, digest, load_snapshot
from stock_analyser.calendars import CutoffPolicy, HistoryResolver
from stock_analyser.collection import EvidencePacket, encode, immutable_write, validate_prices
from stock_analyser.contracts import (
    AnalysisRequest,
    DataAvailability,
    EvidenceMetadata,
    HistoryRequirement,
    SourceRecord,
)
from stock_analyser.fixtures import (
    RawFixtureProvider,
    synthetic_markets,
    synthetic_session_calendars,
)
from stock_analyser.persistence.records import RunRecord, SnapshotReference


@pytest.fixture
def setup(tmp_path):
    markets, instruments, _ = synthetic_markets()
    market, instrument = markets.resolve("US"), instruments.resolve("US", "ACME")
    _, calendars = synthetic_session_calendars()
    resolver = HistoryResolver(
        calendars[0],
        CutoffPolicy(earliest_session=date(2026, 10, 5), publication_timing="synthetic"),
    )
    requirement = HistoryRequirement(timeframe="daily", minimum_bars=6)
    resolution = resolver.resolve(
        market, instrument.venue, requirement, datetime(2026, 10, 15, 21, tzinfo=UTC)
    )
    run = RunRecord(
        run_id=uuid4(),
        request=AnalysisRequest(
            request_id=uuid4(),
            mode="buy",
            market_id="US",
            instrument_ids=(instrument.instrument_id,),
            requested_as_of=resolution.requested_as_of,
            strategy_version="test-1",
            history=(requirement,),
        ),
        market=market,
        configuration={"provider_parameters": {"adjusted": True}},
        source_version={"commit": "test"},
        component_versions={"fixture": "1"},
        history_resolutions=(resolution,),
    )
    now = [resolution.requested_as_of + timedelta(seconds=2)]
    cache = EvidenceCache(tmp_path / "cache", CachePolicy(overlap_periods=2), clock=lambda: now[0])
    identity = cache.identity(run, instrument, resolution, market.route("prices"))
    return cache, run, instrument, resolution, resolver, identity, now


def save(cache, identity, instrument, market, resolution, response, sources=(), lineage=None):
    directory = cache.directory / "snapshots" / str(uuid4())
    raw = directory / "source.json"
    sha = immutable_write(raw, response.payload)
    bars, findings, count = validate_prices(response, resolution)
    errors = any(f.severity == "error" for f in findings)
    metadata = EvidenceMetadata(
        snapshot_id=uuid4(),
        instrument=instrument,
        market=market,
        currency=instrument.quote_currency,
        session_cutoff=resolution.session_cutoff,
        feed=response.feed,
        timeframe=resolution.requirement.timeframe,
        adjustment=response.adjustment,
        availability=DataAvailability.PARTIAL if errors else DataAvailability.AVAILABLE,
        reason="Quality findings" if errors else None,
        quality_flags=tuple(f.code for f in findings),
        requested_bars=resolution.requirement.required_bars,
        returned_bars=count,
        sources=(
            SourceRecord(
                provider=response.provider,
                provider_symbol=response.provider_symbol,
                retrieved_at=response.retrieved_at,
                reference=str(raw),
                sha256=sha,
                session_start=resolution.history_start,
                session_end=resolution.session_date,
            ),
        )
        + sources,
    )
    packet = EvidencePacket(
        metadata=metadata,
        resolution=resolution,
        findings=findings,
        bars=bars,
        raw_reference=str(raw),
        response=response.model_dump(mode="json", exclude={"payload"}),
        source_version={},
        component_versions={},
        cache_refresh=lineage or {},
    )
    manifest = directory / "manifest.json"
    reference = SnapshotReference(
        metadata=metadata,
        reference=str(manifest),
        sha256=immutable_write(manifest, encode(packet.model_dump(mode="json"))),
    )
    cache.commit(identity, reference, packet)
    return reference, packet


def seed(setup):
    cache, run, instrument, resolution, _, identity, _ = setup
    response = RawFixtureProvider().fetch(instrument, run.market, resolution)
    return save(cache, identity, instrument, run.market, resolution, response)


def test_restart_replay_and_expiration_are_independent(setup):
    cache, _, _, resolution, _, identity, now = setup
    reference, packet = seed(setup)
    now[0] += timedelta(days=2)
    restarted = EvidenceCache(cache.directory, cache.policy, clock=lambda: now[0])
    assert restarted.lookup(identity, resolution, "reuse") is None
    assert restarted.lookup(identity, resolution, "replay") == (reference, packet)
    with pytest.raises(CacheMiss):
        restarted.lookup({**identity, "provider": "other"}, resolution, "replay")


def test_old_packet_serialization_stays_byte_identical(setup):
    reference, packet = seed(setup)
    original = Path(reference.reference).read_bytes()
    assert "cache_refresh" not in json.loads(original)
    assert encode(EvidencePacket.model_validate_json(original).model_dump(mode="json")) == original


@pytest.mark.parametrize(
    "field",
    [
        "market",
        "instrument",
        "provider",
        "feed",
        "timeframe",
        "adjustment",
        "parameters",
        "calendar",
        "calendar_data_version",
        "cutoff_policy",
        "implementation",
    ],
)
def test_cache_identity_isolates_every_dimension(setup, field):
    cache, _, _, resolution, _, identity, _ = setup
    seed(setup)
    changed = {**identity, field: "different"}
    assert digest(changed) != digest(identity)
    assert cache.lookup(changed, resolution, "reuse") is None


def test_incremental_refresh_preserves_old_snapshot_and_precise_corrections(setup):
    cache, run, instrument, resolution, resolver, identity, now = setup
    old_reference, old_packet = seed(setup)
    old_bytes = Path(old_reference.reference).read_bytes()
    new_plan = resolver.resolve(
        run.market,
        instrument.venue,
        resolution.requirement,
        resolution.requested_as_of + timedelta(days=1),
    )
    now[0] += timedelta(days=1)
    calls = []
    corrected = resolution.periods[-1].session_end

    class Correction(RawFixtureProvider):
        def fetch(self, instrument, market, plan):
            calls.append(plan)
            response = super().fetch(instrument, market, plan)
            payload = json.loads(response.payload)
            for bar in payload["bars"]:
                if bar["session"] == str(corrected):
                    bar["volume"] = "1.5000000000000000001"
            return response.model_copy(update={"payload": encode(payload)})

    response, sources, lineage = cache.fetch(
        identity, instrument, run.market, new_plan, Correction()
    )
    assert len(calls) == 1
    assert len(calls[0].periods) == 3  # two old periods plus one new session
    assert lineage["corrected_sessions"] == [str(corrected)]
    new_reference, new_packet = save(
        cache, identity, instrument, run.market, new_plan, response, sources, lineage
    )
    assert new_reference.metadata.snapshot_id != old_reference.metadata.snapshot_id
    assert Path(old_reference.reference).read_bytes() == old_bytes
    assert load_snapshot(old_reference) == old_packet
    assert load_snapshot(new_reference) == new_packet
    assert cache.lookup(identity, new_plan, "replay") == (new_reference, new_packet)
    assert old_reference.sha256 == hashlib.sha256(old_bytes).hexdigest()
    assert len(new_packet.bars) == 6
    assert not new_packet.findings


def test_same_window_refresh_and_missing_overlap_never_fill_holes(setup):
    cache, run, instrument, resolution, _, identity, _ = setup
    reference, _ = seed(setup)
    response, sources, lineage = cache.fetch(
        identity, instrument, run.market, resolution, RawFixtureProvider("missing-bar")
    )
    assert sources == () and lineage == {}
    _, findings, _ = validate_prices(response, resolution)
    assert "MISSING_SESSION" in {f.code for f in findings}
    assert cache.lookup(identity, resolution, "replay")[0] == reference


@pytest.mark.parametrize("target", ["manifest", "source", "index"])
def test_corrupt_files_are_not_hits(setup, target):
    cache, _, _, resolution, _, identity, _ = setup
    reference, packet = seed(setup)
    path = {
        "manifest": Path(reference.reference),
        "source": Path(packet.raw_reference),
        "index": next((cache.directory / "prices").rglob("*.json")),
    }[target]
    path.chmod(0o644)
    path.write_bytes(b"incomplete")
    with pytest.raises(CacheMiss):
        cache.lookup(identity, resolution, "replay")


def test_interrupted_publication_leaves_no_committed_hit(setup, monkeypatch):
    cache, _, _, resolution, _, identity, _ = setup
    import stock_analyser.collection as collection

    original = collection.os.link

    def interrupt(source, target):
        if "prices" in Path(target).parts:
            raise OSError("simulated crash before commit")
        return original(source, target)

    monkeypatch.setattr(collection.os, "link", interrupt)
    with pytest.raises(OSError, match="simulated crash"):
        seed(setup)
    assert list(cache.directory.rglob("manifest.json"))  # orphaned evidence is inspectable
    assert not list((cache.directory / "prices").rglob("*.json"))
    with pytest.raises(CacheMiss):
        cache.lookup(identity, resolution, "replay")


def test_metadata_ttl_versions_do_not_expire_prices(setup):
    cache, _, _, resolution, _, identity, now = setup
    reference, packet = seed(setup)
    metadata_identity = {"market": "US", "instrument": "ACME", "provider": "local", "version": "1"}
    calls = []

    def loader():
        calls.append(True)
        return {"revision": len(calls)}

    assert cache.metadata(metadata_identity, loader) == {"revision": 1}
    assert cache.metadata(metadata_identity, loader) == {"revision": 1}
    now[0] += timedelta(days=1)
    assert cache.metadata(metadata_identity, loader) == {"revision": 2}
    assert len(list((cache.directory / "metadata").rglob("*.json"))) == 2
    assert cache.lookup(identity, resolution, "replay") == (reference, packet)


def test_new_snapshot_checks_all_retained_raw_sources(setup):
    cache, run, instrument, resolution, _, identity, _ = setup
    old, _ = seed(setup)
    response, sources, lineage = cache.fetch(
        identity, instrument, run.market, resolution, RawFixtureProvider()
    )
    new, _ = save(cache, identity, instrument, run.market, resolution, response, sources, lineage)
    path = Path(old.metadata.sources[0].reference)
    path.chmod(0o644)
    path.write_bytes(b"corruption")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_snapshot(new)


def test_replay_and_incremental_refresh_preserve_publication_uncertainty(setup):
    cache, run, instrument, resolution, _, identity, _ = setup
    response = (
        RawFixtureProvider()
        .fetch(instrument, run.market, resolution)
        .model_copy(update={"quality_flags": ("PUBLICATION_UNVERIFIED",)})
    )
    reference, packet = save(cache, identity, instrument, run.market, resolution, response)
    assert packet.metadata.availability == DataAvailability.PARTIAL
    assert cache.lookup(identity, resolution, "replay") == (reference, packet)
    refreshed, sources, lineage = cache.fetch(
        identity, instrument, run.market, resolution, RawFixtureProvider()
    )
    _, new = save(cache, identity, instrument, run.market, resolution, refreshed, sources, lineage)
    assert new.metadata.availability == DataAvailability.PARTIAL
    assert "PUBLICATION_UNVERIFIED" in new.metadata.quality_flags


def test_weekly_refresh_queries_complete_periods(setup):
    cache, run, instrument, _, resolver, _, _ = setup
    requirement = HistoryRequirement(timeframe="weekly", minimum_bars=1)
    first = resolver.resolve(
        run.market, instrument.venue, requirement, datetime(2026, 10, 9, 21, tzinfo=UTC)
    )
    identity = cache.identity(run, instrument, first, run.market.route("prices"))
    save(
        cache,
        identity,
        instrument,
        run.market,
        first,
        RawFixtureProvider().fetch(instrument, run.market, first),
    )
    second = resolver.resolve(
        run.market,
        instrument.venue,
        requirement.model_copy(update={"minimum_bars": 2}),
        datetime(2026, 10, 16, 21, tzinfo=UTC),
    )
    calls = []

    class Recording(RawFixtureProvider):
        def fetch(self, instrument, market, plan):
            calls.append(plan)
            return super().fetch(instrument, market, plan)

    response, _, _ = cache.fetch(identity, instrument, run.market, second, Recording())
    assert calls[0].periods == second.periods
    assert calls[0].history_start == date(2026, 10, 5)
    assert len(validate_prices(response, second)[0]) == 2
