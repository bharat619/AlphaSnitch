"""Executable, synthetic owner walkthrough. No providers or database are used."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from stock_analyser.cache import digest
from stock_analyser.calendars import CalendarSession, CutoffPolicy, HistoryResolver
from stock_analyser.collection import (
    EvidencePacket,
    RawPriceResponse,
    encode,
    immutable_write,
    validate_prices,
)
from stock_analyser.contracts import (
    DataAvailability,
    EvidenceMetadata,
    HistoryRequirement,
    SourceRecord,
)
from stock_analyser.fixtures import FixtureSessionCalendar, synthetic_markets
from stock_analyser.indicators import InputProvenance, ObservationSpec, PublicationEvidence, observe
from stock_analyser.persistence.records import SnapshotReference

FEATURES = (
    "prior_high",
    "prior_mean_volume",
    "relative_volume",
    "close_distance",
    "close_above_prior_high",
)


def example_inputs():
    _, _, adapters = synthetic_markets()
    market, instrument = adapters[0].market, adapters[0].listings[0]
    # Enumerated synthetic sessions, including a weekend gap. No live calendar claim.
    days = (2, 5, 6, 7, 8, 9, 12, 13, 14, 15, 16, 19, 20, 21, 22, 23, 26, 27, 28, 29, 30)
    sessions = tuple(
        CalendarSession(
            session=date(2026, 10, day),
            opens_at=datetime(2026, 10, day, 14, tzinfo=UTC),
            closes_at=datetime(2026, 10, day, 20, tzinfo=UTC),
        )
        for day in days
    )
    calendar = FixtureSessionCalendar(market, sessions)
    highs = ["97"] * 15 + ["98", "100", "99", "101", "100", "104"]
    volumes = ["700000"] * 15 + ["800000", "1000000", "1200000", "900000", "1100000", "1600000"]
    rows = [
        dict(
            session=s.session.isoformat(),
            timestamp=s.closes_at.isoformat(),
            open="95",
            low="94",
            high=high,
            close="96" if i < 20 else "102",
            volume=volume,
        )
        for i, (s, high, volume) in enumerate(zip(sessions, highs, volumes, strict=True))
    ]
    return market, instrument, calendar, rows


def freeze_example(directory, *, rows=None, calendar=None, adjustment="split", flags=()):
    market, instrument, default_calendar, default_rows = example_inputs()
    calendar = calendar or default_calendar
    rows = default_rows if rows is None else rows
    as_of = datetime(2026, 10, 30, 21, tzinfo=UTC)
    resolution = HistoryResolver(
        calendar,
        CutoffPolicy(
            daily_grace_seconds=0,
            publication_timing="synthetic",
        ),
    ).resolve(
        market,
        instrument.venue,
        HistoryRequirement(
            timeframe="daily",
            minimum_bars=21,
            adjustment=adjustment,
        ),
        as_of,
    )
    route = market.route("prices")
    raw = encode({"bars": rows})
    raw_hash = digest({"bars": rows})
    raw_path = Path(directory).resolve() / f"{raw_hash}-source.json"
    immutable_write(raw_path, raw)
    response = RawPriceResponse(
        provider=route.provider,
        provider_symbol=instrument.provider_symbol(route.provider, as_of.date()),
        retrieved_at=as_of,
        adjustment=adjustment,
        feed=route.feed,
        payload=raw,
        quality_flags=flags,
    )
    bars, findings, count = validate_prices(response, resolution)
    errors = any(f.severity == "error" for f in findings)
    metadata = EvidenceMetadata(
        snapshot_id=uuid5(NAMESPACE_URL, raw_hash + adjustment + str(flags)),
        instrument=instrument,
        market=market,
        currency="USD",
        session_cutoff=resolution.session_cutoff,
        feed=route.feed,
        timeframe="daily",
        adjustment=adjustment,
        availability=DataAvailability.PARTIAL if errors else DataAvailability.AVAILABLE,
        requested_bars=21,
        returned_bars=count,
        sources=(
            SourceRecord(
                provider=route.provider,
                provider_symbol=response.provider_symbol,
                retrieved_at=as_of,
                reference=str(raw_path),
                sha256=raw_hash,
                session_start=resolution.history_start,
                session_end=resolution.session_date,
            ),
        ),
        quality_flags=tuple(f.code for f in findings),
        reason="Synthetic partial input" if errors else None,
    )
    packet = EvidencePacket(
        metadata=metadata,
        resolution=resolution,
        findings=findings,
        bars=bars,
        raw_reference=str(raw_path),
        response=response.model_dump(mode="json", exclude={"payload"}),
        source_version={"fixture": "prior-session-example-1"},
        component_versions={"collection": "1"},
    )
    content = encode(packet.model_dump(mode="json"))
    manifest = Path(directory).resolve() / f"{digest(packet.model_dump(mode='json'))}-manifest.json"
    sha = immutable_write(manifest, content)
    reference = SnapshotReference(metadata=metadata, reference=str(manifest), sha256=sha)
    provenance = InputProvenance(
        snapshot_sha256=reference.sha256,
        volume_adjustment="split_consistent",
        volume_verification_reference="synthetic://volume-v1",
        publications=tuple(
            PublicationEvidence(
                session=s.session,
                verified_at=s.closes_at + timedelta(minutes=10),
                verification_reference="synthetic://publication-v1",
            )
            for s in calendar.sessions
        ),
    )
    return reference, provenance, calendar, as_of


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    print("Synthetic illustrative N_price=N_volume=5; no strategy is enabled.")
    print("prior_high=max(prior highs); prior_mean_volume=sum(prior volumes)/N_volume")
    print("relative_volume=current volume/prior_mean_volume; close_distance=close-prior_high")
    print("above is the strict close > prior_high comparison (tolerance zero).")
    for scenario in ("above", "equal", "below", "missing-volume"):
        _, _, _, rows = example_inputs()
        if scenario == "equal":
            rows[-1]["close"] = "101"
        if scenario == "below":
            rows[-1]["close"] = "100"
        if scenario == "missing-volume":
            rows[-1].pop("volume")
        snapshot, provenance, calendar, as_of = freeze_example(args.output / scenario, rows=rows)
        print(f"\n{scenario}: prior rows exclude current high=104 and volume=1600000")
        print(json.dumps(rows[-6:], indent=2))
        observations = []
        for feature in FEATURES:
            result = observe(
                snapshot,
                ObservationSpec(
                    feature_id=feature,
                    price_lookback=5,
                    volume_lookback=5,
                    regular_session_seconds=21600,
                ),
                provenance,
                calendar,
                date(2026, 10, 30),
                as_of=as_of,
                calculated_at=as_of,
            )
            print(f"{feature}: {result.value} [{result.status}] reasons={result.reasons}")
            print(
                f"  prior={result.prior_sessions}; available_at={result.available_at}; "
                f"historical={result.historical_availability}"
            )
            observations.append(result.model_dump(mode="json"))
        path = args.output / f"{scenario}-observations.json"
        path.write_bytes(encode(observations))
        print(f"Structured observations: {path}")


if __name__ == "__main__":
    main()
