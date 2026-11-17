"""Synthetic earnings exposure walkthrough; never live qualification or advice."""

import argparse
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_analyser.calendars import ExchangeCalendarAdapter
from stock_analyser.collection import encode, immutable_write
from stock_analyser.earnings import (
    EarningsContext,
    EarningsEvent,
    EarningsPolicy,
    EarningsSourceEvidence,
    EarningsSourcePolicy,
    assess_earnings,
    freeze_earnings_evidence,
    replay_earnings,
)
from stock_analyser.fixtures import fixture_id, synthetic_markets
from stock_analyser.markets import US_MARKET
from stock_analyser.research import EarningsObservation


def example_inputs():
    _, _, adapters = synthetic_markets()
    stock = adapters[0].listings[0].model_copy(update={"venue": "XNYS"})
    stamp = datetime(2026, 11, 6, 22, tzinfo=UTC)
    context = EarningsContext(
        market=US_MARKET,
        instrument=stock,
        signal_session=date(2026, 11, 6),
        signal_available_at=stamp - timedelta(minutes=30),
        as_of=stamp,
    )
    policy = EarningsPolicy(
        max_holding_sessions=20,
        pre_open_buffer_seconds=900,
        sources=tuple(
            EarningsSourcePolicy(
                source_id=sid,
                publisher_id=f"synthetic-{sid}-organization",
                role=sid,
                qualification="SYNTHETIC",
                qualification_reference=f"synthetic://{sid}-contract-1",
                max_age_seconds=86400,
            )
            for sid in ("issuer", "independent")
        ),
    )
    sources = tuple(
        EarningsSourceEvidence(
            source_id=sid,
            publisher_id=f"synthetic-{sid}-organization",
            revision="synthetic-1",
            instrument_id=stock.instrument_id,
            security_id=stock.security_id,
            exchange_timezone=US_MARKET.timezone,
            scope="synthetic",
            coverage_state="VERIFIED",
            coverage_start=date(2026, 11, 6),
            coverage_end=date(2026, 12, 31),
            coverage_as_of=stamp - timedelta(hours=1),
            published_at=stamp - timedelta(minutes=30),
            retrieved_at=stamp,
            available_at=stamp,
            coverage_source_url=f"https://{sid}.example.com/calendar",
            coverage_reference=f"synthetic://{sid}/explicit-coverage",
            events=(
                EarningsEvent(
                    event_id="synthetic-2026-Q4",
                    announcement=EarningsObservation(
                        evidence_id=fixture_id(f"earnings-example:{sid}"),
                        source_url=f"https://{sid}.example.com/release",
                        retrieved_at=stamp,
                        published_at=stamp - timedelta(hours=2),
                        original_date=date(2026, 12, 8),
                        original_timezone=US_MARKET.timezone,
                        exchange_timezone=US_MARKET.timezone,
                        normalized_session_date=date(2026, 12, 8),
                    ),
                    release_state="SCHEDULED",
                    call_state="UNKNOWN",
                ),
            ),
            recent_event_state="NONE_CONFIRMED",
        )
        for sid in ("issuer", "independent")
    )
    return context, policy, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print("Synthetic earnings gate; recommendation eligibility is NOT_EVALUATED.")
    print(
        "Example choices only: 20 holding sessions, 15-minute pre-open buffer, 24-hour freshness."
    )
    for name in ("fits", "overlap", "unknown-coverage", "conflicting-date", "unqualified-source"):
        context, policy, sources = example_inputs()
        if name in ("overlap", "conflicting-date"):
            revised = []
            for index, source in enumerate(sources):
                if name == "conflicting-date" and index == 0:
                    revised.append(source)
                    continue
                event = source.events[0]
                announcement = event.announcement.model_copy(
                    update={
                        "original_date": date(2026, 12, 7),
                        "normalized_session_date": date(2026, 12, 7),
                    }
                )
                revised.append(
                    source.model_copy(
                        update={
                            "revision": "synthetic-2",
                            "events": (event.model_copy(update={"announcement": announcement}),),
                        }
                    )
                )
            sources = tuple(revised)
        elif name == "unknown-coverage":
            sources = tuple(s.model_copy(update={"coverage_state": "UNVERIFIED"}) for s in sources)
        elif name == "unqualified-source":
            policy = policy.model_copy(
                update={
                    "sources": tuple(
                        s.model_copy(update={"qualification": "UNVERIFIED"}) for s in policy.sources
                    )
                }
            )
        refs = tuple(freeze_earnings_evidence(args.output / "sources", s) for s in sources)
        result = assess_earnings(
            context, policy, refs, ExchangeCalendarAdapter(), calculated_at=context.as_of
        )
        replay_earnings(
            result, ExchangeCalendarAdapter(), calculated_at=context.as_of + timedelta(days=1)
        )
        immutable_write(args.output / f"{name}.json", encode(result.model_dump(mode="json")))
        print(
            f"{name}: {result.status}; entry={result.entry_session}; "
            f"holding={result.holding_deadline}; pre-event={result.pre_event_deadline}; "
            f"failed={result.failed_conditions}; unavailable={result.unavailable_reasons}"
        )


if __name__ == "__main__":
    main()
