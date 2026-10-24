"""Approved US technical-price adapter; never a transport-error fallback."""

from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from stock_analyser.calendars import ExchangeCalendarAdapter
from stock_analyser.collection import RawPriceResponse, encode
from stock_analyser.contracts import PriceBar


class YFinanceError(ValueError):
    pass


class YFinancePriceProvider:
    """Freeze one daily frame per listing for all of its daily/weekly requests.

    Yahoo's OHLC already reflects splits. With auto_adjust disabled, dividends
    are separate and Adj Close is retained only for inspection. It is never
    substituted for technical closes or claimed to establish total returns.
    """

    def __init__(self, resolutions=(), *, ticker_factory=None, clock=None):
        if ticker_factory is None:
            from yfinance import Ticker

            ticker_factory = Ticker
        self.ticker_factory = ticker_factory
        self.clock = clock or (lambda: datetime.now(UTC))
        self.resolutions = resolutions
        self._frames = {}

    def fetch_window(self, instrument, market, resolution):
        """A cache refresh must honor its bounded window and make a new request."""
        return YFinancePriceProvider(ticker_factory=self.ticker_factory, clock=self.clock).fetch(
            instrument, market, resolution
        )

    def fetch(self, instrument, market, resolution):
        market.validate_instrument(instrument)
        if (
            market.market_id != "US"
            or market.timezone != "America/New_York"
            or market.route("long_history_prices").provider != "yfinance"
            or resolution.market_id != market.market_id
            or resolution.market_configuration_version != market.configuration_version
            or resolution.timezone != market.timezone
            or resolution.venue != instrument.venue
            or not resolution.periods
            or resolution.requirement.adjustment != "split"
        ):
            raise YFinanceError("yfinance requires resolved US split-adjusted technical history")
        plans = tuple(
            p
            for p in self.resolutions
            if p.venue == instrument.venue and p.requested_as_of == resolution.requested_as_of
        ) + (resolution,)
        start = min(p.history_start for p in plans if p.periods)
        end = max(p.session_date for p in plans if p.periods)
        symbol = instrument.provider_symbol("yfinance", start)
        if any(
            instrument.provider_symbol("yfinance", d) != symbol
            for p in plans
            for period in p.periods
            for d in (period.session_start, period.session_end)
        ):
            raise YFinanceError("yfinance history crosses a provider symbol change")
        key = (instrument.instrument_id, symbol, start, end, resolution.requested_as_of)
        if key not in self._frames:
            options = {
                "start": start.isoformat(),
                "end": (end + timedelta(days=1)).isoformat(),
                "period": None,
                "interval": "1d",
                "auto_adjust": False,
                "back_adjust": False,
                "actions": True,
                "repair": False,
                "keepna": True,
                "rounding": False,
                "prepost": False,
                "timeout": 30,
                "raise_errors": True,
            }
            try:
                ticker = self.ticker_factory(symbol)
                frame = ticker.history(**options).copy(deep=True)
                metadata = ticker.get_history_metadata()
            except Exception:
                raise YFinanceError(
                    "yfinance history request failed; source remains yfinance"
                ) from None
            # Preserve received values, including invalid/NaN values, as strings.
            rows = [
                {"source_timestamp": str(stamp), **{str(k): str(v) for k, v in row.items()}}
                for stamp, row in frame.iterrows()
            ]
            self._frames[key] = (frame, rows, metadata, options, self.clock())
        frame, rows, metadata, options, retrieved = self._frames[key]
        flags = ["PUBLICATION_UNVERIFIED"]
        if metadata.get("currency") != instrument.quote_currency:
            flags.append("CURRENCY_MISMATCH")
        if metadata.get("exchangeTimezoneName") != market.timezone:
            flags.append("TIMEZONE_MISMATCH")
        bars = {}
        labels = []
        for stamp, row in frame.iterrows():
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                flags.append("INVALID_SOURCE_TIMEZONE")
                continue
            local = stamp.to_pydatetime().astimezone(ZoneInfo(market.timezone))
            if local.time() != datetime.min.time():
                flags.append("INVALID_SOURCE_TIMESTAMP")
                continue
            session = local.date()
            labels.append(session)
            try:
                bars[session] = PriceBar(
                    session=session,
                    **{
                        name.lower(): str(row[name])
                        for name in ("Open", "High", "Low", "Close", "Volume")
                    },
                )
            except (ValidationError, KeyError):
                flags.append("INVALID_SOURCE_OHLCV")
        if labels != sorted(set(labels)):
            flags.append("DUPLICATE_OR_UNORDERED_SOURCE_SESSION")
        schedule = ExchangeCalendarAdapter().schedule(market, instrument.venue, start, end)
        scheduled = {s.session for s in schedule}
        if set(labels) - scheduled:
            flags.append("UNEXPECTED_SOURCE_SESSION")
        normalized = []
        for period in resolution.periods:
            expected = sorted(
                s for s in scheduled if period.session_start <= s <= period.session_end
            )
            if not expected or not set(expected) <= bars.keys():
                continue  # No partial week can masquerade as a complete weekly bar.
            group = [bars[s] for s in expected]
            normalized.append(
                {
                    "session": str(period.session_end),
                    "timestamp": period.closes_at.isoformat(),
                    "open": str(group[0].open),
                    "high": str(max(b.high for b in group)),
                    "low": str(min(b.low for b in group)),
                    "close": str(group[-1].close),
                    "volume": str(sum(b.volume for b in group)),
                }
            )
        return RawPriceResponse(
            provider="yfinance",
            provider_symbol=symbol,
            retrieved_at=retrieved,
            adjustment="split",
            feed=market.route("long_history_prices").feed,
            quality_flags=tuple(dict.fromkeys(flags)),
            payload=encode(
                {
                    "bars": normalized,
                    "daily_source": rows,
                    "request_options": options,
                    "yfinance_version": version("yfinance"),
                    "currency": metadata.get("currency"),
                    "source_timezone": metadata.get("exchangeTimezoneName"),
                    "normalization_timezone": market.timezone,
                    "price_basis": (
                        "split-adjusted technical OHLC; dividends separate; not total return"
                    ),
                    "timestamp_semantics": "scheduled close; original daily labels retained",
                    "publication_timing": "unverified",
                }
            ),
        )
