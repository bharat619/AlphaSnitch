"""Explicit market resolution; production configuration enables US only."""

from datetime import date

from stock_analyser.contracts import (
    BenchmarkMapping,
    CalendarReference,
    FeedCoverage,
    Instrument,
    MarketContext,
    ProviderCapabilities,
    ProviderRoute,
)

US_MARKET = MarketContext(
    market_id="US",
    configuration_version="us-1",
    currency="USD",
    timezone="America/New_York",
    calendars=tuple(
        CalendarReference(venue=venue, reference=venue, version="us-calendar-contract-1")
        for venue in ("XNAS", "XNYS", "ARCX")
    ),
    routes=(
        ProviderRoute(
            role="prices",
            provider="massive",
            feed=FeedCoverage(feed_id="us-consolidated", kind="consolidated", venues=("US_ALL",)),
        ),
        ProviderRoute(
            role="long_history_prices",
            provider="yfinance",
            feed=FeedCoverage(feed_id="yfinance-us", kind="consolidated", venues=("US_ALL",)),
        ),
    ),
    capabilities=(
        ProviderCapabilities(
            provider="massive", daily_prices=True, weekly_prices=True, max_history_days=730
        ),
        ProviderCapabilities(provider="yfinance", daily_prices=True, weekly_prices=True),
    ),
    benchmarks=(BenchmarkMapping(key="broad_market", symbol="SPY", venue="ARCX", currency="USD"),),
    strategy_profile="us-swing-1",
)


class MarketRegistry:
    def __init__(self, contexts: tuple[MarketContext, ...] = (US_MARKET,)) -> None:
        self._contexts = {context.market_id: context for context in contexts}
        if len(self._contexts) != len(contexts):
            raise ValueError("Duplicate market IDs")

    def resolve(self, market_id: str) -> MarketContext:
        try:
            return self._contexts[market_id]
        except KeyError:
            raise ValueError("Unsupported market. Only configured markets are enabled.") from None


class InstrumentRegistry:
    """Resolve listings by market and venue, never by bare ticker globally."""

    def __init__(self, instruments: tuple[Instrument, ...]) -> None:
        self._instruments = instruments
        ids = [i.instrument_id for i in instruments]
        if len(ids) != len(set(ids)):
            raise ValueError("Duplicate immutable instrument IDs")

    def resolve(self, market_id: str, symbol: str, venue: str | None = None) -> Instrument:
        matches = [
            i
            for i in self._instruments
            if i.market_id == market_id
            and i.symbol == symbol
            and (venue is None or i.venue == venue)
        ]
        if len(matches) != 1:
            raise ValueError("Instrument symbol is unknown or ambiguous; specify market and venue")
        return matches[0]

    def resolve_provider_symbol(
        self, market_id: str, provider: str, symbol: str, session: date
    ) -> Instrument:
        matches = [
            i
            for i in self._instruments
            if i.market_id == market_id
            and any(
                m.provider == provider and m.symbol == symbol and m.contains(session)
                for m in i.provider_symbols
            )
        ]
        if len(matches) != 1:
            raise ValueError("Provider symbol is unknown or ambiguous for this market and date")
        return matches[0]
