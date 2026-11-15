import json
import os
import subprocess
import sys
from datetime import timedelta
from decimal import ROUND_UP, Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_analyser.breakout import (
    BenchmarkBinding,
    BreakoutAssessment,
    BreakoutPolicy,
    breakout_dependencies,
    qualify_breakout,
    resolve_spy_binding,
)
from stock_analyser.breakout_example import (
    EXAMPLE_CUTOFF,
    breakout_example_inputs,
    freeze_breakout_example,
)
from stock_analyser.calendars import CutoffPolicy
from stock_analyser.contracts import HistoryRequirement
from stock_analyser.fixtures import FixtureSessionCalendar
from stock_analyser.markets import InstrumentRegistry
from stock_analyser.universe import SelectionPolicy


def calculate(
    inputs, profile="stock", *, as_of=None, calculated_at=None, cutoff=EXAMPLE_CUTOFF, **kwargs
):
    snapshot, provenance, calendar, stamp = inputs
    stamp = as_of or stamp
    return qualify_breakout(
        snapshot,
        provenance,
        calendar,
        BreakoutPolicy(profile=profile),
        as_of=stamp,
        calculated_at=calculated_at or stamp,
        cutoff_policy=cutoff,
        **kwargs,
    )


def freeze(tmp_path, *, rows=None, count=260, **kwargs):
    market, _, _, calendar, default_rows = breakout_example_inputs(count)
    return freeze_breakout_example(
        tmp_path,
        market=market,
        calendar=calendar,
        rows=default_rows if rows is None else rows,
        **kwargs,
    )


def spy_inputs(tmp_path, *, count=260, rows=None, **kwargs):
    market, stock, spy, calendar, default_rows = breakout_example_inputs(count)
    reference, provenance, _, stamp = freeze_breakout_example(
        tmp_path,
        market=market,
        instrument=spy,
        calendar=calendar,
        rows=default_rows if rows is None else rows,
        **kwargs,
    )
    binding = resolve_spy_binding(
        market,
        InstrumentRegistry((stock, spy)),
        verification_reference="synthetic://verified-reference-inventory",
    )
    return dict(
        benchmark_binding=binding, benchmark_snapshot=reference, benchmark_provenance=provenance
    )


def test_versioned_policy_requires_explicit_profile_and_fixed_parameters():
    with pytest.raises(ValidationError):
        BreakoutPolicy()
    for key, value in (
        ("price_lookback", 19),
        ("rvol_threshold", "1.4"),
        ("profile", "breakout_only"),
        ("regular_session_seconds", 21600),
    ):
        with pytest.raises(ValidationError):
            BreakoutPolicy(**{"profile": "stock", key: value})


def test_dependency_expansion_preserves_foundation_and_optional_weekly():
    raw = HistoryRequirement(timeframe="daily", minimum_bars=260, adjustment="raw")
    split = HistoryRequirement(timeframe="daily", minimum_bars=15, warmup_bars=245)
    selection = SelectionPolicy(mandatory_history=(split,))
    baseline = breakout_dependencies(BreakoutPolicy(profile="stock"), selection, (raw,))
    variant = breakout_dependencies(BreakoutPolicy(profile="stock_with_spy"), selection, (raw,))
    assert baseline.stock_mandatory == (raw, split)
    assert baseline.benchmark_mandatory == (raw,)  # Disabling SPY never drops foundation history.
    assert variant.benchmark_mandatory == (
        raw,
        HistoryRequirement(timeframe="daily", minimum_bars=200),
    )
    assert baseline.stock_optional[0].timeframe == "weekly"
    assert (
        baseline.stock_optional[0].required_bars == 34 and not baseline.stock_optional[0].mandatory
    )
    assert BreakoutPolicy(profile="stock").stock_history.required_bars == 200
    assert SelectionPolicy().minimum_daily_bars == 260


def test_passing_setup_is_not_recommendation_and_roundtrips(tmp_path):
    result = calculate(freeze(tmp_path))
    assert result.status == "PASS"
    assert [g.status for g in result.gates] == ["PASS", "PASS", "PASS", "NOT_ENABLED"]
    assert result.exact_prices.breakout_level == 359 and result.exact_prices.signal_close == 360
    assert result.volume_observation.value == Decimal("1.5")
    assert result.recommendation_status == "NOT_EVALUATED"
    assert result.policy.relative_strength == "NOT_ENABLED"
    assert result.weekly_context.specification.role == "informational"
    assert result.available_at == result.as_of
    assert BreakoutAssessment.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "close,expected",
    [
        ("359", "FAIL"),
        ("358.9", "FAIL"),
        ("359.000000000000000000000000000000000000000000000000000000000001", "PASS"),
    ],
)
def test_breakout_is_strict_and_current_high_excluded(tmp_path, close, expected):
    _, _, _, _, rows = breakout_example_inputs()
    rows[-1].update(close=close, high="999", low="358")
    result = calculate(freeze(tmp_path, rows=rows))
    assert result.gates[0].status == expected and result.exact_prices.breakout_level == 359
    assert len(result.breakout_observation.prior_sessions) == 20
    assert all(d < result.observed_session for d in result.breakout_observation.prior_sessions)


@pytest.mark.parametrize(
    "volume,expected",
    [
        ("1499600", "FAIL"),
        ("1500000", "PASS"),
        ("1500001", "PASS"),
        ("0", "FAIL"),
        ("1499999." + "9" * 60, "FAIL"),
    ],
)
def test_rvol_exact_inclusive_threshold_not_display_rounding(tmp_path, volume, expected):
    _, _, _, _, rows = breakout_example_inputs()
    rows[-1]["volume"] = volume
    result = calculate(freeze(tmp_path, rows=rows))
    assert result.gates[2].status == expected
    if volume.endswith("9" * 60):
        assert result.volume_observation.value == Decimal("1.5")
        assert result.gates[2].comparisons["rvol_at_least_1_5"] is False


def test_fractional_volume_uses_exact_source_values(tmp_path):
    _, _, _, _, rows = breakout_example_inputs()
    for row in rows:
        row["volume"] = "0.1"
    rows[-1]["volume"] = "0.15"
    result = calculate(freeze(tmp_path, rows=rows))
    assert result.status == "PASS" and result.exact_volumes.prior_sum == 2


@pytest.mark.parametrize(
    "count,status", [(199, "UNAVAILABLE"), (200, "PASS"), (259, "PASS"), (260, "PASS")]
)
def test_technical_history_boundary_does_not_clear_foundation(tmp_path, count, status):
    result = calculate(freeze(tmp_path, count=count))
    assert result.status == status
    assert result.recommendation_status == "NOT_EVALUATED"
    if count == 199:
        assert result.gates[0].status == "PASS" and result.gates[2].status == "PASS"
        assert result.gates[1].status == "UNAVAILABLE"


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing-volume", "MISSING_VOLUME"),
        ("zero-baseline", "ZERO_DENOMINATOR"),
        ("missing-session", "MISSING_SESSION"),
        ("duplicate", "DUPLICATE_SESSION"),
        ("close", "INVALID_OHLC"),
        ("publication", "PUBLICATION_UNVERIFIED"),
        ("raw", "INCOMPATIBLE_PRICE_BASIS"),
    ],
)
def test_mandatory_evidence_never_becomes_false_or_neutral(tmp_path, mutation, reason):
    _, _, _, _, rows = breakout_example_inputs()
    kwargs = {}
    if mutation == "missing-volume":
        rows[-1].pop("volume")
    elif mutation == "zero-baseline":
        for row in rows[-21:-1]:
            row["volume"] = "0"
    elif mutation == "missing-session":
        rows.pop(-3)
    elif mutation == "duplicate":
        rows.insert(-3, dict(rows[-3]))
    elif mutation == "close":
        rows[-1]["close"] = "NaN"
    elif mutation == "publication":
        kwargs["flags"] = ("PUBLICATION_UNVERIFIED",)
    else:
        kwargs["adjustment"] = "raw"
    result = calculate(freeze(tmp_path, rows=rows, **kwargs))
    assert result.status == "UNAVAILABLE" and result.available_at is None
    assert any(reason in r for g in result.gates for r in g.unavailable_reasons)
    if mutation in ("missing-volume", "zero-baseline"):
        assert [g.status for g in result.gates[:3]] == ["PASS", "PASS", "UNAVAILABLE"]


def test_missing_evidence_precedes_but_does_not_hide_valid_failures(tmp_path):
    _, _, _, _, rows = breakout_example_inputs()
    rows[-1]["close"] = rows[-2]["high"]
    rows[-1].pop("volume")
    result = calculate(freeze(tmp_path, rows=rows))
    assert result.status == "UNAVAILABLE"
    assert result.gates[0].status == "FAIL"
    assert result.gates[0].failed_conditions == ("close_above_prior_high",)
    assert result.gates[2].comparisons == {"rvol_at_least_1_5": None}


def test_three_trend_comparisons_remain_one_gate(tmp_path):
    _, _, _, _, rows = breakout_example_inputs()
    for row in rows:
        row.update(open="100", high="101", low="99", close="100")
    result = calculate(freeze(tmp_path, rows=rows))
    gate = result.gates[1]
    assert gate.status == "FAIL" and len(gate.failed_conditions) == 3
    assert gate.comparisons == dict.fromkeys(
        ("close_above_sma50", "sma50_above_sma200", "sma50_rising"), False
    )
    assert len(result.gates) == 4 and result.gates[2].status == "PASS"


def test_default_unverified_cutoff_cannot_be_promoted_by_sidecar(tmp_path):
    result = calculate(freeze(tmp_path), cutoff=None)
    assert result.status == "UNAVAILABLE" and result.context_reasons == ("PUBLICATION_UNVERIFIED",)
    assert all(g.status == "PASS" for g in result.gates[:3])
    payload = result.model_dump(mode="json")
    payload["context_reasons"] = []
    with pytest.raises(ValidationError, match="blockers"):
        BreakoutAssessment.model_validate(payload)


def test_missing_latest_session_never_falls_back(tmp_path):
    _, _, _, _, rows = breakout_example_inputs()
    result = calculate(freeze(tmp_path, rows=rows[:-1]))
    assert result.observed_session.isoformat() == "2026-10-30"
    assert result.status == "UNAVAILABLE"
    assert all("MISSING_SESSION" in str(g.unavailable_reasons) for g in result.gates[:3])


def test_grace_boundary_targets_expected_session(tmp_path):
    _, _, _, calendar, rows = breakout_example_inputs()
    before = calendar.sessions[-1].closes_at + timedelta(minutes=14, seconds=59)
    frozen = freeze(tmp_path, rows=rows[:-1], as_of=before)
    previous = calculate(frozen)
    assert previous.observed_session == calendar.sessions[-2].session
    due = calculate(frozen, as_of=before + timedelta(seconds=1))
    assert due.observed_session == calendar.sessions[-1].session and due.status == "UNAVAILABLE"


def test_short_session_only_blocks_volume(tmp_path):
    market, _, _, calendar, rows = breakout_example_inputs()
    sessions = list(calendar.sessions)
    sessions[-2] = sessions[-2].model_copy(
        update={"opens_at": sessions[-2].opens_at + timedelta(hours=3)}
    )
    calendar = FixtureSessionCalendar(market, tuple(sessions))
    result = calculate(
        freeze_breakout_example(tmp_path, market=market, calendar=calendar, rows=rows)
    )
    assert [g.status for g in result.gates[:3]] == ["PASS", "PASS", "UNAVAILABLE"]
    assert result.gates[2].unavailable_reasons == ("SHORTENED_OR_INCOMPATIBLE_SESSION",)


def test_optional_weekly_unavailable_does_not_block_daily(tmp_path):
    result = calculate(
        freeze(tmp_path),
        cutoff=CutoffPolicy(publication_timing="synthetic", weekly_grace_seconds=180 * 86400),
    )
    assert result.weekly_context.classification == "UNAVAILABLE" and result.status == "PASS"
    assert result.available_at == result.as_of


def test_weekly_opposition_is_informational(tmp_path):
    _, _, _, calendar, rows = breakout_example_inputs()
    with localcontext() as ctx:
        ctx.prec = 100
        for i, row in enumerate(rows):
            close = (
                Decimal(140 if calendar.sessions[i].session.weekday() == 4 else 100)
                if i < 210
                else Decimal(110) + Decimal(i - 210) / 5
            )
            row.update(
                open=str(close),
                high=str(close + Decimal("0.1")),
                low=str(close - Decimal("0.1")),
                close=str(close),
            )
    result = calculate(freeze(tmp_path, rows=rows))
    assert result.weekly_context.classification == "OPPOSES"
    assert result.status == "PASS"


def test_spy_pass_and_disabled_input_never_changes_stock_profile(tmp_path):
    stock = freeze(tmp_path / "stock")
    kwargs = spy_inputs(tmp_path / "spy")
    variant = calculate(stock, "stock_with_spy", **kwargs)
    assert variant.status == "PASS" and variant.gates[3].status == "PASS"
    assert variant.spy_trend.observations[0].observed_session == variant.observed_session
    baseline = calculate(stock, **kwargs)
    assert baseline.semantic_bytes() == calculate(stock).semantic_bytes()
    assert baseline.gates[3].status == "NOT_ENABLED" and baseline.spy_trend is None
    assert (
        variant.benchmark_snapshot.metadata.instrument.instrument_id
        != variant.stock_snapshot.metadata.instrument.instrument_id
    )


def test_spy_valid_failure_preserves_passing_stock_gates(tmp_path):
    _, _, _, _, rows = breakout_example_inputs()
    for row in rows:
        row.update(open="100", high="101", low="99", close="100")
    result = calculate(
        freeze(tmp_path / "stock"), "stock_with_spy", **spy_inputs(tmp_path / "spy", rows=rows)
    )
    assert result.status == "FAIL"
    assert [g.status for g in result.gates] == ["PASS", "PASS", "PASS", "FAIL"]
    assert len(result.gates[3].failed_conditions) == 3


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing", "BENCHMARK_EVIDENCE_MISSING"),
        ("no-binding", "BENCHMARK_IDENTITY_UNVERIFIED"),
        ("no-provenance", "BENCHMARK_PROVENANCE_MISSING"),
        ("wrong-id", "INCOMPATIBLE_BENCHMARK_IDENTITY"),
        ("old-session", "MISSING_SESSION"),
        ("raw", "INCOMPATIBLE_BENCHMARK_SOURCE_OR_BASIS"),
        ("unverified", "PUBLICATION_UNVERIFIED"),
    ],
)
def test_spy_missing_or_incompatible_never_passes(tmp_path, mutation, reason):
    stock = freeze(tmp_path / "stock")
    _, _, _, _, rows = breakout_example_inputs()
    kwargs = spy_inputs(
        tmp_path / "spy",
        rows=rows[:-1] if mutation == "old-session" else rows,
        adjustment="raw" if mutation == "raw" else "split",
        flags=("PUBLICATION_UNVERIFIED",) if mutation == "unverified" else (),
    )
    if mutation == "missing":
        kwargs.pop("benchmark_snapshot")
    elif mutation == "no-binding":
        kwargs.pop("benchmark_binding")
    elif mutation == "no-provenance":
        kwargs.pop("benchmark_provenance")
    elif mutation == "wrong-id":
        binding = kwargs["benchmark_binding"]
        kwargs["benchmark_binding"] = binding.model_copy(
            update={"instrument": binding.instrument.model_copy(update={"instrument_id": uuid4()})}
        )
    result = calculate(stock, "stock_with_spy", **kwargs)
    assert result.status == "UNAVAILABLE"
    assert any(reason in r for r in result.gates[3].unavailable_reasons)
    assert all(g.status == "PASS" for g in result.gates[:3])


def test_registry_binding_rejects_wrong_ticker_or_venue():
    market, stock, spy, _, _ = breakout_example_inputs()
    with pytest.raises(ValueError):
        resolve_spy_binding(
            market, InstrumentRegistry((stock,)), verification_reference="synthetic://missing"
        )
    payload = resolve_spy_binding(
        market, InstrumentRegistry((spy,)), verification_reference="synthetic://SPY"
    ).model_dump(mode="json")
    payload["instrument"]["venue"] = "XNAS"
    with pytest.raises(ValidationError):
        BenchmarkBinding.model_validate(payload)


def test_ambient_decimal_settings_and_later_replay_are_identical(tmp_path):
    stock = freeze(tmp_path / "stock")
    kwargs = spy_inputs(tmp_path / "spy")
    original = calculate(stock, "stock_with_spy", **kwargs)
    with localcontext() as ctx:
        ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax = 2, ROUND_UP, -1, 1
        ctx.traps[Inexact] = ctx.traps[Rounded] = True
        assert (
            calculate(stock, "stock_with_spy", **kwargs).semantic_bytes()
            == original.semantic_bytes()
        )
    assert (
        calculate(
            stock, "stock_with_spy", calculated_at=stock[-1] + timedelta(days=1), **kwargs
        ).semantic_bytes()
        == original.semantic_bytes()
    )


def test_future_appends_and_corrections_preserve_original_replay(tmp_path):
    _, _, _, _, rows = breakout_example_inputs()
    original_inputs = freeze(tmp_path / "original", rows=rows)
    original = calculate(original_inputs)
    rows.append(
        dict(
            rows[-1],
            session="2026-11-02",
            timestamp="2026-11-02T20:00:00+00:00",
            volume="999999999",
        )
    )
    appended = calculate(freeze(tmp_path / "future", rows=rows))
    assert appended.status == original.status and appended.exact_volumes == original.exact_volumes
    rows[-2]["volume"] = "1"
    corrected = calculate(freeze(tmp_path / "corrected", rows=rows))
    assert (
        corrected.status == "FAIL"
        and calculate(original_inputs).semantic_bytes() == original.semantic_bytes()
    )


def test_tampered_evidence_stops_before_any_qualification(tmp_path):
    frozen = freeze(tmp_path)
    path = Path(frozen[0].reference)
    path.chmod(0o600)
    path.write_text("{}")
    with pytest.raises(ValueError):
        calculate(frozen)


def test_serialized_gate_and_identity_tampering_rejected(tmp_path):
    result = calculate(freeze(tmp_path))
    for field, value in (
        ("status", "FAIL"),
        ("policy_sha256", "0" * 64),
        ("observed_session", "2026-10-29"),
        ("recommendation_status", "BUY"),
    ):
        payload = result.model_dump(mode="json")
        payload[field] = value
        with pytest.raises(ValidationError):
            BreakoutAssessment.model_validate(payload)
    payload = result.model_dump(mode="json")
    payload["gates"][2]["comparisons"]["rvol_at_least_1_5"] = False
    with pytest.raises(ValidationError):
        BreakoutAssessment.model_validate(payload)


def test_walkthrough_replays_in_fresh_processes(tmp_path):
    cmd = [sys.executable, "-m", "stock_analyser.breakout_example", "--output", str(tmp_path)]
    one = subprocess.check_output(cmd, text=True, env=os.environ)
    files = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    two = subprocess.check_output(cmd, text=True, env=os.environ)
    assert one == two and files == {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    assert json.loads(files["pass.json"])["status"] == "PASS"
    assert json.loads(files["missing-volume.json"])["status"] == "UNAVAILABLE"


def test_replay_authenticates_exact_volumes_not_only_rounded_observation(tmp_path):
    from stock_analyser.breakout import replay_breakout

    _, _, _, _, rows = breakout_example_inputs()
    rows[-1]["volume"] = "1499999." + "9" * 60
    frozen = freeze(tmp_path, rows=rows)
    result = calculate(frozen)
    replay = replay_breakout(result, frozen[2], calculated_at=frozen[-1] + timedelta(days=1))
    assert replay.semantic_bytes() == result.semantic_bytes()
    # This forged volume has the same rounded RVOL, but the opposite exact decision.
    payload = result.model_dump(mode="json")
    payload["exact_volumes"]["current_volume"] = "1500000"
    payload["gates"][2].update(
        status="PASS", comparisons={"rvol_at_least_1_5": True}, failed_conditions=[]
    )
    payload["status"] = "PASS"
    forged = BreakoutAssessment.model_validate(payload)
    with pytest.raises(ValueError, match="authenticated evidence replay"):
        replay_breakout(forged, frozen[2], calculated_at=frozen[-1])


def test_unsupported_calendar_and_other_market_stay_unavailable(tmp_path):
    frozen = freeze(tmp_path / "calendar")

    class Unsupported:
        def data_version(self, market, venue):
            raise ValueError("unsupported")

    result = calculate((frozen[0], frozen[1], Unsupported(), frozen[-1]))
    assert result.status == "UNAVAILABLE" and "UNSUPPORTED_CALENDAR" in result.context_reasons
    assert result.weekly_context.classification == "UNAVAILABLE"
    from stock_analyser.fixtures import synthetic_markets

    _, _, adapters = synthetic_markets()
    market, instrument = adapters[1].market, adapters[1].listings[0]
    # Same dates as the US fixture, but correct exchange-local session labels for TEST.
    sessions = tuple(
        s.model_copy(
            update={
                "opens_at": s.opens_at - timedelta(hours=9),
                "closes_at": s.closes_at - timedelta(hours=9),
            }
        )
        for s in frozen[2].sessions
    )
    calendar = FixtureSessionCalendar(market, sessions)
    _, _, _, _, rows = breakout_example_inputs()
    for row, session in zip(rows, sessions, strict=True):
        row["timestamp"] = session.closes_at.isoformat()
    other = freeze_breakout_example(
        tmp_path / "other",
        rows=rows,
        calendar=calendar,
        market=market,
        instrument=instrument,
        as_of=sessions[-1].closes_at + timedelta(hours=1),
    )
    blocked = calculate(other)
    assert (
        blocked.status == "UNAVAILABLE" and "UNSUPPORTED_STRATEGY_MARKET" in blocked.context_reasons
    )


def test_future_publication_blocks_all_affected_groups(tmp_path):
    snapshot, prov, calendar, stamp = freeze(tmp_path)
    pubs = list(prov.publications)
    pubs[-1] = pubs[-1].model_copy(update={"verified_at": stamp + timedelta(minutes=1)})
    prov = prov.model_copy(update={"publications": tuple(pubs)})
    result = calculate((snapshot, prov, calendar, stamp))
    assert result.status == "UNAVAILABLE"
    assert all("INPUT_NOT_YET_AVAILABLE" in str(g.unavailable_reasons) for g in result.gates[:3])


def test_reordering_cannot_hide_duplicate_or_missing_dates(tmp_path):
    _, _, _, _, rows = breakout_example_inputs()
    result = calculate(freeze(tmp_path, rows=list(reversed(rows))))
    assert result.status == "UNAVAILABLE"
    assert all("UNORDERED_SESSIONS" in str(g.unavailable_reasons) for g in result.gates[:3])


def test_benchmark_publication_and_source_conflicts_are_retained(tmp_path):
    from stock_analyser.contracts import ProviderCapabilities, ProviderRoute, SymbolMapping

    market, stock, spy, calendar, rows = breakout_example_inputs()
    feed = market.route("prices").feed
    market = market.model_copy(
        update={
            "routes": (
                *market.routes,
                ProviderRoute(role="long_history_prices", provider="fixture-alt", feed=feed),
            ),
            "capabilities": (
                *market.capabilities,
                ProviderCapabilities(provider="fixture-alt", daily_prices=True),
            ),
        }
    )
    calendar = FixtureSessionCalendar(market, calendar.sessions)
    stock_inputs = freeze_breakout_example(
        tmp_path / "stock", market=market, calendar=calendar, rows=rows
    )
    # A configured alternate source cannot silently replace the stock's selected source.
    spy = spy.model_copy(
        update={
            "provider_symbols": (
                *spy.provider_symbols,
                SymbolMapping(
                    provider="fixture-alt",
                    symbol="SPY.ALT",
                    effective_from=spy.provider_symbols[0].effective_from,
                ),
            )
        }
    )
    alternate_market = market.model_copy(
        update={
            "routes": tuple(
                r.model_copy(update={"provider": "fixture-alt"}) if r.role == "prices" else r
                for r in market.routes
            )
        }
    )
    alternate_calendar = FixtureSessionCalendar(alternate_market, calendar.sessions)
    benchmark, prov, _, _ = freeze_breakout_example(
        tmp_path / "spy",
        market=alternate_market,
        instrument=spy,
        calendar=alternate_calendar,
        rows=rows,
    )
    binding = resolve_spy_binding(
        market, InstrumentRegistry((stock, spy)), verification_reference="synthetic://inventory"
    )
    result = calculate(
        stock_inputs,
        "stock_with_spy",
        benchmark_binding=binding,
        benchmark_snapshot=benchmark,
        benchmark_provenance=prov,
    )
    assert result.status == "UNAVAILABLE"
    assert "INCOMPATIBLE_BENCHMARK_SOURCE_OR_BASIS" in result.gates[3].unavailable_reasons


def test_actual_older_benchmark_snapshot_cannot_substitute_for_t(tmp_path):
    _, _, _, calendar, rows = breakout_example_inputs()
    kwargs = spy_inputs(
        tmp_path / "spy", rows=rows[:-1], as_of=calendar.sessions[-2].closes_at + timedelta(hours=1)
    )
    result = calculate(freeze(tmp_path / "stock"), "stock_with_spy", **kwargs)
    assert result.status == "UNAVAILABLE"
    assert result.observed_session == calendar.sessions[-1].session
    assert result.spy_trend.observations[0].observed_session == result.observed_session
    assert "MISSING_SESSION" in str(result.gates[3].unavailable_reasons)


def test_exact_stock_trend_changes_below_display_precision(tmp_path):
    _, _, _, _, rows = breakout_example_inputs()
    for row in rows:
        row.update(open="100", high="101", low="99", close="100")
    rows[-1]["close"] = "100." + "0" * 59 + "1"
    result = calculate(freeze(tmp_path, rows=rows))
    assert all(o.value == 100 for o in result.stock_trend.observations)
    assert result.gates[1].status == "PASS"  # All three exact comparisons are positive.
    assert result.gates[0].status == "FAIL" and result.status == "FAIL"


def test_equal_payloads_for_distinct_instruments_keep_distinct_snapshot_ids(tmp_path):
    stock = freeze(tmp_path / "stock")[0]
    spy = spy_inputs(tmp_path / "spy")["benchmark_snapshot"]
    assert stock.metadata.sources[0].sha256 == spy.metadata.sources[0].sha256
    assert stock.metadata.snapshot_id != spy.metadata.snapshot_id


@pytest.mark.parametrize("profile", ["stock", "stock_with_spy"])
@pytest.mark.parametrize("state", ["PASS", "FAIL", "UNAVAILABLE"])
def test_reordered_comparison_json_roundtrips_and_replays(tmp_path, profile, state):
    from stock_analyser.breakout import replay_breakout

    _, _, _, _, rows = breakout_example_inputs()
    if state != "PASS":
        for row in rows:
            row.update(open="100", high="101", low="99", close="100")
    if state == "UNAVAILABLE":
        rows.pop(-150)  # SMA200 is unavailable; the other two trend comparisons still fail.
    inputs = freeze(tmp_path / "stock", rows=rows)
    kwargs = spy_inputs(tmp_path / "spy", rows=rows) if profile == "stock_with_spy" else {}
    original = calculate(inputs, profile, **kwargs)
    assert original.gates[1].status == state
    if profile == "stock_with_spy":
        assert original.gates[3].status == state
    payload = original.model_dump(mode="json")
    for gate in payload["gates"]:
        gate["comparisons"] = dict(reversed(tuple(gate["comparisons"].items())))
    restored = BreakoutAssessment.model_validate_json(json.dumps(payload))
    assert restored == original
    assert restored.semantic_bytes() == original.semantic_bytes()
    assert [g.failed_conditions for g in restored.gates] == [
        g.failed_conditions for g in original.gates
    ]
    replay = replay_breakout(restored, inputs[2], calculated_at=inputs[-1] + timedelta(seconds=1))
    assert replay.semantic_bytes() == original.semantic_bytes()


@pytest.mark.parametrize("group", ["stock_trend", "spy_trend"])
@pytest.mark.parametrize("mutation", ["missing", "unexpected"])
def test_comparison_key_set_still_rejects_invalid_keys(group, mutation):
    from stock_analyser.breakout import GateResult

    comparisons = {
        "sma50_rising": True,
        "sma50_above_sma200": True,
        "close_above_sma50": True,
    }
    if mutation == "missing":
        comparisons.pop("sma50_rising")
    else:
        comparisons["unapproved_condition"] = True
    with pytest.raises(ValidationError, match="comparisons differ"):
        GateResult(group=group, status="PASS", comparisons=comparisons)


@pytest.mark.parametrize("group", ["stock_trend", "spy_trend"])
def test_gate_construction_retains_canonical_failure_order(group):
    from stock_analyser.breakout import GateResult, _gate

    gate = _gate(
        group,
        {"sma50_rising": False, "sma50_above_sma200": None, "close_above_sma50": False},
        ("SMA200_UNAVAILABLE",),
    )
    assert gate.status == "UNAVAILABLE"
    assert gate.failed_conditions == ("close_above_sma50", "sma50_rising")
    payload = gate.model_dump(mode="json")
    payload["failed_conditions"].reverse()
    with pytest.raises(ValidationError, match="retain all failed comparisons"):
        GateResult.model_validate(payload)
