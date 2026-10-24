"""Frozen sector selection must explain exclusions and replay without providers."""

from datetime import date, timedelta, timezone
from decimal import ROUND_UP, Decimal, Inexact, localcontext
from pathlib import Path
from uuid import uuid4

import pytest

from stock_analyser.cache import load_snapshot
from stock_analyser.collection import encode, immutable_write
from stock_analyser.contracts import HistoryRequirement
from stock_analyser.persistence.records import SnapshotReference
from stock_analyser.universe import (
    FrozenUniverseAdapter,
    ReferenceUniverseAdapter,
    SelectionPolicy,
    UniverseSnapshot,
    freeze_selection,
    replay_universe,
    select_universe,
)
from stock_analyser.universe_demo import fixture_request
from stock_analyser.universe_spike import Enumeration


@pytest.fixture
def data(tmp_path):
    return fixture_request(tmp_path / "inputs")


def decide(data):
    request, resolver = data
    snapshot = select_universe(request, resolver)
    return snapshot, {d.ticker: d for d in snapshot.result.decisions}


def change_candidate(request, ticker, **changes):
    candidates = tuple(
        c.model_copy(update=changes) if c.reference.ticker == ticker else c
        for c in request.population.candidates
    )
    return request.model_copy(
        update={"population": request.population.model_copy(update={"candidates": candidates})}
    )


def change_packet(request, tmp_path, symbol, change, *, timeframe="daily", adjustment="raw"):
    """Publish a distinct history generation; leave the initial immutable evidence alone."""
    reference = next(
        h
        for h in request.histories
        if h.metadata.instrument.symbol == symbol
        and h.metadata.timeframe == timeframe
        and h.metadata.adjustment == adjustment
    )
    packet = change(load_snapshot(reference))
    metadata = packet.metadata.model_copy(update={"snapshot_id": uuid4()})
    packet = packet.model_copy(update={"metadata": metadata})
    path = tmp_path / f"{uuid4()}.json"
    replacement = SnapshotReference(
        metadata=metadata,
        reference=str(path),
        sha256=immutable_write(path, encode(packet.model_dump(mode="json"))),
    )
    return request.model_copy(
        update={"histories": tuple(replacement if h == reference else h for h in request.histories)}
    )


def test_ten_distinct_with_ties_and_all_exclusions(data):
    snapshot, decisions = decide(data)
    result = snapshot.result
    expected = sorted(
        (
            c.instrument
            for c in data[0].population.candidates
            if c.reference.ticker.startswith("TECH")
        ),
        key=lambda i: str(i.instrument_id),
    )
    assert result.selected == tuple(expected[:10])
    assert len({i.security_id for i in result.selected}) == 10
    assert result.shortfall == 0 and result.enumeration_complete
    assert result.scope == "PROTOTYPE" and result.full_sector_accepted is False
    assert result.unresolved_classifications == ("UNKNOWN",)
    for symbol, reason in {
        "ETF": "NON_COMMON_OR_UNKNOWN_TYPE",
        "ADR": "NON_COMMON_OR_UNKNOWN_TYPE",
        "OTC": "UNSUPPORTED_OR_UNKNOWN_VENUE",
        "UNKNOWN": "UNKNOWN_CLASSIFICATION",
        "CHEAP": "BELOW_MINIMUM_PRICE",
        "ILLIQUID": "BELOW_MINIMUM_LIQUIDITY",
        "SHORT": "INVALID_MANDATORY_HISTORY",
        "ALTERNATE": "DUPLICATE_SECURITY",
    }.items():
        assert reason in decisions[symbol].reasons
    assert len(decisions["TECH00"].observations) == 60
    assert decisions["TECH00"].average_traded_value == Decimal("20000000")


def test_shortfall_is_separate_from_incomplete_enumeration(tmp_path):
    request, resolver = fixture_request(tmp_path, eligible_count=7)
    complete = select_universe(request, resolver)
    partial_request = request.model_copy(
        update={
            "population": request.population.model_copy(
                update={"enumeration_status": "TRUNCATED", "enumeration_reason": "PAGE_LIMIT"}
            )
        }
    )
    partial = select_universe(partial_request, resolver)
    assert complete.result.shortfall == partial.result.shortfall == 3
    assert complete.result.selected == partial.result.selected
    assert complete.result.enumeration_complete is True
    assert partial.result.enumeration_complete is False
    assert partial.result.full_sector_accepted is False
    assert complete.snapshot_id != partial.snapshot_id


def test_reordered_frozen_inputs_and_decimal_context_reproduce_identical_snapshot(data):
    request, resolver = data
    snapshot = select_universe(request, resolver)
    shuffled = request.model_copy(
        update={
            "population": request.population.model_copy(
                update={"candidates": request.population.candidates[::-1]}
            ),
            "histories": request.histories[::-1],
        }
    )
    with localcontext() as context:
        context.prec = 3
        assert select_universe(shuffled, resolver) == snapshot
        assert (
            replay_universe(UniverseSnapshot.model_validate_json(snapshot.model_dump_json()))
            == snapshot
        )


def test_replay_and_downstream_adapter_reuse_exact_cached_histories(data, monkeypatch):
    snapshot, _ = decide(data)

    def forbidden(*args, **kwargs):
        raise AssertionError("Replay must not re-resolve the calendar or call a provider")

    monkeypatch.setattr(type(data[1]), "resolve", forbidden)
    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", forbidden)
    adapter = FrozenUniverseAdapter(snapshot)
    assert (
        adapter.instruments(snapshot.request.market, snapshot.request.requested_as_of)
        == snapshot.result.selected
    )
    for instrument in snapshot.result.selected:
        references = adapter.histories(instrument.instrument_id)
        assert references and all(r in snapshot.request.histories for r in references)
        assert (
            load_snapshot(references[0]).metadata.snapshot_id == references[0].metadata.snapshot_id
        )
    with pytest.raises(ValueError, match="cutoff"):
        adapter.instruments(
            snapshot.request.market, snapshot.request.requested_as_of + timedelta(days=1)
        )
    with pytest.raises(ValueError, match="not selected"):
        adapter.histories(uuid4())


def test_threshold_configuration_changes_snapshot_and_filters_before_ranking(data):
    request, resolver = data
    policy = SelectionPolicy(minimum_price_usd="10", minimum_average_traded_value_usd="20000000")
    boundary = select_universe(request.model_copy(update={"policy": policy}), resolver)
    assert len(boundary.result.selected) == 10
    higher = policy.model_copy(update={"minimum_average_traded_value_usd": Decimal("20000000.01")})
    excluded = select_universe(request.model_copy(update={"policy": higher}), resolver)
    assert excluded.result.selected == () and excluded.result.shortfall == 10
    assert boundary.snapshot_id != excluded.snapshot_id


def test_zero_volume_contributes_zero_and_no_older_replacement(data, tmp_path):
    request, resolver = data

    def zero(packet):
        bars = packet.bars[:-1] + (packet.bars[-1].model_copy(update={"volume": Decimal(0)}),)
        return packet.model_copy(update={"bars": bars})

    request = change_packet(request, tmp_path, "TECH00", zero)
    snapshot = select_universe(request, resolver)
    decision = next(d for d in snapshot.result.decisions if d.ticker == "TECH00")
    assert len(decision.observations) == 60
    assert decision.observations[-1].value == 0
    assert decision.average_traded_value < Decimal("20000000")
    assert (
        decision.observations[0].session
        == load_snapshot(
            next(h for h in request.histories if h.metadata.instrument.symbol == "TECH00")
        )
        .bars[-60]
        .session
    )
    assert snapshot.result.ranked_instrument_ids[-1] == decision.instrument_id
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_UP
        context.traps[Inexact] = True
        assert replay_universe(snapshot) == snapshot


def test_ranking_preserves_differences_beyond_default_decimal_precision(data, tmp_path):
    request, resolver = data
    first = next(c for c in request.population.candidates if c.reference.ticker == "TECH00")

    def more_liquid(packet):
        bars = tuple(
            b.model_copy(update={"volume": Decimal("2000000.000000000000000000000000001")})
            for b in packet.bars
        )
        return packet.model_copy(update={"bars": bars})

    request = change_packet(request, tmp_path, "TECH00", more_liquid)
    with localcontext() as context:
        context.prec = 3
        snapshot = select_universe(request, resolver)
    assert snapshot.result.selected[0] == first.instrument


@pytest.mark.parametrize(
    "alternate_symbol,missing_weekly",
    [
        ("TECH00", False),
        ("CHEAP", False),
        ("ILLIQUID", False),
        ("TECH00", True),
    ],
)
def test_only_eligible_candidates_determine_feed_compatibility(
    data, tmp_path, alternate_symbol, missing_weekly
):
    from stock_analyser.contracts import ProviderRoute

    request, resolver = data
    if missing_weekly:
        request, resolver = fixture_request(
            tmp_path / "weekly",
            policy=SelectionPolicy(
                mandatory_history=(HistoryRequirement(timeframe="weekly", minimum_bars=2),)
            ),
        )
        request = request.model_copy(
            update={
                "histories": tuple(
                    h
                    for h in request.histories
                    if not (
                        h.metadata.instrument.symbol == alternate_symbol
                        and h.metadata.timeframe == "weekly"
                    )
                )
            }
        )
    baseline = select_universe(request, resolver)
    alternate_feed = request.market.route("prices").feed.model_copy(update={"feed_id": "alternate"})
    market = request.market.model_copy(
        update={
            "routes": (
                *request.market.routes,
                ProviderRoute(
                    role="long_history_prices", provider="fixture-us", feed=alternate_feed
                ),
            )
        }
    )
    request = request.model_copy(update={"market": market})
    resolver.calendar.market = market
    for reference in request.histories:
        symbol = reference.metadata.instrument.symbol

        def change(packet, symbol=symbol):
            metadata = packet.metadata.model_copy(
                update={
                    "market": market,
                    "feed": alternate_feed if symbol == alternate_symbol else packet.metadata.feed,
                }
            )
            return packet.model_copy(update={"metadata": metadata})

        request = change_packet(
            request,
            tmp_path,
            symbol,
            change,
            timeframe=reference.metadata.timeframe,
            adjustment=reference.metadata.adjustment,
        )
    snapshot, decisions = decide((request, resolver))
    if alternate_symbol == "TECH00" and not missing_weekly:
        assert snapshot.result.selected == ()
        assert "INCOMPARABLE_LIQUIDITY_INPUTS" in decisions["TECH00"].reasons
        assert "INCOMPARABLE_LIQUIDITY_INPUTS" in decisions["TECH01"].reasons
        assert "INCOMPARABLE_LIQUIDITY_INPUTS" not in decisions["CHEAP"].reasons
    else:
        assert snapshot.result.selected == baseline.result.selected
        assert len(snapshot.result.selected) == 10
        assert all("INCOMPARABLE_LIQUIDITY_INPUTS" not in d.reasons for d in decisions.values())
    legacy = freeze_selection(
        snapshot.request, snapshot.resolutions, implementation_version="sector-selection-1"
    )
    assert legacy.result.selected == ()
    assert replay_universe(UniverseSnapshot.model_validate_json(legacy.model_dump_json())) == legacy
    assert legacy.snapshot_id != snapshot.snapshot_id


@pytest.mark.parametrize("effective_date", [date(2026, 10, 16), date(2026, 10, 17)])
@pytest.mark.parametrize("hours_later", [0, 5])
def test_classification_uses_market_date_for_equivalent_instants(data, effective_date, hours_later):
    request, resolver = data
    # At 02:00 UTC on October 17 the New York market date is still October 16.
    request = request.model_copy(
        update={"requested_as_of": request.requested_as_of + timedelta(hours=hours_later)}
    )
    candidate = next(c for c in request.population.candidates if c.reference.ticker == "TECH00")
    request = change_candidate(
        request,
        "TECH00",
        classification=candidate.classification.model_copy(
            update={"effective_date": effective_date}
        ),
    )
    snapshots = [
        select_universe(
            request.model_copy(
                update={
                    "requested_as_of": request.requested_as_of.astimezone(
                        timezone(timedelta(hours=offset))
                    )
                }
            ),
            resolver,
        )
        for offset in (0, 14, -12)
    ]
    assert all(s.result == snapshots[0].result for s in snapshots)
    decision = next(d for d in snapshots[0].result.decisions if d.ticker == "TECH00")
    assert ("FUTURE_CLASSIFICATION" in decision.reasons) == (effective_date.day == 17)
    # Historical version-1 results retain their original semantics and identity.
    legacy = freeze_selection(
        snapshots[1].request, snapshots[1].resolutions, implementation_version="sector-selection-1"
    )
    assert replay_universe(UniverseSnapshot.model_validate_json(legacy.model_dump_json())) == legacy
    legacy_decision = next(d for d in legacy.result.decisions if d.ticker == "TECH00")
    assert "FUTURE_CLASSIFICATION" not in legacy_decision.reasons


def test_excluded_candidate_window_does_not_poison_ranking(data, monkeypatch):
    import stock_analyser.universe as universe

    request, resolver = data
    baseline = select_universe(request, resolver)
    original = universe._history

    def shifted_window(reference, instrument, request, resolution):
        packet, bars = original(reference, instrument, request, resolution)
        if instrument.symbol == "CHEAP":
            bars = tuple(
                b.model_copy(update={"session": b.session - timedelta(days=1)}) for b in bars
            )
        return packet, bars

    monkeypatch.setattr(universe, "_history", shifted_window)
    snapshot = select_universe(request, resolver)
    assert snapshot.result.selected == baseline.result.selected
    assert all("INCOMPARABLE_LIQUIDITY_INPUTS" not in d.reasons for d in snapshot.result.decisions)


@pytest.mark.parametrize("failure", ["gap", "stale", "split", "unverified", "wrong_instrument"])
def test_invalid_history_cannot_rank(data, tmp_path, failure):
    request, resolver = data

    def corrupt(packet):
        if failure == "gap":
            bars = packet.bars[:230] + packet.bars[231:]
            return packet.model_copy(
                update={
                    "bars": bars,
                    "metadata": packet.metadata.model_copy(
                        update={
                            "returned_bars": len(bars),
                            "requested_bars": len(bars),
                        }
                    ),
                }
            )
        if failure == "stale":
            return packet.model_copy(
                update={
                    "resolution": packet.resolution.model_copy(
                        update={"periods": packet.resolution.periods[:-1]}
                    )
                }
            )
        if failure == "split":
            return packet.model_copy(
                update={"metadata": packet.metadata.model_copy(update={"adjustment": "split"})}
            )
        if failure == "unverified":
            from stock_analyser.collection import QualityFinding

            return packet.model_copy(
                update={"findings": (QualityFinding(code="PUBLICATION_UNVERIFIED"),)}
            )
        instrument = packet.metadata.instrument.model_copy(update={"series": "preferred"})
        return packet.model_copy(
            update={"metadata": packet.metadata.model_copy(update={"instrument": instrument})}
        )

    request = change_packet(request, tmp_path, "TECH00", corrupt)
    _, decisions = decide((request, resolver))
    assert any("HISTORY" in reason for reason in decisions["TECH00"].reasons)


def test_mandatory_weekly_history_is_independent_and_counts_warmup(tmp_path):
    policy = SelectionPolicy(
        mandatory_history=(
            HistoryRequirement(
                timeframe="weekly", minimum_bars=52, warmup_bars=2, adjustment="split"
            ),
        )
    )
    request, resolver = fixture_request(tmp_path / "weekly", policy=policy)
    full = select_universe(request, resolver)
    assert len(full.result.selected) == 10
    assert {len(r.periods) for r in full.resolutions} == {260, 54}
    request = request.model_copy(
        update={
            "histories": tuple(
                h
                for h in request.histories
                if not (
                    h.metadata.instrument.symbol == "TECH00" and h.metadata.timeframe == "weekly"
                )
            )
        }
    )
    _, decisions = decide((request, resolver))
    assert "MISSING_MANDATORY_HISTORY" in decisions["TECH00"].reasons
    policy = SelectionPolicy(
        mandatory_history=(HistoryRequirement(timeframe="daily", minimum_bars=300, warmup_bars=20),)
    )
    assert next(h for h in policy.requirements() if h.adjustment == "raw").required_bars == 320


def test_dedup_cannot_replace_primary_with_a_classified_alternate(data):
    request, resolver = data
    primary = next(c for c in request.population.candidates if c.reference.ticker == "TECH00")
    request = change_candidate(
        request,
        "TECH00",
        classification=primary.classification.model_copy(update={"original_sector": None}),
    )
    snapshot, decisions = decide((request, resolver))
    assert "UNKNOWN_CLASSIFICATION" in decisions["TECH00"].reasons
    assert "DUPLICATE_SECURITY" in decisions["ALTERNATE"].reasons
    assert primary.instrument.security_id not in {i.security_id for i in snapshot.result.selected}


def test_conflicting_security_classifications_block_both_listings(data):
    request, resolver = data
    alternate = next(c for c in request.population.candidates if c.reference.ticker == "ALTERNATE")
    request = change_candidate(
        request,
        "ALTERNATE",
        classification=alternate.classification.model_copy(
            update={"original_sector": "Financial Services"}
        ),
    )
    _, decisions = decide((request, resolver))
    for symbol in ("TECH00", "ALTERNATE"):
        assert "CONFLICTING_SECURITY_CLASSIFICATION" in decisions[symbol].reasons


def test_identity_conflicts_cannot_duplicate_a_security(data):
    request, resolver = data
    alternate = next(c for c in request.population.candidates if c.reference.ticker == "ALTERNATE")
    request = change_candidate(
        request,
        "ALTERNATE",
        instrument=alternate.instrument.model_copy(update={"security_id": uuid4()}),
    )
    _, decisions = decide((request, resolver))
    for symbol in ("TECH00", "ALTERNATE"):
        assert "CONFLICTING_INSTRUMENT_IDENTITY" in decisions[symbol].reasons


def test_profile_must_match_verified_effective_provider_symbol(data):
    request, resolver = data
    candidate = next(c for c in request.population.candidates if c.reference.ticker == "TECH00")
    classification = candidate.classification.model_copy(
        update={
            "source": candidate.classification.source.model_copy(
                update={"provider_symbol": "OTHER"}
            )
        }
    )
    request = change_candidate(request, "TECH00", classification=classification)
    snapshot, decisions = decide((request, resolver))
    assert "UNVERIFIED_CLASSIFICATION_IDENTITY" in decisions["TECH00"].reasons
    assert "TECH00" in snapshot.result.unresolved_classifications


def test_adapter_preserves_partial_population_and_enrichment_failures(data):
    request, _ = data
    rows = [
        c.reference.model_dump(exclude={"schema_version"}) for c in request.population.candidates
    ]
    calls = []

    def failed(row, instrument):
        calls.append(row.ticker)
        raise ConnectionError("Do not persist exception content")

    adapter = ReferenceUniverseAdapter(
        Enumeration("massive", {}, rows=rows, status="TRUNCATED", reason="PAGE_LIMIT"),
        request.population.source,
        lambda _: None,
        failed,
    )
    population = adapter.enumerate()
    assert population.enumeration_status == "TRUNCATED"
    assert len(population.candidates) == len(rows)
    assert all(c.classification is None for c in population.candidates)
    assert "TECH00" in calls and "ETF" not in calls
    assert "Do not persist" not in population.model_dump_json()
    candidate = next(c for c in population.candidates if c.reference.ticker == "TECH00")
    assert [(f.stage, f.category) for f in candidate.enrichment_failures] == [
        ("identity", "UNRESOLVED"),
        ("classification", "PROVIDER_UNAVAILABLE"),
    ]


@pytest.mark.parametrize("stage", ["identity", "classification"])
@pytest.mark.parametrize(
    "failure,category",
    [
        (None, "UNRESOLVED"),
        (ConnectionError("secret-url"), "PROVIDER_UNAVAILABLE"),
        ({"invalid": "secret-response"}, "INVALID_RESPONSE"),
    ],
)
def test_enrichment_failures_have_sanitized_categories(data, stage, failure, category):
    request, _ = data
    candidate = next(c for c in request.population.candidates if c.reference.ticker == "TECH00")

    def fail(*args):
        if isinstance(failure, Exception):
            raise failure
        return failure

    adapter = ReferenceUniverseAdapter(
        Enumeration(
            "massive",
            {},
            rows=[candidate.reference.model_dump(exclude={"schema_version"})],
            status="COMPLETE",
            reason="TERMINAL_PAGE",
        ),
        request.population.source,
        fail if stage == "identity" else lambda _: candidate.instrument,
        fail if stage == "classification" else lambda *_: candidate.classification,
    )
    population = adapter.enumerate()
    assert [(f.stage, f.category) for f in population.candidates[0].enrichment_failures] == [
        (stage, category)
    ]
    assert "secret" not in population.model_dump_json()


@pytest.mark.parametrize("stage", ["identity", "classification"])
def test_enrichment_programming_errors_are_not_silently_downgraded(data, stage):
    request, _ = data
    candidate = next(c for c in request.population.candidates if c.reference.ticker == "TECH00")

    def broken(*args):
        raise RuntimeError("programming error")

    adapter = ReferenceUniverseAdapter(
        Enumeration(
            "massive", {}, rows=[candidate.reference.model_dump(exclude={"schema_version"})]
        ),
        request.population.source,
        broken if stage == "identity" else lambda _: candidate.instrument,
        broken if stage == "classification" else lambda *_: candidate.classification,
    )
    with pytest.raises(RuntimeError, match="programming error"):
        adapter.enumerate()


def test_rule_version_is_immutable_and_spike_mutation_cannot_change_replay(data, monkeypatch):
    from dataclasses import FrozenInstanceError

    from stock_analyser.universe_policy import TECHNOLOGY_RULES_V1
    from stock_analyser.universe_spike import POLICY

    snapshot, _ = decide(data)
    with pytest.raises(FrozenInstanceError):
        TECHNOLOGY_RULES_V1.currency = "CAD"
    with pytest.raises(TypeError):
        TECHNOLOGY_RULES_V1.listing_order[0] = "OTC"
    monkeypatch.setitem(POLICY, "currency", "CAD")
    monkeypatch.setitem(POLICY, "eligible_type", "ETF")
    monkeypatch.setitem(POLICY, "venues", [])
    monkeypatch.setitem(POLICY, "listing_order", [])
    monkeypatch.setitem(POLICY, "other_known_sectors", ["Unknown"])
    assert replay_universe(snapshot) == snapshot
    assert "enrichment_failures" not in snapshot.model_dump_json()


def test_tampered_price_chain_or_selection_fails_replay(data):
    snapshot, _ = decide(data)
    changed = snapshot.model_copy(
        update={
            "result": snapshot.result.model_copy(
                update={"selected": snapshot.result.selected[::-1]}
            )
        }
    )
    with pytest.raises(ValueError, match="replay differs"):
        replay_universe(changed)
    reference = next(
        h for h in snapshot.request.histories if h.metadata.instrument.symbol == "TECH00"
    )
    source = Path(reference.metadata.sources[0].reference)
    source.chmod(0o644)
    source.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="replay differs"):
        replay_universe(snapshot)


def test_snapshot_rejects_identity_tampering_and_false_full_sector_claim(data):
    snapshot, _ = decide(data)
    payload = snapshot.model_dump(mode="json")
    payload["inputs_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="identity mismatch"):
        UniverseSnapshot.model_validate(payload)
    payload = snapshot.model_dump(mode="json")
    payload["result"]["full_sector_accepted"] = True
    with pytest.raises(ValueError):
        UniverseSnapshot.model_validate(payload)
