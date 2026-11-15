import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, localcontext
from pathlib import Path

import pytest

from stock_analyser.calendars import ExchangeCalendarAdapter
from stock_analyser.fixtures import FixtureSessionCalendar
from stock_analyser.indicator_example import FEATURES, example_inputs, freeze_example
from stock_analyser.indicators import InputProvenance, ObservationSpec, PublicationEvidence, observe
from stock_analyser.markets import US_MARKET


def calculate(inputs, feature="relative_volume", price=5, volume=5, as_of=None, day=None):
    reference, provenance, calendar, stamp = inputs
    return observe(
        reference,
        ObservationSpec(
            feature_id=feature,
            price_lookback=price,
            volume_lookback=volume,
            regular_session_seconds=21600,
        ),
        provenance,
        calendar,
        day or stamp.date(),
        as_of=as_of or stamp,
        calculated_at=stamp,
    )


def changed(tmp_path, change, **kwargs):
    _, _, _, rows = example_inputs()
    change(rows)
    return freeze_example(tmp_path, rows=rows, **kwargs)


@pytest.mark.parametrize(
    "feature,expected", list(zip(FEATURES, [101, 1000000, Decimal("1.6"), 1, True], strict=True))
)
def test_hand_computed_golden_values(tmp_path, feature, expected):
    result = calculate(freeze_example(tmp_path), feature)
    assert result.status == "AVAILABLE", result.reasons
    assert result.value == expected
    assert result.prior_sessions == tuple(date(2026, 10, d) for d in (23, 26, 27, 28, 29))
    assert date(2026, 10, 30) not in result.prior_sessions


@pytest.mark.parametrize(
    "n,high,mean,rvol",
    [
        (1, 100, 1100000, Decimal("1.4545454545454545454545454545454545454545454545455")),
        (5, 101, 1000000, Decimal("1.6")),
        (20, 101, 775000, Decimal("2.0645161290322580645161290322580645161290322580645")),
    ],
)
def test_explicit_lookbacks(tmp_path, n, high, mean, rvol):
    inputs = freeze_example(tmp_path)
    assert calculate(inputs, "prior_high", price=n).value == high
    assert calculate(inputs, "prior_mean_volume", volume=n).value == mean
    assert calculate(inputs, volume=n).value == rvol


def test_independent_windows_and_missing_outside_short_window(tmp_path):
    inputs = changed(tmp_path, lambda rows: rows[-5].pop("volume"))
    assert calculate(inputs, "prior_high", price=5, volume=1).value == 101
    assert calculate(inputs, volume=1).status == "AVAILABLE"
    assert "MISSING_VOLUME" in calculate(inputs, volume=5).reasons
    assert calculate(inputs, "prior_high", price=1, volume=5).value == 100


@pytest.mark.parametrize("close,expected", [("101", False), ("100", False), ("102", True)])
def test_strict_close_comparison_despite_higher_intraday_high(tmp_path, close, expected):
    inputs = changed(tmp_path, lambda rows: rows[-1].update(close=close))
    assert calculate(inputs, "close_above_prior_high").value is expected


@pytest.mark.parametrize("index", [-1, -3])
def test_missing_volume_preserves_price(tmp_path, index):
    inputs = changed(tmp_path, lambda rows: rows[index].pop("volume"))
    assert calculate(inputs, "prior_high").value == 101
    assert calculate(inputs, "close_distance").value == 1
    assert "MISSING_VOLUME" in calculate(inputs).reasons


@pytest.mark.parametrize("value", ["-1", "NaN", "Infinity", "-Infinity", "bad"])
def test_invalid_volume(tmp_path, value):
    inputs = changed(tmp_path, lambda rows: rows[-1].update(volume=value))
    assert "INVALID_VOLUME" in calculate(inputs).reasons
    assert calculate(inputs, "close_distance").value == 1


@pytest.mark.parametrize(
    "field,value",
    [("close", "NaN"), ("high", "Infinity"), ("low", "-1"), ("high", "90"), ("close", "bad")],
)
def test_invalid_prices(tmp_path, field, value):
    inputs = changed(tmp_path, lambda rows: rows[-1].update({field: value}))
    assert "INVALID_OHLC" in calculate(inputs, "close_distance").reasons
    assert calculate(inputs).value == Decimal("1.6")


def test_zero_and_fractional_volume(tmp_path):
    inputs = changed(tmp_path / "zero", lambda rows: rows[-1].update(volume="0"))
    assert calculate(inputs).value == 0
    inputs = changed(tmp_path / "baseline", lambda rows: [r.update(volume="0") for r in rows[:-1]])
    assert calculate(inputs).value is None
    assert "ZERO_DENOMINATOR" in calculate(inputs).reasons
    assert calculate(inputs, "prior_mean_volume").value == 0
    inputs = changed(tmp_path / "fraction", lambda rows: [r.update(volume="0.25") for r in rows])
    assert calculate(inputs, "prior_mean_volume").value == Decimal("0.25")
    assert calculate(inputs).value == 1


def test_decimal_results_ignore_global_precision(tmp_path):
    inputs = changed(
        tmp_path, lambda rows: [r.update(volume="0.12345678901234567890123456789") for r in rows]
    )
    with localcontext() as context:
        context.prec = 4
        assert calculate(inputs, "prior_mean_volume").value == Decimal(
            "0.12345678901234567890123456789"
        )
        assert calculate(inputs).value == 1


@pytest.mark.parametrize("count,available", [(6, True), (5, False)])
def test_exactly_sufficient_and_one_short(tmp_path, count, available):
    inputs = changed(tmp_path, lambda rows: rows.__delitem__(slice(None, -count)))
    assert (calculate(inputs).status == "AVAILABLE") is available
    if not available:
        assert "MISSING_SESSION" in calculate(inputs).reasons


@pytest.mark.parametrize(
    "operation,reason",
    [
        (lambda rows: rows.pop(-3), "MISSING_SESSION"),
        (lambda rows: rows.append(dict(rows[-2])), "DUPLICATE_SESSION"),
    ],
)
def test_exact_expected_sessions_no_older_substitution(tmp_path, operation, reason):
    inputs = changed(tmp_path, operation)
    assert reason in calculate(inputs).reasons
    assert calculate(inputs).value is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("instrument_id", "other"),
        ("feed_id", "other"),
        ("provider", "other"),
        ("adjustment", "raw"),
    ],
)
def test_mixed_row_identity(tmp_path, field, value):
    inputs = changed(tmp_path, lambda rows: rows[-2].update({field: value}))
    assert "INVALID_BAR_PROVENANCE" in calculate(inputs).reasons
    assert "INVALID_BAR_PROVENANCE" in calculate(inputs, "prior_high").reasons


def test_raw_price_basis_and_unverified_volume(tmp_path):
    inputs = freeze_example(tmp_path, adjustment="raw")
    assert "INCOMPATIBLE_PRICE_BASIS" in calculate(inputs, "prior_high").reasons
    inputs = list(freeze_example(tmp_path / "split"))
    inputs[1] = inputs[1].model_copy(update={"volume_adjustment": "unverified"})
    assert "UNVERIFIED_VOLUME_BASIS" in calculate(inputs).reasons
    assert calculate(inputs, "prior_high").value == 101


@pytest.mark.parametrize("index", [-1, -2])
def test_shortened_session_blocks_only_volume(tmp_path, index):
    _, _, calendar, rows = example_inputs()
    sessions = list(calendar.sessions)
    sessions[index] = sessions[index].model_copy(
        update={
            "opens_at": sessions[index].opens_at + timedelta(hours=2),
        }
    )
    calendar = FixtureSessionCalendar(calendar.market, tuple(sessions))
    inputs = freeze_example(tmp_path, calendar=calendar, rows=rows)
    assert "SHORTENED_OR_INCOMPATIBLE_SESSION" in calculate(inputs).reasons
    assert calculate(inputs, "close_distance").value == 1


def test_completion_retrieval_and_claimed_publication_are_distinct(tmp_path):
    inputs = list(freeze_example(tmp_path))
    earlier = inputs[-1] - timedelta(hours=2)
    result = calculate(inputs, as_of=earlier)
    assert "SESSION_INCOMPLETE" in result.reasons
    result = calculate(inputs, as_of=inputs[-1] - timedelta(minutes=30))
    assert "INPUT_NOT_YET_AVAILABLE" in result.reasons
    assert result.available_at == inputs[-1]
    inputs[1] = InputProvenance(
        snapshot_sha256=inputs[0].sha256,
        volume_adjustment="split_consistent",
        volume_verification_reference="synthetic://volume",
        publications=tuple(
            PublicationEvidence(session=p.session, claimed_at=p.verified_at)
            for p in inputs[1].publications
        ),
    )
    result = calculate(inputs)
    assert result.historical_availability == "unknown"
    assert result.available_at == inputs[-1]
    assert "PUBLICATION_UNVERIFIED" in result.reasons
    assert result.value is None


def test_packet_publication_blocker_cannot_be_cleared_by_sidecar(tmp_path):
    inputs = freeze_example(tmp_path, flags=("PUBLICATION_UNVERIFIED",))
    assert "PUBLICATION_UNVERIFIED" in calculate(inputs, "prior_high").reasons
    assert calculate(inputs).historical_availability == "unknown"


def test_unsupported_calendar(tmp_path):
    inputs = list(freeze_example(tmp_path))
    inputs[2] = ExchangeCalendarAdapter()
    assert "UNSUPPORTED_CALENDAR" in calculate(inputs).reasons


def test_real_us_holiday_and_early_close():
    sessions = ExchangeCalendarAdapter().schedule(
        US_MARKET, "XNYS", date(2026, 11, 23), date(2026, 11, 30)
    )
    assert date(2026, 11, 26) not in {s.session for s in sessions}
    friday = next(s for s in sessions if s.session == date(2026, 11, 27))
    assert friday.closes_at - friday.opens_at == timedelta(hours=3, minutes=30)


@pytest.mark.parametrize("target", ["raw", "manifest"])
def test_integrity_failure_never_salvages(tmp_path, target):
    inputs = freeze_example(tmp_path)
    snapshot = inputs[0]
    path = Path(
        snapshot.reference if target == "manifest" else snapshot.metadata.sources[0].reference
    )
    path.chmod(0o644)
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ValueError, match="hash mismatch"):
        calculate(inputs, "prior_high")


def test_future_extremes_and_versioned_correction(tmp_path):
    inputs = freeze_example(tmp_path / "old")
    original = calculate(inputs)
    _, _, _, rows = example_inputs()
    future = dict(
        rows[-1],
        session="2026-11-02",
        timestamp="2026-11-02T20:00:00+00:00",
        high="99999",
        volume="9999999999999",
    )
    appended = freeze_example(tmp_path / "appended", rows=[*rows, future])
    assert calculate(appended).value == original.value
    assert calculate(appended, "prior_high").value == 101
    corrected = changed(tmp_path / "corrected", lambda r: r[-2].update(volume="2100000"))
    assert calculate(corrected).value != original.value
    assert corrected[0].sha256 != inputs[0].sha256
    assert calculate(inputs).semantic_bytes() == original.semantic_bytes()


def test_calendar_history_shortfall(tmp_path):
    _, _, calendar, _ = example_inputs()
    calendar = FixtureSessionCalendar(calendar.market, calendar.sessions[-5:])
    result = calculate(freeze_example(tmp_path, calendar=calendar))
    assert "INSUFFICIENT_HISTORY" in result.reasons


def test_fresh_process_example_is_deterministic(tmp_path):
    command = [sys.executable, "-m", "stock_analyser.indicator_example", "--output", str(tmp_path)]
    first = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    files = {p.name: p.read_bytes() for p in tmp_path.glob("*-observations.json")}
    second = subprocess.run(command, capture_output=True, text=True, check=True).stdout
    assert first == second
    assert files == {p.name: p.read_bytes() for p in tmp_path.glob("*-observations.json")}
    assert json.loads(files["equal-observations.json"])[-1]["value"] is False
    assert json.loads(files["missing-volume-observations.json"])[2]["reasons"] == ["MISSING_VOLUME"]


def test_numeric_json_fractional_volume(tmp_path):
    inputs = changed(tmp_path, lambda rows: [r.update(volume=0.125) for r in rows])
    assert calculate(inputs, "prior_mean_volume").value == Decimal("0.125")
    assert calculate(inputs).value == 1


def test_holiday_calendar_window_is_sessions_not_days(tmp_path):
    _, _, calendar, rows = example_inputs()
    holiday = date(2026, 10, 26)  # Explicit synthetic closure, not a live US holiday claim.
    calendar = FixtureSessionCalendar(
        calendar.market, tuple(s for s in calendar.sessions if s.session != holiday)
    )
    rows = [r for r in rows if r["session"] != holiday.isoformat()]
    inputs = freeze_example(tmp_path, calendar=calendar, rows=rows)
    result = calculate(inputs, "prior_mean_volume")
    assert result.value == 940000
    assert holiday not in result.prior_sessions
    assert result.prior_sessions[0] == date(2026, 10, 22)


def test_whole_packet_failure_blocks_both_fields(tmp_path):
    inputs = freeze_example(tmp_path, flags=("UNSUPPORTED_FEED",))
    for feature in FEATURES:
        result = calculate(inputs, feature)
        assert result.value is None
        assert "UNSUPPORTED_FEED" in result.reasons


def test_invalid_timestamp_blocks_both_fields(tmp_path):
    inputs = changed(tmp_path, lambda rows: rows[-2].update(timestamp="2026-10-29T19:00:00Z"))
    assert "INVALID_BAR_PROVENANCE" in calculate(inputs).reasons
    assert "INVALID_BAR_PROVENANCE" in calculate(inputs, "prior_high").reasons


def test_unverified_and_preclose_publication(tmp_path):
    inputs = list(freeze_example(tmp_path))
    publications = list(inputs[1].publications)
    publications[-1] = publications[-1].model_copy(
        update={"verified_at": datetime(2026, 10, 30, 19, tzinfo=UTC)}
    )
    inputs[1] = inputs[1].model_copy(update={"publications": tuple(publications)})
    assert "INVALID_PUBLICATION_TIME" in calculate(inputs).reasons
    inputs[1] = inputs[1].model_copy(update={"publications": ()})
    assert calculate(inputs).historical_availability == "unknown"
    assert "PUBLICATION_UNVERIFIED" in calculate(inputs).reasons


def test_unknown_assessment_session_is_unavailable(tmp_path):
    result = calculate(freeze_example(tmp_path), day=date(2026, 10, 31))
    assert "UNSUPPORTED_CALENDAR" in result.reasons


def test_baseline_excludes_invalid_current_fields(tmp_path):
    inputs = changed(tmp_path, lambda rows: rows[-1].update(volume="NaN", close="NaN"))
    assert calculate(inputs, "prior_high").value == 101
    assert calculate(inputs, "prior_mean_volume").value == 1000000
    assert calculate(inputs).value is None
    assert calculate(inputs, "close_distance").value is None


def test_incompatible_row_volume_basis_preserves_prices(tmp_path):
    inputs = changed(tmp_path, lambda rows: rows[-2].update(volume_adjustment="raw"))
    assert "INCOMPATIBLE_VOLUME_BASIS" in calculate(inputs).reasons
    assert calculate(inputs, "prior_high").value == 101
