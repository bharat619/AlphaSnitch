"""Synthetic sector population and cached evidence for repeatable offline demonstrations."""

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_analyser.cache import EvidenceCache
from stock_analyser.calendars import CalendarSession, CutoffPolicy, HistoryResolver
from stock_analyser.collection import (
    EvidencePacket,
    RawPriceResponse,
    encode,
    immutable_write,
    validate_prices,
)
from stock_analyser.contracts import (
    AnalysisRequest,
    DataAvailability,
    EvidenceMetadata,
    Instrument,
    SourceRecord,
    SymbolMapping,
)
from stock_analyser.fixtures import FixtureSessionCalendar, fixture_id, fixture_market
from stock_analyser.persistence.records import RunRecord, SnapshotReference
from stock_analyser.universe import (
    Classification,
    ReferenceUniverseAdapter,
    SelectionPolicy,
    SelectionRequest,
)
from stock_analyser.universe_spike import Enumeration


def fixture_request(
    directory: Path,
    *,
    eligible_count=12,
    policy=None,
    as_of=None,
    cutoff_policy=None,
    extra_symbols=(),
):
    """Create a new synthetic dataset; all price histories are published to EvidenceCache."""
    directory = directory.resolve()
    market = fixture_market("US", "USD", "XNAS", "America/New_York")
    cutoff = as_of or datetime(2026, 10, 16, 21, tzinfo=UTC)
    cutoff_policy = cutoff_policy or CutoffPolicy(publication_timing="synthetic")
    sessions = []
    day = date(2025, 1, 1)
    while day <= cutoff.date():
        if day.weekday() < 5:
            sessions.append(
                CalendarSession(
                    session=day,
                    opens_at=datetime(day.year, day.month, day.day, 14, tzinfo=UTC),
                    closes_at=datetime(day.year, day.month, day.day, 20, tzinfo=UTC),
                )
            )
        day += timedelta(days=1)
    resolver = HistoryResolver(FixtureSessionCalendar(market, tuple(sessions)), cutoff_policy)
    policy = policy or SelectionPolicy()
    rows, instruments = [], {}
    symbols = [f"TECH{n:02}" for n in range(eligible_count)]
    symbols += list(extra_symbols)
    symbols += ["ETF", "ADR", "OTC", "UNKNOWN", "CHEAP", "ILLIQUID", "SHORT", "ALTERNATE"]
    for symbol in symbols:
        share = "TECH00" if symbol == "ALTERNATE" else symbol
        rows.append(
            {
                "ticker": symbol,
                "active": True,
                "market": "stocks",
                "locale": "us",
                "type": "ETF" if symbol == "ETF" else "ADRC" if symbol == "ADR" else "CS",
                "primary_exchange": "OTC" if symbol == "OTC" else "XNAS",
                "currency_name": "usd",
                "share_class_figi": f"fixture-share-{share}",
                "composite_figi": "zz-alternate" if symbol == "ALTERNATE" else f"listing-{symbol}",
            }
        )
        instruments[symbol] = Instrument(
            instrument_id=fixture_id(f"universe:listing:{symbol}"),
            security_id=fixture_id(f"universe:security:{share}"),
            market_id="US",
            venue="XNAS",
            symbol=symbol,
            quote_currency="USD",
            provider_symbols=(
                SymbolMapping(
                    provider="fixture-us",
                    symbol=symbol,
                    effective_from=date(2020, 1, 1),
                ),
            ),
        )
    reference_path = directory / "reference.json"
    source = SourceRecord(
        provider="fixture-us",
        provider_symbol="US",
        retrieved_at=cutoff,
        reference=str(reference_path),
        sha256=immutable_write(reference_path, encode(rows)),
    )

    def classify(row, instrument):
        sector = None if row.ticker == "UNKNOWN" else "Technology"
        payload = {"sector": sector, "industry": "Synthetic electronics", "symbol": row.ticker}
        path = directory / "profiles" / f"{row.ticker}.json"
        return Classification(
            source=SourceRecord(
                provider="fixture-us",
                provider_symbol=row.ticker,
                retrieved_at=cutoff,
                reference=str(path),
                sha256=immutable_write(path, encode(payload)),
            ),
            status="OBSERVED",
            original_sector=sector,
            original_industry=payload["industry"],
        )

    population = (
        ReferenceUniverseAdapter(
            Enumeration(
                "fixture-us",
                {},
                rows=rows,
                pages=[{"rows": len(rows)}],
                status="COMPLETE",
                reason="SYNTHETIC_TERMINAL_PAGE",
            ),
            source,
            lambda row: instruments[row.ticker],
            classify,
        )
        .enumerate()
        .model_copy(update={"scope": "Synthetic fixture, not live sector coverage"})
    )
    references = []
    cache = EvidenceCache(directory / "cache", clock=lambda: cutoff + timedelta(seconds=1))
    requirements = policy.requirements()
    resolutions = tuple(resolver.resolve(market, "XNAS", h, cutoff) for h in requirements)
    for c in population.candidates:
        instrument = c.instrument
        if instrument is None:
            continue
        symbol = instrument.symbol
        # One run per basis: collection contracts allow one requirement per timeframe.
        for plan in resolutions:
            close = "4" if symbol == "CHEAP" else "10"
            volume = "1" if symbol == "ILLIQUID" else "2000000"
            periods = plan.periods[:-1] if symbol == "SHORT" else plan.periods
            response = RawPriceResponse(
                provider="fixture-us",
                provider_symbol=symbol,
                retrieved_at=cutoff + timedelta(seconds=1),
                adjustment=plan.requirement.adjustment,
                feed=market.route("prices").feed,
                payload=encode(
                    {
                        "bars": [
                            {
                                "session": p.session_end.isoformat(),
                                "timestamp": p.closes_at.isoformat(),
                                "open": close,
                                "high": close,
                                "low": close,
                                "close": close,
                                "volume": volume,
                            }
                            for p in periods
                        ]
                    }
                ),
            )
            tag = f"{symbol}-{plan.requirement.timeframe}-{plan.requirement.adjustment}"
            raw = directory / "prices" / tag / "source.json"
            sha = immutable_write(raw, response.payload)
            bars, findings, count = validate_prices(response, plan)
            errors = any(f.severity == "error" for f in findings)
            metadata = EvidenceMetadata(
                snapshot_id=fixture_id(f"universe:history:{sha}:{tag}"),
                instrument=instrument,
                market=market,
                currency="USD",
                session_cutoff=plan.session_cutoff,
                feed=response.feed,
                timeframe=plan.requirement.timeframe,
                adjustment=response.adjustment,
                availability=DataAvailability.PARTIAL if errors else DataAvailability.AVAILABLE,
                requested_bars=plan.requirement.required_bars,
                returned_bars=count,
                reason="Synthetic missing history" if errors else None,
                quality_flags=tuple(f.code for f in findings),
                sources=(
                    SourceRecord(
                        provider="fixture-us",
                        provider_symbol=symbol,
                        retrieved_at=response.retrieved_at,
                        reference=str(raw),
                        sha256=sha,
                        session_start=plan.history_start,
                        session_end=plan.session_date,
                    ),
                ),
            )
            packet = EvidencePacket(
                metadata=metadata,
                resolution=plan,
                findings=findings,
                bars=bars,
                raw_reference=str(raw),
                response=response.model_dump(mode="json", exclude={"payload"}),
                source_version={"fixture": "universe-1"},
                component_versions={"fixture": "1"},
            )
            manifest = raw.parent / "manifest.json"
            reference = SnapshotReference(
                metadata=metadata,
                reference=str(manifest),
                sha256=immutable_write(manifest, encode(packet.model_dump(mode="json"))),
            )
            run = RunRecord(
                run_id=fixture_id(f"universe:run:{tag}"),
                request=AnalysisRequest(
                    request_id=fixture_id(f"universe:request:{tag}"),
                    mode="buy",
                    market_id="US",
                    instrument_ids=(instrument.instrument_id,),
                    requested_as_of=cutoff,
                    strategy_version="synthetic-universe-1",
                    history=(plan.requirement,),
                ),
                market=market,
                configuration={},
                source_version=packet.source_version,
                component_versions=packet.component_versions,
                history_resolutions=(plan,),
            )
            identity = cache.identity(run, instrument, plan, market.route("prices"))
            cache.commit(identity, reference, packet)
            if not errors:
                # Feed ranking with the existing cache's exact snapshot, not a new fetch/copy.
                reference, _ = cache.lookup(identity, plan, "replay")
            references.append(reference)
    return SelectionRequest(
        market=market,
        requested_as_of=cutoff,
        population=population,
        policy=policy,
        cutoff_policy=cutoff_policy,
        histories=tuple(references),
    ), resolver
