"""Deterministic synthetic adapters, independent of live providers and credentials.

These contexts are test data, not additional publicly enabled markets. Calendars
contain explicit fixture closes; they do not infer exchange schedules from weekdays.
"""

import hashlib
import json
from datetime import UTC, date, datetime
from itertools import groupby
from uuid import NAMESPACE_URL, uuid5

from stock_analyser.calendars import CalendarSession, calendar_reference
from stock_analyser.contracts import (
    BenchmarkMapping,
    CalendarReference,
    DataAvailability,
    EvidenceMetadata,
    FeedCoverage,
    HistoryRequirement,
    Instrument,
    MarketContext,
    PriceBar,
    ProviderCapabilities,
    ProviderRoute,
    SourceRecord,
    SymbolMapping,
)
from stock_analyser.markets import InstrumentRegistry, MarketRegistry
from stock_analyser.providers import CorporateAction, LLMResponse, PriceHistory, ResearchItem


def fixture_id(name: str):
    return uuid5(NAMESPACE_URL, f"alphasnitch:synthetic:v1:{name}")


def fixture_market(market_id: str, currency: str, venue: str, timezone: str) -> MarketContext:
    provider = f"fixture-{market_id.lower()}"
    feed = FeedCoverage(feed_id=provider, kind="synthetic", venues=(venue,))
    return MarketContext(
        market_id=market_id,
        configuration_version="synthetic-1",
        currency=currency,
        timezone=timezone,
        calendars=(CalendarReference(venue=venue, reference=provider, version="synthetic-1"),),
        routes=tuple(
            ProviderRoute(role=role, provider=provider, feed=feed)
            for role in ("prices", "universe", "research", "corporate_actions")
        ),
        capabilities=(
            ProviderCapabilities(
                provider=provider,
                daily_prices=True,
                universe=True,
                news=True,
                corporate_actions=True,
            ),
        ),
        benchmarks=(
            BenchmarkMapping(
                key="broad_market",
                symbol="INDEX",
                venue=venue,
                currency=currency,
            ),
        ),
        strategy_profile="synthetic-1",
    )


def _bars_sha256(bars: tuple[PriceBar, ...]) -> str:
    payload = "[" + ",".join(b.model_dump_json() for b in bars) + "]"
    return hashlib.sha256(payload.encode()).hexdigest()


class FixtureAdapter:
    def __init__(
        self,
        market: MarketContext,
        instruments: tuple[Instrument, ...],
        closes: tuple[datetime, ...],
        bars: tuple[PriceBar, ...],
    ) -> None:
        for instrument in instruments:
            market.validate_instrument(instrument)
        if any(c.tzinfo is None or c.utcoffset() is None for c in closes):
            raise ValueError("Fixture closes must be timezone aware")
        self.market = market
        self.listings = instruments
        self.closes = closes
        self.bars = bars

    def _context(self, market: MarketContext, cutoff: datetime) -> None:
        if market != self.market:
            raise ValueError("Fixture provider route belongs to another market context")
        if cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("Cutoff must be timezone aware")

    def _instrument(self, instrument: Instrument, market: MarketContext, cutoff: datetime) -> None:
        self._context(market, cutoff)
        market.validate_instrument(instrument)
        if instrument not in self.listings:
            raise ValueError("Instrument is not served by this fixture adapter")

    def instruments(self, market: MarketContext, as_of: datetime) -> tuple[Instrument, ...]:
        self._context(market, as_of)
        return self.listings

    def latest_completed_session(
        self,
        market: MarketContext,
        venue: str,
        as_of: datetime,
    ) -> datetime:
        self._context(market, as_of)
        if venue not in {c.venue for c in market.calendars}:
            raise ValueError("Unknown fixture venue")
        completed = [c for c in self.closes if c <= as_of]
        if not completed:
            raise ValueError("No completed session in fixture calendar")
        return max(completed)

    def history(
        self,
        instrument: Instrument,
        market: MarketContext,
        requirement: HistoryRequirement,
        cutoff: datetime,
    ) -> PriceHistory:
        self._instrument(instrument, market, cutoff)
        if requirement.timeframe != "daily" or requirement.adjustment != "split":
            raise ValueError("Fixture supports split-adjusted daily bars only")
        session = self.latest_completed_session(market, instrument.venue, cutoff)
        route = market.route("prices")
        bars = tuple(b for b in self.bars if b.session <= session.date())[
            -requirement.required_bars :
        ]

        def mapping_for(bar: PriceBar) -> SymbolMapping:
            for mapping in instrument.provider_symbols:
                if mapping.provider == route.provider and mapping.contains(bar.session):
                    return mapping
            raise ValueError("No provider symbol mapping for fixture bar session")

        sources = []
        for mapping, group in groupby(bars, key=mapping_for):
            covered_bars = tuple(group)
            source_digest = _bars_sha256(covered_bars)
            sources.append(
                SourceRecord(
                    provider=route.provider,
                    provider_symbol=mapping.symbol,
                    retrieved_at=cutoff,
                    reference=f"fixture://{market.market_id}/{instrument.instrument_id}/{source_digest}",
                    sha256=source_digest,
                    session_start=covered_bars[0].session,
                    session_end=covered_bars[-1].session,
                )
            )
        availability = (
            DataAvailability.AVAILABLE
            if len(bars) >= requirement.required_bars
            else DataAvailability.PARTIAL
            if bars
            else DataAvailability.UNAVAILABLE
        )
        metadata = EvidenceMetadata(
            snapshot_id=fixture_id("pending-snapshot-identity"),
            instrument=instrument,
            market=market,
            currency=instrument.quote_currency,
            session_cutoff=session,
            feed=route.feed,
            timeframe=requirement.timeframe,
            adjustment=requirement.adjustment,
            availability=availability,
            requested_bars=requirement.required_bars,
            returned_bars=len(bars),
            sources=tuple(sources),
            reason=None
            if availability == DataAvailability.AVAILABLE
            else "Insufficient fixture bars",
        )
        # Retrieval time, configuration, symbol history and requirements are immutable
        # snapshot content too. Equal bar bytes alone do not identify a collection.
        payload = json.dumps(
            metadata.model_dump(mode="json", exclude={"snapshot_id"}),
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()
        metadata = metadata.model_copy(update={"snapshot_id": fixture_id(f"snapshot:{digest}")})
        return PriceHistory(metadata=metadata, bars=bars)

    def research(
        self,
        instrument: Instrument,
        market: MarketContext,
        cutoff: datetime,
    ) -> tuple[ResearchItem, ...]:
        self._instrument(instrument, market, cutoff)
        return ()  # The synthetic dataset explicitly contains no research items.

    def corporate_actions(
        self,
        instrument: Instrument,
        market: MarketContext,
        cutoff: datetime,
    ) -> tuple[CorporateAction, ...]:
        self._instrument(instrument, market, cutoff)
        return ()  # The synthetic dataset explicitly contains no corporate actions.


class FixtureLLMProvider:
    def complete(self, prompt: str) -> LLMResponse:
        if not prompt.strip():
            raise ValueError("Prompt must not be empty")
        return LLMResponse(provider="fixture", model="synthetic-1", content="Synthetic response")


def synthetic_markets() -> tuple[MarketRegistry, InstrumentRegistry, tuple[FixtureAdapter, ...]]:
    """Same ticker, separate securities, currencies, providers and session closes."""
    contexts = (
        fixture_market("US", "USD", "XNAS", "America/New_York"),
        fixture_market("TEST", "INR", "XTEST", "Asia/Kolkata"),
    )
    instruments = tuple(
        Instrument(
            instrument_id=fixture_id(f"{m.market_id}:ACME:listing"),
            security_id=fixture_id(f"{m.market_id}:ACME:security"),
            market_id=m.market_id,
            venue=m.calendars[0].venue,
            symbol="ACME",
            quote_currency=m.currency,
            provider_symbols=(
                SymbolMapping(
                    provider=m.route("prices").provider,
                    symbol=f"ACME.{m.market_id}",
                    effective_from=date(2020, 1, 1),
                ),
            ),
        )
        for m in contexts
    )
    bars = (
        PriceBar(
            session=date(2026, 10, 15), open="10", high="12", low="9", close="11", volume="1.5"
        ),
        PriceBar(
            session=date(2026, 10, 16), open="11", high="13", low="10", close="12", volume="2.5"
        ),
    )
    adapters = tuple(
        FixtureAdapter(
            m,
            (i,),
            tuple(datetime(2026, 10, day, hour, minute, tzinfo=UTC) for day in (15, 16)),
            bars,
        )
        for m, i, hour, minute in zip(contexts, instruments, (20, 10), (0, 0), strict=True)
    )
    return MarketRegistry(contexts), InstrumentRegistry(instruments), adapters


class FixtureSessionCalendar:
    """Explicit synthetic schedule: omitted dates are closed, not inferred weekdays."""

    def __init__(self, market: MarketContext, sessions: tuple[CalendarSession, ...]) -> None:
        self.market = market
        self.sessions = sessions

    def data_version(self, market: MarketContext, venue: str) -> str:
        calendar_reference(market, venue)
        if market != self.market:
            raise ValueError("Fixture calendar belongs to another market context")
        return "synthetic-session-calendar-1"

    def schedule(
        self, market: MarketContext, venue: str, start: date, end: date
    ) -> tuple[CalendarSession, ...]:
        self.data_version(market, venue)
        return tuple(s for s in self.sessions if start <= s.session <= end)


def synthetic_session_calendars() -> tuple[MarketRegistry, tuple[FixtureSessionCalendar, ...]]:
    markets, _, adapters = synthetic_markets()
    # Two fully enumerated synthetic weeks; neither fixture asserts a live schedule.
    dates = tuple(date(2026, 10, day) for day in (5, 6, 7, 8, 9, 12, 13, 14, 15, 16))
    calendars = tuple(
        FixtureSessionCalendar(
            adapter.market,
            tuple(
                CalendarSession(
                    session=day,
                    opens_at=datetime(2026, 10, day.day, opening, tzinfo=UTC),
                    closes_at=datetime(2026, 10, day.day, closing, tzinfo=UTC),
                )
                for day in dates
            ),
        )
        for adapter, opening, closing in zip(adapters, (14, 4), (20, 10), strict=True)
    )
    return markets, calendars


class RawFixtureProvider:
    """Synthetic raw rows include exact close timestamps and decimal strings."""

    def __init__(self, scenario: str = "valid"):
        if scenario not in ("valid", "missing-bar", "provider-failure"):
            raise ValueError("Unknown collection fixture")
        self.scenario = scenario

    def fetch(self, instrument, market, resolution):
        from datetime import timedelta

        from stock_analyser.collection import RawPriceResponse, encode

        if self.scenario == "provider-failure":
            raise ConnectionError("Synthetic provider failure")
        periods = resolution.periods
        if self.scenario == "missing-bar":
            periods = periods[:-1]
        rows = [
            {
                "session": p.session_end.isoformat(),
                "timestamp": p.closes_at.isoformat(),
                "open": "10",
                "high": "12",
                "low": "9",
                "close": "11",
                "volume": "1.5",
            }
            for p in periods
        ]
        route = market.route("prices")
        return RawPriceResponse(
            provider=route.provider,
            provider_symbol=instrument.provider_symbol(route.provider, resolution.session_date),
            retrieved_at=resolution.requested_as_of + timedelta(seconds=1),
            adjustment=resolution.requirement.adjustment,
            feed=route.feed,
            payload=encode({"bars": rows}),
        )
