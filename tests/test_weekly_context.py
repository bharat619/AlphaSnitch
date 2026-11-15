import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_UP, Decimal, Inexact, Rounded, localcontext
from fractions import Fraction
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_analyser.calendars import CutoffPolicy, ExchangeCalendarAdapter
from stock_analyser.fixtures import FixtureSessionCalendar
from stock_analyser.indicators import SMASpec, observe
from stock_analyser.markets import US_MARKET
from stock_analyser.weekly_context import (
    WeeklyContextObservation,
    WeeklyContextSpec,
    observe_weekly_context,
)
from stock_analyser.weekly_example import freeze_weekly_example, weekly_example_inputs

POLICY = CutoffPolicy(publication_timing="synthetic")


def calculate(inputs, **kwargs):
    snapshot, provenance, calendar, stamp = inputs
    return observe_weekly_context(
        snapshot,
        provenance,
        calendar,
        as_of=kwargs.pop("as_of", stamp),
        calculated_at=kwargs.pop("calculated_at", stamp),
        cutoff_policy=kwargs.pop("cutoff_policy", POLICY),
        **kwargs,
    )


@pytest.mark.parametrize(
    "closes,label,current,previous",
    [
        (list(range(100, 134)), "SUPPORTS", "118.5", "114.5"),
        (list(range(133, 99, -1)), "OPPOSES", "114.5", "118.5"),
        ([100] * 34, "MIXED", "100", "100"),
        ([*range(100, 133), 110], "MIXED", None, "114.5"),
    ],
)
def test_agreed_examples_and_roundtrip(tmp_path, closes, label, current, previous):
    inputs = freeze_weekly_example(tmp_path, closes=closes)
    result = calculate(inputs)
    assert result.classification == label
    assert current is None or result.sma30 == Decimal(current)
    assert result.sma30_four_weeks_earlier == Decimal(previous)
    assert result.current_sum.fraction() == sum(Fraction(c) for c in closes[-30:])
    assert result.previous_sum.fraction() == sum(Fraction(c) for c in closes[:30])
    assert len(result.weeks) == 34
    assert all(len(w.sessions) == 5 for w in result.weeks)
    assert result.available_at == inputs[-1]
    assert result.daily_session == result.resolution.session_date == date(2026, 10, 30)
    assert WeeklyContextObservation.model_validate_json(result.model_dump_json()) == result


def test_weekly_history_is_separate_from_daily_universe():
    from stock_analyser.universe import SelectionPolicy

    requirement = WeeklyContextSpec().history_requirement
    assert (requirement.timeframe, requirement.required_bars) == ("weekly", 34)
    assert requirement.warmup_bars == 0
    assert SelectionPolicy().minimum_daily_bars == 260
    assert WeeklyContextSpec().role == "informational"


@pytest.mark.parametrize("count,available", [(33, False), (34, True), (35, True)])
def test_required_week_count(tmp_path, count, available):
    result = calculate(freeze_weekly_example(tmp_path, closes=list(range(100, 100 + count))))
    assert (result.classification != "UNAVAILABLE") is available
    if available:
        assert len(result.weeks) == 34
    else:
        assert "INSUFFICIENT_HISTORY" in result.reasons
        assert result.current_sum is None and not result.weeks


@pytest.mark.parametrize(
    "mutation,reason",
    [
        ("midweek", "MISSING_SESSION"),
        ("last", "MISSING_SESSION"),
        ("bad-close", "INVALID_OHLC"),
        ("duplicate", "DUPLICATE_SESSION"),
        ("timestamp", "INVALID_BAR_PROVENANCE"),
        ("volume", None),
    ],
)
def test_complete_constituents_required_volume_optional(tmp_path, mutation, reason):
    calendar, rows = weekly_example_inputs()
    if mutation == "midweek":
        rows.pop(-3)
    elif mutation == "last":
        rows.pop()
    elif mutation == "bad-close":
        rows[-3]["close"] = "NaN"
    elif mutation == "duplicate":
        rows.insert(-3, dict(rows[-3]))
    elif mutation == "timestamp":
        rows[-3]["timestamp"] = "2026-10-28T19:00:00+00:00"
    else:
        for row in rows:
            row.pop("volume")
    result = calculate(freeze_weekly_example(tmp_path, calendar=calendar, rows=rows))
    if reason:
        assert result.classification == "UNAVAILABLE" and reason in result.reasons
        assert result.resolution.session_date == date(2026, 10, 30)  # No older fallback.
        assert not result.weeks and result.sma30 is None
    else:
        assert result.classification == "SUPPORTS"


@pytest.mark.parametrize(
    "case,reason",
    [
        ("policy", "PUBLICATION_UNVERIFIED"),
        ("publication", "PUBLICATION_UNVERIFIED"),
        ("flag", "PUBLICATION_UNVERIFIED"),
        ("raw", "INCOMPATIBLE_PRICE_BASIS"),
        ("late", "INPUT_NOT_YET_AVAILABLE"),
        ("provenance", "INCOMPATIBLE_PROVENANCE"),
    ],
)
def test_publication_basis_and_lineage_blockers(tmp_path, case, reason):
    inputs = list(
        freeze_weekly_example(
            tmp_path,
            adjustment="raw" if case == "raw" else "split",
            flags=("PUBLICATION_UNVERIFIED",) if case == "flag" else (),
        )
    )
    kwargs = {}
    if case == "policy":
        kwargs["cutoff_policy"] = None  # Default policy never asserts publication.
    elif case == "publication":
        inputs[1] = inputs[1].model_copy(update={"publications": inputs[1].publications[1:]})
    elif case == "late":
        kwargs["as_of"] = inputs[-1] - timedelta(minutes=30)
    elif case == "provenance":
        inputs[1] = inputs[1].model_copy(update={"snapshot_sha256": "0" * 64})
    result = calculate(inputs, **kwargs)
    assert result.classification == "UNAVAILABLE" and reason in result.reasons


def test_midweek_excludes_incomplete_week_and_keeps_daily_date(tmp_path):
    calendar, rows = weekly_example_inputs(list(range(100, 135)))
    stamp = datetime(2026, 10, 28, 21, tzinfo=UTC)
    rows = [r for r in rows if r["session"] <= stamp.date().isoformat()]
    for row in rows[-3:]:
        row.pop("close")  # These incomplete-week prices are not dependencies.
    inputs = freeze_weekly_example(tmp_path, calendar=calendar, rows=rows, as_of=stamp)
    result = calculate(inputs)
    assert result.classification == "SUPPORTS"
    assert result.daily_session == date(2026, 10, 28)
    assert result.resolution.session_date == date(2026, 10, 23)
    assert result.weeks[-1].close == 133
    assert all(d <= date(2026, 10, 23) for d in result.constituent_evidence.window_sessions)


def test_calendar_grace_boundary_requires_latest_evidence(tmp_path):
    calendar, rows = weekly_example_inputs(list(range(100, 135)))
    before = datetime(2026, 10, 30, 20, 14, 59, tzinfo=UTC)
    inputs = freeze_weekly_example(tmp_path, calendar=calendar, rows=rows[:-1], as_of=before)
    earlier = calculate(inputs)
    assert earlier.classification == "SUPPORTS"
    assert earlier.resolution.session_date == date(2026, 10, 23)
    cutoff = before + timedelta(seconds=1)
    latest = calculate(inputs, as_of=cutoff, calculated_at=cutoff)
    assert latest.classification == "UNAVAILABLE"
    assert latest.resolution.session_date == date(2026, 10, 30)
    assert "MISSING_SESSION" in latest.reasons


@pytest.mark.parametrize(
    "delta,expected", [("0", "MIXED"), ("1e-60", "SUPPORTS"), ("-1e-60", "OPPOSES")]
)
def test_unrounded_comparisons(tmp_path, delta, expected):
    with localcontext() as context:
        context.prec = 100
        closes = [Decimal(100)] * 33 + [Decimal(100) + Decimal(delta)]
    result = calculate(freeze_weekly_example(tmp_path, closes=closes))
    assert result.sma30 == result.sma30_four_weeks_earlier == 100
    assert result.classification == expected


def test_four_week_slope_is_net_change_not_four_consecutive_rises(tmp_path):
    closes = [100] * 30 + [140, 140, 140, 101]
    result = calculate(freeze_weekly_example(tmp_path, closes=closes))
    assert result.current_sum.fraction() > result.previous_sum.fraction()
    assert result.weeks[-1].close < result.sma30
    assert result.classification == "MIXED"


def test_weekly_close_mean_is_not_daily_mean(tmp_path):
    calendar, rows = weekly_example_inputs()
    for row in rows:
        row.update(open="90", low="90", high=row["close"])
        if date.fromisoformat(row["session"]).weekday() != 4:
            row["close"] = "90"
    inputs = freeze_weekly_example(tmp_path, calendar=calendar, rows=rows)
    result = calculate(inputs)
    assert result.sma30 == Decimal("118.5")
    daily = observe(
        inputs[0],
        SMASpec(period=150),
        inputs[1],
        calendar,
        inputs[-1].date(),
        as_of=inputs[-1],
        calculated_at=inputs[-1],
    )
    assert daily.value != result.sma30


def test_holiday_and_early_close_constituent_aggregation(tmp_path):
    # Use maintained XNYS dates/times, mapped into the synthetic evidence market.
    real = ExchangeCalendarAdapter().schedule(
        US_MARKET, "XNYS", date(2026, 3, 30), date(2026, 11, 27)
    )
    fixture_calendar, _ = weekly_example_inputs()
    calendar = FixtureSessionCalendar(fixture_calendar.market, real)
    rows = [
        dict(
            session=s.session.isoformat(),
            timestamp=s.closes_at.isoformat(),
            open="100",
            high="100",
            low="100",
            close="100",
            volume="1",
        )
        for s in real
    ]
    stamp = datetime(2026, 11, 27, 18, 15, tzinfo=UTC)
    inputs = freeze_weekly_example(tmp_path, calendar=calendar, rows=rows, as_of=stamp)
    result = calculate(inputs)
    assert result.classification == "MIXED"
    assert result.weeks[-1].sessions == tuple(date(2026, 11, d) for d in (23, 24, 25, 27))
    assert result.weeks[-1].closes_at.hour == 18
    # The preceding 34-week window includes Good Friday's Thursday close.
    earlier = datetime(2026, 11, 20, 22, tzinfo=UTC)
    earlier_inputs = freeze_weekly_example(
        tmp_path / "thursday",
        calendar=calendar,
        rows=[r for r in rows if r["session"] <= earlier.date().isoformat()],
        as_of=earlier,
    )
    thursday = next(w for w in calculate(earlier_inputs).weeks if w.session_end == date(2026, 4, 2))
    assert len(thursday.sessions) == 4


def test_ambient_decimal_context_and_replay(tmp_path):
    inputs = freeze_weekly_example(tmp_path, closes=[*range(100, 133), 110])
    result = calculate(inputs)
    with localcontext() as context:
        context.prec = 2
        context.rounding = ROUND_UP
        context.Emax, context.Emin = 1, -1
        context.traps[Inexact] = context.traps[Rounded] = True
        assert calculate(inputs).semantic_bytes() == result.semantic_bytes()
    replay = calculate(inputs, calculated_at=inputs[-1] + timedelta(days=1))
    assert replay.semantic_bytes() == result.semantic_bytes()


def test_future_append_correction_and_original_replay(tmp_path):
    calendar, rows = weekly_example_inputs()
    inputs = freeze_weekly_example(tmp_path / "original", calendar=calendar, rows=rows)
    original = calculate(inputs)
    rows.append(
        dict(rows[-1], session="2026-11-02", timestamp="2026-11-02T20:00:00+00:00", close="NaN")
    )
    appended = calculate(freeze_weekly_example(tmp_path / "future", calendar=calendar, rows=rows))
    assert appended.weeks == original.weeks
    assert appended.current_sum == original.current_sum
    rows[-2].update(close="132", open="132", high="132", low="132")
    corrected = calculate(
        freeze_weekly_example(tmp_path / "correction", calendar=calendar, rows=rows)
    )
    assert corrected.current_sum != original.current_sum
    assert calculate(inputs).semantic_bytes() == original.semantic_bytes()


def test_integrity_failure_and_unsupported_calendar(tmp_path):
    inputs = freeze_weekly_example(tmp_path / "valid")

    class Unsupported:
        def data_version(self, market, venue):
            raise ValueError("unsupported")

    result = calculate((inputs[0], inputs[1], Unsupported(), inputs[-1]))
    assert result.classification == "UNAVAILABLE"
    assert "UNSUPPORTED_CALENDAR" in result.reasons
    Path(inputs[0].reference).chmod(0o600)
    Path(inputs[0].reference).write_text("{}")
    with pytest.raises(ValueError):
        calculate(inputs)


def test_schema_rejects_wrong_aggregate_and_classification(tmp_path):
    result = calculate(freeze_weekly_example(tmp_path))
    for field, value in (
        ("classification", "MIXED"),
        ("sma30", "1"),
        ("specification_sha256", "0" * 64),
    ):
        payload = result.model_dump(mode="json")
        payload[field] = value
        with pytest.raises(ValidationError):
            WeeklyContextObservation.model_validate(payload)


def test_fresh_process_example(tmp_path):
    command = [sys.executable, "-m", "stock_analyser.weekly_example", "--output", str(tmp_path)]
    first = subprocess.run(
        command, capture_output=True, text=True, check=True, env=os.environ.copy()
    )
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*-observations.json")}
    second = subprocess.run(
        command, capture_output=True, text=True, check=True, env=os.environ.copy()
    )
    assert first.stdout == second.stdout
    assert before == {p.name: p.read_bytes() for p in tmp_path.glob("*-observations.json")}
    assert json.loads(before["supports-observations.json"])["classification"] == "SUPPORTS"
    assert (
        json.loads(before["missing-constituent-observations.json"])["classification"]
        == "UNAVAILABLE"
    )


def test_missing_weekly_history_does_not_change_daily_observation(tmp_path):
    calendar, rows = weekly_example_inputs()
    rows.pop(0)
    snapshot, provenance, calendar, stamp = freeze_weekly_example(
        tmp_path,
        calendar=calendar,
        rows=rows,
    )
    weekly = observe_weekly_context(
        snapshot,
        provenance,
        calendar,
        as_of=stamp,
        calculated_at=stamp,
        cutoff_policy=POLICY,
    )
    daily = observe(
        snapshot,
        SMASpec(period=50),
        provenance,
        calendar,
        stamp.date(),
        as_of=stamp,
        calculated_at=stamp,
    )
    assert weekly.classification == "UNAVAILABLE"
    assert daily.status == "AVAILABLE"


def test_first_proven_time_includes_every_daily_constituent(tmp_path):
    inputs = list(freeze_weekly_example(tmp_path))
    cutoff = inputs[-1] + timedelta(minutes=5)
    publications = list(inputs[1].publications)
    publications[0] = publications[0].model_copy(update={"verified_at": cutoff})
    inputs[1] = inputs[1].model_copy(update={"publications": tuple(publications)})
    before = calculate(inputs)
    assert before.classification == "UNAVAILABLE"
    assert "INPUT_NOT_YET_AVAILABLE" in before.reasons
    after = calculate(inputs, as_of=cutoff, calculated_at=cutoff)
    assert after.classification == "SUPPORTS"
    assert after.available_at == after.weeks[0].available_at == cutoff
    assert after.weeks[-1].available_at < cutoff
