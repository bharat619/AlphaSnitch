import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_UP, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction

import pytest
from pydantic import ValidationError

from stock_analyser.calendars import ExchangeCalendarAdapter
from stock_analyser.indicator_example import freeze_example
from stock_analyser.indicators import (
    DailyTrendObservation,
    ObservationSpec,
    SMAObservation,
    SMASpec,
    daily_trend_specs,
    observe,
    observe_daily_trend,
    sma_history_requirement,
)
from stock_analyser.markets import US_MARKET
from stock_analyser.sma_example import freeze_sma_example, sma_example_inputs


def calculate(inputs, spec=None, *, day=None, as_of=None, calculated_at=None):
    snapshot, provenance, calendar, stamp = inputs
    return observe(
        snapshot,
        SMASpec(period=50) if spec is None else spec,
        provenance,
        calendar,
        stamp.date() if day is None else day,
        as_of=stamp if as_of is None else as_of,
        calculated_at=stamp if calculated_at is None else calculated_at,
    )


def trend(inputs):
    snapshot, provenance, calendar, stamp = inputs
    return observe_daily_trend(
        snapshot,
        provenance,
        calendar,
        stamp.date(),
        as_of=stamp,
        calculated_at=stamp,
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("period", 0),
        ("period", -1),
        ("period", True),
        ("period", "50"),
        ("period", 1.5),
        ("session_offset", -1),
        ("session_offset", True),
        ("session_offset", "10"),
        ("session_offset", 1.5),
    ],
)
def test_explicit_sma_parameters(field, value):
    parameters = {"period": 50, field: value}
    with pytest.raises(ValidationError):
        SMASpec(**parameters)


def test_sma_dependencies_do_not_change_universe_floor():
    from stock_analyser.universe import SelectionPolicy

    specs = daily_trend_specs()
    assert [s.history_requirement.required_bars for s in specs] == [50, 200, 60]
    combined = sma_history_requirement(specs)
    assert combined.minimum_bars == 200
    assert combined.adjustment == "split" and combined.timeframe == "daily"
    assert combined.warmup_bars == 0
    assert SelectionPolicy().minimum_daily_bars == 260
    with pytest.raises(ValueError, match="At least one"):
        sma_history_requirement(())


def test_hand_calculated_sma_and_trend(tmp_path):
    inputs = freeze_sma_example(tmp_path)
    result = trend(inputs)
    assert [o.value for o in result.observations] == [
        Decimal("274.5"),
        Decimal("199.5"),
        Decimal("264.5"),
    ]
    assert [o.exact_sum.fraction() for o in result.observations] == [13725, 39900, 13225]
    assert result.confirmed is True
    assert all(result.comparisons().values())
    first, _, lag = result.observations
    assert first.window_sessions[-1] == inputs[-1].date()
    assert first.assessment_close == 299
    assert lag.assessment_close is None
    assert lag.window_sessions == tuple(s.session for s in inputs[2].sessions[-60:-10])
    assert first.historical_availability == "verified"
    assert first.available_at == inputs[-1]
    assert SMAObservation.model_validate_json(first.model_dump_json()) == first
    assert DailyTrendObservation.model_validate_json(result.model_dump_json()) == result


def test_small_fixture_includes_current_close(tmp_path):
    calendar, rows = sma_example_inputs(3)
    for i, row in enumerate(rows):
        row.update(open=str(i + 101), low=str(i + 101), high=str(i + 101), close=str(i + 101))
    result = calculate(
        freeze_sma_example(tmp_path, calendar=calendar, rows=rows), SMASpec(period=3)
    )
    assert result.status == "AVAILABLE" and result.value == 102
    assert result.exact_sum.fraction() == 306
    assert len(result.contributors) == 3


@pytest.mark.parametrize(
    "period,offset,count,available",
    [
        (50, 0, 49, False),
        (50, 0, 50, True),
        (50, 10, 59, False),
        (50, 10, 60, True),
        (200, 0, 199, False),
        (200, 0, 200, True),
        (1, 0, 1, True),
    ],
)
def test_calendar_history_boundaries(tmp_path, period, offset, count, available):
    calendar, rows = sma_example_inputs(count)
    result = calculate(
        freeze_sma_example(tmp_path, calendar=calendar, rows=rows),
        SMASpec(period=period, session_offset=offset),
    )
    assert (result.status == "AVAILABLE") is available
    if not available:
        assert "INSUFFICIENT_HISTORY" in result.reasons
        assert result.value is None and result.exact_sum is None


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("missing", "MISSING_SESSION"),
        ("duplicate", "DUPLICATE_SESSION"),
        ("bad-close", "INVALID_OHLC"),
        ("bad-time", "INVALID_BAR_PROVENANCE"),
    ],
)
def test_required_window_failures(tmp_path, mutation, reason):
    calendar, rows = sma_example_inputs(201)
    if mutation == "missing":
        rows.pop(-20)
    elif mutation == "duplicate":
        rows.append(dict(rows[-20]))
    elif mutation == "bad-close":
        rows[-20]["close"] = "NaN"
    else:
        rows[-20]["timestamp"] = "2026-01-01T00:00:00+00:00"
    result = calculate(freeze_sma_example(tmp_path, calendar=calendar, rows=rows))
    assert result.status == "UNAVAILABLE" and reason in result.reasons


def test_missing_old_evidence_preserves_shorter_windows(tmp_path):
    calendar, rows = sma_example_inputs()
    rows[0].pop("close")
    result = trend(freeze_sma_example(tmp_path, calendar=calendar, rows=rows))
    assert result.comparisons() == {
        "close_above_sma50": True,
        "sma50_above_sma200": None,
        "sma50_rising": True,
    }
    assert result.confirmed is None
    assert result.observations[1].exact_sum is None


def test_volume_and_skipped_recent_closes_are_not_sma_dependencies(tmp_path):
    calendar, rows = sma_example_inputs()
    for row in rows:
        row.pop("volume")
    inputs = freeze_sma_example(tmp_path / "volume", calendar=calendar, rows=rows)
    assert trend(inputs).confirmed is True
    for row in rows[-10:]:
        row.pop("close")
    inputs = freeze_sma_example(tmp_path / "recent", calendar=calendar, rows=rows)
    assert calculate(inputs, SMASpec(period=50, session_offset=10)).value == Decimal("264.5")
    assert calculate(inputs).status == "UNAVAILABLE"


@pytest.mark.parametrize("delta,expected", [("0", False), ("1e-60", True), ("-1e-60", False)])
def test_exact_comparisons_survive_identical_display_values(tmp_path, delta, expected):
    calendar, rows = sma_example_inputs()
    for row in rows:
        row.update(open="100", low="99", high="101", close="100")
    with localcontext() as context:
        context.prec = 100
        rows[-1]["close"] = str(Decimal(100) + Decimal(delta))
    result = trend(freeze_sma_example(tmp_path, calendar=calendar, rows=rows))
    assert all(o.value == 100 for o in result.observations)
    assert all(value is expected for value in result.comparisons().values())
    assert result.confirmed is expected


def test_unavailable_cannot_become_false_or_pass(tmp_path):
    inputs = freeze_sma_example(tmp_path, flags=("PUBLICATION_UNVERIFIED",))
    result = trend(inputs)
    assert result.confirmed is None
    assert all(value is None for value in result.comparisons().values())
    assert all("PUBLICATION_UNVERIFIED" in o.reasons for o in result.observations)


@pytest.mark.parametrize(
    "case,reason",
    [
        ("raw", "INCOMPATIBLE_PRICE_BASIS"),
        ("unverified", "PUBLICATION_UNVERIFIED"),
        ("hash", "INCOMPATIBLE_PROVENANCE"),
        ("preclose", "SESSION_INCOMPLETE"),
        ("preavailable", "INPUT_NOT_YET_AVAILABLE"),
        ("weekend", "UNSUPPORTED_CALENDAR"),
    ],
)
def test_basis_availability_and_explicit_assessment(tmp_path, case, reason):
    inputs = list(freeze_sma_example(tmp_path, adjustment="raw" if case == "raw" else "split"))
    kwargs = {}
    if case == "unverified":
        inputs[1] = inputs[1].model_copy(update={"publications": ()})
    elif case == "hash":
        inputs[1] = inputs[1].model_copy(update={"snapshot_sha256": "0" * 64})
    elif case == "preclose":
        kwargs["as_of"] = inputs[-1] - timedelta(hours=2)
    elif case == "preavailable":
        kwargs["as_of"] = inputs[-1] - timedelta(minutes=30)
    elif case == "weekend":
        kwargs["day"] = date(2026, 10, 31)
    result = calculate(inputs, **kwargs)
    assert result.value is None and reason in result.reasons


def test_tampered_snapshot_stops_calculation(tmp_path):
    inputs = freeze_sma_example(tmp_path)
    from pathlib import Path

    Path(inputs[0].reference).chmod(0o600)
    Path(inputs[0].reference).write_text("{}")
    with pytest.raises(ValueError):
        calculate(inputs)


def test_new_sma_and_existing_rvol_ignore_decimal_traps_and_limits(tmp_path):
    inputs = freeze_sma_example(tmp_path / "sma")
    rvol_inputs = freeze_example(tmp_path / "rvol")
    rvol_spec = ObservationSpec(
        feature_id="relative_volume",
        price_lookback=5,
        volume_lookback=1,
        regular_session_seconds=21600,
    )
    expected = calculate(inputs, SMASpec(period=3)).semantic_bytes()
    expected_rvol = calculate(rvol_inputs, rvol_spec).semantic_bytes()
    with localcontext() as ambient:
        ambient.prec = 2
        ambient.rounding = ROUND_UP
        ambient.Emax = 1
        ambient.Emin = -1
        ambient.traps[Inexact] = True
        ambient.traps[Rounded] = True
        assert calculate(inputs, SMASpec(period=3)).semantic_bytes() == expected
        assert calculate(rvol_inputs, rvol_spec).semantic_bytes() == expected_rvol
        assert (
            calculate(
                rvol_inputs,
                ObservationSpec(
                    feature_id="close_distance",
                    price_lookback=5,
                    volume_lookback=5,
                    regular_session_seconds=21600,
                ),
            ).value
            == 1
        )


def test_sma_nonterminating_mean_retains_exact_sum(tmp_path):
    calendar, rows = sma_example_inputs(3)
    for row in rows:
        row.update(open="1", high="3", low="1", close="1")
    rows[-1]["close"] = "2"
    inputs = freeze_sma_example(tmp_path, calendar=calendar, rows=rows)
    expected = calculate(inputs, SMASpec(period=3))
    assert expected.exact_sum.fraction() / 3 == Fraction(4, 3)
    assert str(expected.value) == "1." + "3" * 49
    with localcontext() as context:
        context.traps[Inexact] = True
        assert calculate(inputs, SMASpec(period=3)).semantic_bytes() == expected.semantic_bytes()


def test_future_append_correction_and_reordered_input(tmp_path):
    calendar, rows = sma_example_inputs()
    inputs = freeze_sma_example(tmp_path / "original", calendar=calendar, rows=rows)
    result = calculate(inputs)
    replay = calculate(inputs, calculated_at=inputs[-1] + timedelta(seconds=5))
    assert result.semantic_bytes() == replay.semantic_bytes()
    rows.append(
        dict(
            rows[-1],
            session="2026-11-02",
            timestamp="2026-11-02T20:00:00+00:00",
            close="99999",
            high="100000",
        )
    )
    appended = calculate(freeze_sma_example(tmp_path / "future", calendar=calendar, rows=rows))
    assert (appended.value, appended.exact_sum, appended.reasons) == (
        result.value,
        result.exact_sum,
        result.reasons,
    )
    assert appended.snapshot_sha256 != result.snapshot_sha256
    reordered = calculate(
        freeze_sma_example(tmp_path / "reordered", calendar=calendar, rows=list(reversed(rows)))
    )
    assert reordered.exact_sum is None
    assert "UNORDERED_SESSIONS" in reordered.reasons
    rows[-2]["close"] = "300"
    corrected = calculate(freeze_sma_example(tmp_path / "correction", calendar=calendar, rows=rows))
    assert corrected.exact_sum != result.exact_sum
    assert calculate(inputs).semantic_bytes() == result.semantic_bytes()


def test_trend_rejects_mixed_evidence(tmp_path):
    a = trend(freeze_sma_example(tmp_path / "a"))
    b = trend(freeze_sma_example(tmp_path / "b"))
    with pytest.raises(ValidationError, match="Incompatible"):
        DailyTrendObservation(
            observations=(a.observations[0], b.observations[1], a.observations[2])
        )


def test_offset_counts_exchange_sessions_across_holiday():
    from stock_analyser.calendars import CutoffPolicy, HistoryResolver

    spec = SMASpec(period=3, session_offset=1)
    resolved = HistoryResolver(
        ExchangeCalendarAdapter(), CutoffPolicy(publication_timing="synthetic")
    ).resolve(
        US_MARKET,
        "XNYS",
        spec.history_requirement,
        datetime(2026, 11, 30, 22, tzinfo=UTC),
    )
    labels = [p.session_end for p in resolved.periods]
    assert labels == [
        date(2026, 11, 24),
        date(2026, 11, 25),
        date(2026, 11, 27),
        date(2026, 11, 30),
    ]


def test_fresh_process_walkthrough(tmp_path):
    command = [sys.executable, "-m", "stock_analyser.sma_example", "--output", str(tmp_path)]
    first = subprocess.run(
        command, capture_output=True, text=True, check=True, env=os.environ.copy()
    )
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*-observations.json")}
    second = subprocess.run(
        command, capture_output=True, text=True, check=True, env=os.environ.copy()
    )
    assert first.stdout == second.stdout
    assert before == {p.name: p.read_bytes() for p in tmp_path.glob("*-observations.json")}
    rising = json.loads(before["rising-observations.json"])
    assert rising["confirmed"] is True
    assert json.loads(before["flat-observations.json"])["confirmed"] is False
    assert json.loads(before["missing-old-close-observations.json"])["confirmed"] is None
