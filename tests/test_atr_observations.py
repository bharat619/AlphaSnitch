import json
import os
import subprocess
import sys
from datetime import date, timedelta
from decimal import ROUND_UP, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest
from pydantic import ValidationError

from stock_analyser.atr import (
    ATRCheckpoint,
    ATRObservation,
    ATRSpec,
    authenticate_checkpoint,
    canonical_atr,
    declare_atr_series,
    observe_atr,
)
from stock_analyser.atr_example import (
    atr_example_inputs,
    example_series,
    freeze_atr_example,
)
from stock_analyser.calendars import ExchangeCalendarAdapter
from stock_analyser.indicator_example import example_inputs
from stock_analyser.indicators import _decimal
from stock_analyser.markets import US_MARKET


def inputs(tmp_path, *, count=260, rows=None, calendar=None, **kwargs):
    if calendar is None:
        calendar, default_rows = atr_example_inputs(300)
        rows = default_rows[:count] if rows is None else rows
    return freeze_atr_example(
        tmp_path, calendar=calendar, rows=rows, day=calendar.sessions[count - 1].session, **kwargs
    )


def calculate(frozen, *, series=None, checkpoint=None, **kwargs):
    snapshot, provenance, calendar, stamp = frozen
    return observe_atr(
        snapshot,
        series or example_series(calendar),
        provenance,
        calendar,
        stamp.date(),
        as_of=stamp,
        calculated_at=stamp,
        checkpoint=checkpoint,
        **kwargs,
    )


def checkpoint_at(tmp_path, count=260, *, calendar=None, rows=None):
    frozen = inputs(tmp_path, count=count, calendar=calendar, rows=rows)
    result = calculate(frozen)
    return ATRCheckpoint(observation=result, created_at=frozen[-1])


def test_history_and_exchange_anchor():
    req = ATRSpec().history_requirement
    assert (
        req.minimum_bars,
        req.warmup_bars,
        req.required_bars,
        req.adjustment,
        req.mandatory,
    ) == (
        15,
        245,
        260,
        "split",
        True,
    )
    _, instrument, _, _ = example_inputs()
    route = US_MARKET.route("prices")
    series = declare_atr_series(
        instrument,
        US_MARKET,
        route.provider,
        route.feed,
        ExchangeCalendarAdapter(),
        date(2026, 11, 13),
        policy_series_id="test",
    )
    assert series.anchor_session == date(2025, 11, 3)
    assert series.first_assessment_session == date(2026, 11, 13)


@pytest.mark.parametrize(
    "count,value,updates,ready",
    [
        (14, None, None, False),
        (15, Decimal(2), 0, False),
        (259, Decimal(2), 244, False),
        (260, Decimal(2), 245, True),
        (261, Decimal(2), 246, True),
    ],
)
def test_boundaries(tmp_path, count, value, updates, ready):
    result = calculate(inputs(tmp_path, count=count))
    assert (result.value, result.update_count, result.strategy_ready) == (value, updates, ready)
    assert (result.status == "AVAILABLE") == (value is not None)
    assert result.readiness_reasons == (
        ()
        if ready
        else ("WARMUP_INCOMPLETE",)
        if value is not None
        else ("INSUFFICIENT_SEED_HISTORY",)
    )
    assert ATRObservation.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("low,high,close", [(102, 104, 103), (96, 98, 97)])
def test_gaps_use_real_previous_close(tmp_path, low, high, close):
    calendar, rows = atr_example_inputs(300)
    rows[15].update(open=str(close), high=str(high), low=str(low), close=str(close))
    frozen = inputs(tmp_path, count=16, calendar=calendar, rows=rows[:16])
    result = calculate(frozen)
    assert result.value == _decimal(Fraction(15, 7))
    assert result.previous_close == close


def test_flat_is_available_zero_not_positive_risk(tmp_path):
    calendar, rows = atr_example_inputs(300)
    for row in rows:
        row.update(open="100", high="100", low="100", close="100")
    result = calculate(inputs(tmp_path, calendar=calendar, rows=rows[:260]))
    assert result.value == 0 and result.status == "AVAILABLE" and result.strategy_ready


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing", "MISSING_SESSION"),
        ("duplicate", "DUPLICATE_SESSION"),
        ("close", "INVALID_OHLC"),
        ("timestamp", "INVALID_BAR_PROVENANCE"),
        ("incomplete", "INVALID_BAR_PROVENANCE"),
        ("basis", "INCOMPATIBLE_PRICE_BASIS"),
        ("publication", "PUBLICATION_UNVERIFIED"),
        ("numeric-limit", "NUMERIC_RESOURCE_LIMIT"),
    ],
)
def test_invalid_inputs_never_reseed(tmp_path, mutation, reason):
    calendar, rows = atr_example_inputs(300)
    rows = rows[:260]
    kwargs = {}
    if mutation == "missing":
        rows.pop(0)
    elif mutation == "duplicate":
        rows.append(dict(rows[30]))
    elif mutation == "close":
        rows[40]["close"] = "NaN"
    elif mutation == "timestamp":
        rows[40]["timestamp"] = rows[41]["timestamp"]
    elif mutation == "incomplete":
        rows[40]["completed"] = False
    elif mutation == "basis":
        kwargs["adjustment"] = "raw"
    elif mutation == "publication":
        kwargs["flags"] = ("PUBLICATION_UNVERIFIED",)
    elif mutation == "numeric-limit":
        rows[40]["high"] = "1e201"
    result = calculate(inputs(tmp_path, calendar=calendar, rows=rows, **kwargs))
    assert result.status == "UNAVAILABLE" and reason in result.reasons
    assert result.value is None and not result.strategy_ready


def test_missing_volume_and_short_sessions_do_not_block_atr(tmp_path):
    calendar, rows = atr_example_inputs(300)
    for row in rows:
        row.pop("volume")
    result = calculate(inputs(tmp_path, calendar=calendar, rows=rows[:260]))
    assert result.status == "AVAILABLE" and result.value == 2


def test_publication_and_incompatible_provenance(tmp_path):
    frozen = inputs(tmp_path)
    snapshot, provenance, calendar, stamp = frozen
    incomplete = provenance.model_copy(update={"publications": provenance.publications[1:]})
    result = calculate((snapshot, incomplete, calendar, stamp))
    assert "PUBLICATION_UNVERIFIED" in result.reasons
    future = provenance.publications[0].model_copy(
        update={"verified_at": stamp + timedelta(days=1)}
    )
    p = provenance.model_copy(update={"publications": (future, *provenance.publications[1:])})
    assert "INPUT_NOT_YET_AVAILABLE" in calculate((snapshot, p, calendar, stamp)).reasons
    p = provenance.model_copy(update={"snapshot_sha256": "0" * 64})
    assert "INCOMPATIBLE_PROVENANCE" in calculate((snapshot, p, calendar, stamp)).reasons


def test_arithmetic_independent_of_ambient_context(tmp_path):
    calendar, rows = atr_example_inputs(300)
    rows[200]["high"] = "103.123456789123456789"
    frozen = inputs(tmp_path, calendar=calendar, rows=rows[:260])
    expected = calculate(frozen)
    with localcontext() as ctx:
        ctx.prec = 6
        ctx.rounding = ROUND_UP
        ctx.Emax, ctx.Emin = 9, -9
        ctx.traps[Inexact] = ctx.traps[Rounded] = True
        assert calculate(frozen).semantic_bytes() == expected.semantic_bytes()
        assert ATRObservation.model_validate_json(expected.model_dump_json()) == expected


def integer_round(value, digits=50):
    """Independent integer quotient/remainder significant-digit half-even reference."""
    if not value:
        return Decimal(0)
    n, d = value.numerator, value.denominator
    exponent = len(str(n)) - len(str(d))
    if (n < d * 10**exponent) if exponent >= 0 else (n * 10 ** (-exponent) < d):
        exponent -= 1
    shift = digits - 1 - exponent
    q, r = divmod(n * 10 ** max(0, shift), d * 10 ** max(0, -shift))
    denominator = d * 10 ** max(0, -shift)
    if 2 * r > denominator or (2 * r == denominator and q % 2):
        q += 1
    return Decimal(f"{q}e{-shift}")


@pytest.mark.parametrize("scale", [Fraction(1, 10**60), Fraction(1), Fraction(10**60)])
def test_long_recurrence_against_independent_and_exact_references(scale):
    ranges = tuple(Fraction((37 * i) % 101, 10) * scale for i in range(1014))
    exact = sum(ranges[:14]) / 14
    independent = integer_round(exact)
    max_error = Fraction()
    for tr in ranges[14:]:
        exact = (13 * exact + tr) / 14
        independent = integer_round((13 * Fraction(independent) + tr) / 14)
        max_error = max(max_error, abs(Fraction(independent) - exact))
    assert canonical_atr(ranges) == independent
    assert max_error < scale / 10**47  # fixture-specific measurement, not a policy tolerance


@pytest.mark.parametrize("count", [15, 259, 260, 281])
def test_checkpoint_continuation_exact_and_authenticated(tmp_path, count):
    calendar, rows = atr_example_inputs(300)
    rows[count].update(open="103", high="104", low="102", close="103")
    checkpoint = checkpoint_at(tmp_path / "old", count, calendar=calendar, rows=rows[:count])
    frozen = inputs(tmp_path / "new", count=count + 1, calendar=calendar, rows=rows[: count + 1])
    continued = calculate(frozen, checkpoint=checkpoint)
    replay = calculate(frozen)
    assert continued.calculation_mode == "CONTINUED" and not continued.checkpoint_rejections
    assert continued.value == replay.value == _decimal(Fraction(15, 7))
    assert continued.consumed_prefix_sha256 == replay.consumed_prefix_sha256
    assert continued.update_count == count - 14
    authenticate_checkpoint(checkpoint, calendar)


@pytest.mark.parametrize(
    "field,new",
    [
        ("value", Decimal(3)),
        ("value", Decimal("2.0")),
        ("previous_close", Decimal(99)),
        ("update_count", 244),
        ("consumed_prefix_sha256", "0" * 64),
    ],
)
def test_forged_state_replays_instead_of_trusting_checksum(tmp_path, field, new):
    checkpoint = checkpoint_at(tmp_path / "old")
    bad = checkpoint.model_copy(
        update={"observation": checkpoint.observation.model_copy(update={field: new})}
    )
    result = calculate(inputs(tmp_path / "new", count=261), checkpoint=bad)
    assert result.calculation_mode == "REPLAYED" and result.value == 2
    assert "CHECKPOINT_AUTHENTICATION_FAILED" in result.checkpoint_rejections


def test_correction_replays_same_anchor_and_preserves_original(tmp_path):
    checkpoint = checkpoint_at(tmp_path / "old")
    calendar, rows = atr_example_inputs(300)
    rows[30]["high"] = "103"
    frozen = inputs(
        tmp_path / "corrected", count=261, calendar=calendar, rows=rows[:261], revision="corrected"
    )
    result = calculate(frozen, checkpoint=checkpoint)
    assert result.calculation_mode == "REPLAYED"
    assert "CHECKPOINT_CONSUMED_HISTORY_CHANGED" in result.checkpoint_rejections
    assert result.value == calculate(frozen).value and result.value > 2
    assert result.series.anchor_session == checkpoint.observation.series.anchor_session
    authenticate_checkpoint(checkpoint, calendar)
    assert checkpoint.observation.value == 2
    missing = inputs(
        tmp_path / "missing", count=261, calendar=calendar, rows=rows[1:261], revision="corrected"
    )
    unavailable = calculate(missing, checkpoint=checkpoint)
    assert unavailable.status == "UNAVAILABLE" and "MISSING_SESSION" in unavailable.reasons
    assert unavailable.checkpoint_rejections


@pytest.mark.parametrize("count", [260, 262])
def test_duplicate_and_skipped_session_require_full_replay(tmp_path, count):
    checkpoint = checkpoint_at(tmp_path / "old")
    result = calculate(inputs(tmp_path / "new", count=count), checkpoint=checkpoint)
    assert "CHECKPOINT_NOT_NEXT_SESSION" in result.checkpoint_rejections
    assert result.calculation_mode == "REPLAYED"
    assert result.update_count == count - 15


def test_future_appends_and_input_order_do_not_change_prefix_or_value(tmp_path):
    calendar, rows = atr_example_inputs(300)
    original = calculate(inputs(tmp_path / "old", count=260, calendar=calendar, rows=rows[:260]))
    future = calculate(inputs(tmp_path / "future", count=260, calendar=calendar, rows=rows))
    assert (original.value, original.consumed_prefix_sha256) == (
        future.value,
        future.consumed_prefix_sha256,
    )
    reordered = calculate(
        inputs(tmp_path / "reordered", count=260, calendar=calendar, rows=list(reversed(rows)))
    )
    assert "UNORDERED_SESSIONS" in reordered.reasons and reordered.value is None
    old_anchor = original.series.anchor_session
    new_series = declare_atr_series(
        original.series.instrument,
        original.series.market,
        original.series.provider,
        original.series.feed,
        calendar,
        calendar.sessions[260].session,
        policy_series_id="new",
    )
    assert (
        new_series.anchor_session > old_anchor and new_series.identity != original.series.identity
    )


def test_integrity_failure_is_not_salvaged(tmp_path):
    frozen = inputs(tmp_path)
    from pathlib import Path

    Path(frozen[0].reference).chmod(0o600)
    Path(frozen[0].reference).write_text("{}")
    with pytest.raises(ValueError):
        calculate(frozen)


def test_example_repeats_in_fresh_processes(tmp_path):
    command = [sys.executable, "-m", "stock_analyser.atr_example", "--output", str(tmp_path)]
    one = subprocess.check_output(command, env=os.environ, text=True)
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    two = subprocess.check_output(command, env=os.environ, text=True)
    assert one == two
    assert before == {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    assert "mode=CONTINUED" in one and "mode=REPLAYED" in one
    assert json.loads((tmp_path / "bar-260.json").read_text())["update_count"] == 245


def test_continuation_ignores_ambient_decimal_and_replay_time(tmp_path):
    calendar, rows = atr_example_inputs(300)
    rows[70]["high"] = "105.123456789"
    cp = checkpoint_at(tmp_path / "old", calendar=calendar, rows=rows[:260])
    frozen = inputs(tmp_path / "next", count=261, calendar=calendar, rows=rows[:261])
    expected = calculate(frozen, checkpoint=cp)
    with localcontext() as ctx:
        ctx.prec, ctx.rounding, ctx.Emin, ctx.Emax = 3, ROUND_UP, -2, 2
        ctx.traps[Inexact] = ctx.traps[Rounded] = True
        assert calculate(frozen, checkpoint=cp).semantic_bytes() == expected.semantic_bytes()
    snapshot, provenance, calendar, stamp = frozen
    later = observe_atr(
        snapshot,
        expected.series,
        provenance,
        calendar,
        stamp.date(),
        as_of=stamp,
        calculated_at=stamp + timedelta(days=1),
        checkpoint=cp,
    )
    assert later.semantic_bytes() == expected.semantic_bytes()


def test_missing_checkpoint_source_replays_current_complete_history(tmp_path):
    from pathlib import Path

    cp = checkpoint_at(tmp_path / "old")
    path = Path(cp.observation.snapshot.reference)
    path.unlink()
    result = calculate(inputs(tmp_path / "new", count=261), checkpoint=cp)
    assert result.calculation_mode == "REPLAYED" and result.value == 2
    assert result.checkpoint_rejections == ("CHECKPOINT_AUTHENTICATION_FAILED",)


@pytest.mark.parametrize("change", ["reference-close", "revision-only", "publication-only"])
def test_every_consumed_lineage_change_invalidates_continuation(tmp_path, change):
    cp = checkpoint_at(tmp_path / "old")
    calendar, rows = atr_example_inputs(300)
    if change == "reference-close":
        rows[0]["close"] = "100.5"
    frozen = inputs(
        tmp_path / "new",
        count=261,
        calendar=calendar,
        rows=rows[:261],
        revision="revised" if change == "revision-only" else "synthetic-split-1",
    )
    if change == "publication-only":
        snapshot, prov, calendar, stamp = frozen
        first = prov.publications[0].model_copy(
            update={"verification_reference": "synthetic://correction"}
        )
        prov = prov.model_copy(update={"publications": (first, *prov.publications[1:])})
        frozen = (snapshot, prov, calendar, stamp)
    result = calculate(frozen, checkpoint=cp)
    assert result.calculation_mode == "REPLAYED"
    assert result.checkpoint_rejections == ("CHECKPOINT_CONSUMED_HISTORY_CHANGED",)
    assert result.value == calculate(frozen).value


def test_configuration_change_and_future_checkpoint_are_rejected(tmp_path):
    cp = checkpoint_at(tmp_path / "old")
    frozen = inputs(tmp_path / "new", count=261)
    series = example_series(frozen[2]).model_copy(update={"policy_series_id": "different-policy"})
    result = calculate(frozen, series=series, checkpoint=cp)
    assert "CHECKPOINT_CONFIGURATION_MISMATCH" in result.checkpoint_rejections
    assert result.calculation_mode == "REPLAYED"
    future = cp.model_copy(update={"created_at": frozen[-1] + timedelta(days=1)})
    result = calculate(frozen, checkpoint=future)
    assert result.checkpoint_rejections == ("CHECKPOINT_CREATED_AFTER_CALCULATION",)


def test_changed_anchor_or_numeric_contract_is_unavailable(tmp_path):
    frozen = inputs(tmp_path)
    series = example_series(frozen[2])
    changed = series.model_copy(update={"anchor_session": frozen[2].sessions[1].session})
    assert "INVALID_ANCHOR_OR_CALENDAR" in calculate(frozen, series=changed).reasons
    with pytest.raises(ValidationError):
        ATRSpec(numeric_version="another-version")


def test_identity_mismatch_blocks_usable_state(tmp_path):
    frozen = inputs(tmp_path)
    series = example_series(frozen[2]).model_copy(update={"provider": "wrong-provider"})
    result = calculate(frozen, series=series)
    assert "INCOMPATIBLE_SERIES_IDENTITY" in result.reasons
    assert result.value is None


@pytest.mark.parametrize("scale", [-60, 60])
def test_extreme_scales_pass_through_evidence_engine(tmp_path, scale):
    calendar, rows = atr_example_inputs(300)
    for row in rows:
        row.update(
            open=f"100e{scale}", high=f"101e{scale}", low=f"99e{scale}", close=f"100e{scale}"
        )
    result = calculate(inputs(tmp_path, calendar=calendar, rows=rows[:260]))
    assert result.value == Decimal(f"2e{scale}")


def test_adding_older_history_never_moves_seed(tmp_path):
    frozen = inputs(tmp_path / "old")
    old = calculate(frozen)
    calendar, rows = atr_example_inputs(400)
    # The additional older bars differ materially; they must not enter this series.
    for row in rows[:100]:
        row.update(open="100", high="200", low="90", close="100")
    stamp = frozen[-1]
    extended = freeze_atr_example(
        tmp_path / "extended", calendar=calendar, rows=rows[:360], day=stamp.date()
    )
    new = calculate(extended, series=old.series)
    assert new.value == old.value and new.consumed_prefix_sha256 == old.consumed_prefix_sha256


def test_canonical_boundary_is_not_infinite_precision_equivalence():
    exact = Fraction(15, 7)
    canonical = canonical_atr((*([Fraction(2)] * 14), Fraction(4)))
    midpoint = (exact + Fraction(canonical)) / 2
    assert (exact > midpoint) != (Fraction(canonical) > midpoint)
    # Half-even is checked without Decimal quantization or ambient-context helpers.
    for numerator in (10**50 + 5, 10**50 + 15):
        value = Fraction(numerator, 10**50)
        assert _decimal(value) == integer_round(value)


def test_shortened_price_session_is_valid(tmp_path):
    from stock_analyser.fixtures import FixtureSessionCalendar

    calendar, rows = atr_example_inputs(300)
    session = calendar.sessions[50]
    shortened = session.model_copy(update={"opens_at": session.opens_at + timedelta(hours=3)})
    calendar = FixtureSessionCalendar(
        example_series(calendar).market,
        (*calendar.sessions[:50], shortened, *calendar.sessions[51:]),
    )
    assert calculate(inputs(tmp_path, calendar=calendar, rows=rows[:260])).status == "AVAILABLE"


def test_continuation_history_request_grows_from_the_same_anchor():
    from stock_analyser.atr import atr_history_requirement
    from stock_analyser.universe import SelectionPolicy

    calendar, _ = atr_example_inputs(300)
    series = example_series(calendar)
    assert (
        atr_history_requirement(series, calendar, calendar.sessions[259].session).required_bars
        == 260
    )
    extended = atr_history_requirement(series, calendar, calendar.sessions[280].session)
    assert extended.required_bars == 281 and extended.adjustment == "split" and extended.mandatory
    assert SelectionPolicy().minimum_daily_bars == 260
