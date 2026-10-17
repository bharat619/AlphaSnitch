"""Raw corruption must remain inspectable and must block mandatory evidence."""

import hashlib
import json
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from stock_analyser.calendars import CutoffPolicy, HistoryResolver
from stock_analyser.collection import encode, immutable_write, validate_prices
from stock_analyser.contracts import HistoryRequirement
from stock_analyser.fixtures import (
    RawFixtureProvider,
    synthetic_markets,
    synthetic_session_calendars,
)


@pytest.fixture
def evidence():
    markets, instruments, _ = synthetic_markets()
    market = markets.resolve("US")
    instrument = instruments.resolve("US", "ACME")
    _, calendars = synthetic_session_calendars()
    resolution = HistoryResolver(
        calendars[0],
        CutoffPolicy(earliest_session=date(2026, 10, 5), publication_timing="synthetic"),
    ).resolve(
        market,
        instrument.venue,
        HistoryRequirement(timeframe="daily", minimum_bars=3),
        datetime(2026, 10, 16, 21, tzinfo=UTC),
    )
    return RawFixtureProvider().fetch(instrument, market, resolution), resolution


def codes(findings):
    return {f.code for f in findings}


def test_valid_fractional_volume_and_zero_volume_policy(evidence):
    response, resolution = evidence
    bars, findings, count = validate_prices(response, resolution)
    assert count == 3
    assert not findings
    assert bars[0].volume == Decimal("1.5")
    payload = json.loads(response.payload)
    payload["bars"][0]["volume"] = "0"
    bars, findings, _ = validate_prices(
        response.model_copy(update={"payload": encode(payload)}), resolution
    )
    assert bars[0].volume == 0
    assert codes(findings) == {"ZERO_VOLUME"}
    assert findings[0].severity == "warning"


@pytest.mark.parametrize(
    "field,value",
    [
        ("open", "NaN"),
        ("high", "Infinity"),
        ("low", "-Infinity"),
        ("close", None),
        ("volume", None),
        ("volume", "-0.5"),
        ("high", "9"),
        ("low", "12"),
        ("open", "0"),
    ],
)
def test_invalid_prices_and_missing_volume_are_errors(evidence, field, value):
    response, resolution = evidence
    payload = json.loads(response.payload)
    payload["bars"][0][field] = value
    _, findings, count = validate_prices(
        response.model_copy(update={"payload": encode(payload)}), resolution
    )
    assert count == 3
    assert "INVALID_OHLCV" in codes(findings)
    assert all(f.severity == "error" for f in findings)


@pytest.mark.parametrize(
    "mutation,expected",
    [
        (lambda rows: rows.pop(1), {"MISSING_SESSION", "STALE_DATA", "INSUFFICIENT_HISTORY"}),
        (lambda rows: rows.append(rows[0]), {"DUPLICATE_SESSION", "UNORDERED_SESSIONS"}),
        (lambda rows: rows.reverse(), {"UNORDERED_SESSIONS"}),
        (lambda rows: rows[0].update(session="2026-10-17"), {"UNEXPECTED_SESSION"}),
        (lambda rows: rows[0].update(session="bad"), {"INVALID_SESSION"}),
        (lambda rows: rows[0].update(timestamp="2026-10-14T20:00:00"), {"INVALID_TIMESTAMP"}),
        (lambda rows: rows[0].update(timestamp="2026-10-14T19:00:00Z"), {"INVALID_TIMESTAMP"}),
        (
            lambda rows: rows[0].update(open="5", high="6", low="4", close="5"),
            {"CORPORATE_ACTION_DISCONTINUITY"},
        ),
    ],
)
def test_session_timestamp_and_discontinuity_validation(evidence, mutation, expected):
    response, resolution = evidence
    payload = json.loads(response.payload)
    mutation(payload["bars"])
    _, findings, _ = validate_prices(
        response.model_copy(update={"payload": encode(payload)}), resolution
    )
    assert expected <= codes(findings)


@pytest.mark.parametrize("payload", [b"bad", b"{}", b'{"bars":null}', b'{"bars":[null,42,{}]}'])
def test_malformed_input_stays_a_quality_failure(evidence, payload):
    response, resolution = evidence
    _, findings, _ = validate_prices(response.model_copy(update={"payload": payload}), resolution)
    assert findings
    assert all(f.severity == "error" for f in findings)


def test_metadata_freshness_and_adjustment_failures(evidence):
    response, resolution = evidence
    response = response.model_copy(
        update={"adjustment": "raw", "retrieved_at": datetime(2026, 10, 15, tzinfo=UTC)}
    )
    _, findings, _ = validate_prices(response, resolution)
    assert {"ADJUSTMENT_MISMATCH", "RETRIEVED_BEFORE_REQUEST", "INVALID_TIMESTAMP"} <= codes(
        findings
    )


def test_immutable_files_do_not_overwrite_existing_evidence(tmp_path):
    path = tmp_path / "snapshot" / "source.json"
    assert immutable_write(path, b"original") == hashlib.sha256(b"original").hexdigest()
    immutable_write(path, b"original")
    with pytest.raises(ValueError, match="Conflicting immutable"):
        immutable_write(path, b"changed")
    assert path.read_bytes() == b"original"
    assert not list(path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "field,number",
    [
        ("volume", "1.1234567890123456789"),
        ("open", "10.1234567890123456789"),
        ("close", "11.1234567890123456789"),
        ("volume", "1.1234567890123456789e-20"),
    ],
)
def test_numeric_json_preserves_decimal_precision(evidence, field, number):
    response, resolution = evidence
    payload = json.loads(response.payload)
    payload["bars"][0][field] = "NUMERIC_TOKEN"
    raw = encode(payload).replace(b'"NUMERIC_TOKEN"', number.encode())
    bars, findings, _ = validate_prices(response.model_copy(update={"payload": raw}), resolution)
    assert not findings
    assert getattr(bars[0], field) == Decimal(number)
    # Persisting the normalized bar must preserve the same exact value too.
    assert json.loads(bars[0].model_dump_json())[field] == str(Decimal(number))
