"""Research contracts preserve uncertainty, original timing and metadata boundaries."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_analyser.fixtures import research_fixture, synthetic_markets
from stock_analyser.research import (
    EarningsEvidence,
    EarningsObservation,
    NewsCoverage,
    NewsItem,
    ResearchRecord,
    render_research,
    source_url,
)

CUTOFF = datetime(2026, 10, 16, 21, tzinfo=UTC)


def observation(**changes):
    return EarningsObservation.model_validate(
        {
            "evidence_id": uuid4(),
            "source_url": "https://example.com/earnings",
            "retrieved_at": CUTOFF,
            "original_date": date(2026, 10, 21),
            "original_timezone": "UTC",
            "original_timestamp": "2026-10-21T00:30:00Z",
            "exchange_timezone": "America/New_York",
            "normalized_session_date": date(2026, 10, 20),
            "corroboration_source": "https://example.org/issuer-calendar",
            **changes,
        }
    )


def test_event_timezone_roundtrip_and_conflicts():
    first = observation()
    assert first.original_date != first.normalized_session_date
    assert EarningsObservation.model_validate_json(first.model_dump_json()) == first
    known = EarningsEvidence(state="KNOWN", observations=(first,))
    assert known.mandatory_gate_cleared is False
    second = observation(
        original_date=date(2026, 10, 22),
        original_timestamp="2026-10-22T00:30:00Z",
        normalized_session_date=date(2026, 10, 21),
    )
    conflicting = EarningsEvidence(state="CONFLICTING", observations=(first, second))
    assert len(conflicting.observations) == 2
    assert conflicting.forward_coverage == "UNVERIFIED"
    with pytest.raises(ValidationError, match="state"):
        EarningsEvidence(state="KNOWN", observations=(first, second))
    with pytest.raises(ValidationError):
        EarningsEvidence(mandatory_gate_cleared=True)


def test_date_only_cross_timezone_stays_unknown_and_bad_normalization_rejected():
    event = observation(original_timestamp=None, normalized_session_date=None)
    assert EarningsEvidence(observations=(event,)).state == "UNKNOWN"
    for changes in (
        {"normalized_session_date": date(2026, 10, 21)},
        {"original_date": date(2026, 10, 20)},
        {"original_timezone": "America/New_York"},
        {"exchange_timezone": "Not/AZone"},
        {"published_at": CUTOFF + timedelta(days=1)},
    ):
        with pytest.raises(ValidationError):
            observation(**changes)


@pytest.mark.parametrize(
    "scenario,state",
    [
        ("relevant", "UNKNOWN"),
        ("irrelevant", "UNKNOWN"),
        ("empty", "UNKNOWN"),
        ("failed", "UNKNOWN"),
        ("known-event", "KNOWN"),
        ("unknown-event", "UNKNOWN"),
        ("conflicting-event", "CONFLICTING"),
    ],
)
def test_distinct_research_fixture_renderings(scenario, state):
    markets, instruments, _ = synthetic_markets()
    market, instrument = markets.resolve("US"), instruments.resolve("US", "ACME")
    record = ResearchRecord(
        run_id=uuid4(),
        instrument=instrument,
        **research_fixture(instrument, market, CUTOFF, scenario),
    )
    assert record.earnings.state == state
    assert ResearchRecord.model_validate_json(record.model_dump_json()) == record
    rendered = "\n".join(render_research((record,)))
    assert f"Earnings {state}" in rendered
    assert "mandatory gate cleared: False" in rendered
    assert "investor-relations" in rendered
    assert "NOT_SENT" in rendered
    if scenario in ("relevant", "irrelevant"):
        assert f"relevance {scenario.upper()}" in rendered
        assert "ticker association True" in rendered
    if scenario == "conflicting-event":
        assert "2026-10-20" in rendered and "2026-10-21" in rendered
        assert "America/New_York" in rendered


def test_public_urls_keep_article_identity_and_drop_credentials():
    assert source_url("https://example.com/article?id=one&utm_source=x&apiKey=secret#part") == (
        "https://example.com/article?id=one"
    )
    for value in ("javascript:alert(1)", "https://user:secret@example.com", "https://x/a\nb"):
        with pytest.raises(ValueError):
            source_url(value)


def test_news_contract_rejects_content_and_false_completeness():
    item = dict(
        evidence_id=uuid4(),
        provider_id="one",
        source_url="https://example.com/one",
        published_at=CUTOFF,
        retrieved_at=CUTOFF,
        ticker_associated=True,
    )
    for changes in (
        {"description": "not permitted"},
        {"relevance": "RELEVANT"},
        {"published_at": CUTOFF + timedelta(seconds=1)},
    ):
        with pytest.raises(ValidationError):
            NewsItem(**(item | changes))
    base = dict(
        provider="massive",
        provider_symbol="AAPL",
        window_start=CUTOFF,
        window_end=CUTOFF,
        retrieved_at=CUTOFF,
        status="EMPTY",
        reason="Empty",
        pages=1,
    )
    for changes in (
        {"status": "COMPLETE"},
        {"items": (NewsItem(**item),)},
        {"pages": 0},
        {"rejected": 1},
    ):
        with pytest.raises(ValidationError):
            NewsCoverage(**(base | changes))
