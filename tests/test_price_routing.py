"""Capability routing and actual complete-history checks use no provider network."""

import json
from datetime import UTC, date, datetime

import pandas as pd
import pytest

from stock_analyser.calendars import CutoffPolicy, ExchangeCalendarAdapter, HistoryResolver
from stock_analyser.collection import validate_prices
from stock_analyser.contracts import HistoryRequirement
from stock_analyser.live import smoke_instrument
from stock_analyser.markets import US_MARKET
from stock_analyser.routing import RoutingSettings, VerifiedCoverage, select_price_route
from stock_analyser.yfinance import YFinanceError, YFinancePriceProvider

AS_OF = datetime(2026, 10, 17, tzinfo=UTC)
COVERAGE = RoutingSettings(
    massive_coverage=VerifiedCoverage(
        start=date(2024, 10, 17), end=date(2026, 10, 16), reference="verified fixture account"
    )
)


def plan(timeframe="daily", n=3, *, mandatory=True, adjustment="split", venue="XNAS", warmup=0):
    return HistoryResolver(ExchangeCalendarAdapter(), CutoffPolicy()).resolve(
        US_MARKET,
        venue,
        HistoryRequirement(
            timeframe=timeframe,
            minimum_bars=n,
            warmup_bars=warmup,
            mandatory=mandatory,
            adjustment=adjustment,
        ),
        AS_OF,
    )


def test_short_verified_history_uses_massive_and_optional_history_cannot_switch():
    decision = select_price_route(
        US_MARKET, (plan(), plan("weekly", 260, mandatory=False)), COVERAGE
    )
    assert decision.route.provider == "massive"
    assert "verified coverage meets" in decision.reason
    assert len(decision.mandatory_history) == 1
    assert decision.massive_coverage == COVERAGE.massive_coverage


def test_long_weekly_requirement_selects_one_yfinance_route_for_all_venues():
    plans = (plan(), plan("weekly", 260), plan(venue="ARCX"), plan("weekly", 260, venue="ARCX"))
    decision = select_price_route(US_MARKET, plans, COVERAGE)
    assert decision.route.provider == "yfinance"
    assert decision.mandatory_history == plans
    assert "mandatory history exceeds" in decision.reason


def test_warmup_and_first_week_constituent_must_fit_verified_coverage():
    weekly = plan("weekly", 1)
    settings = RoutingSettings(
        massive_coverage=VerifiedCoverage(
            start=weekly.session_date, end=weekly.session_date, reference="last daily session only"
        )
    )
    assert select_price_route(US_MARKET, (weekly,), settings).route.provider == "yfinance"
    daily = plan(warmup=10)
    assert len(daily.periods) == 13
    assert select_price_route(US_MARKET, (daily,), settings).route.provider == "yfinance"


def test_unverified_and_expired_coverage_are_never_assumed_capable():
    assert select_price_route(US_MARKET, (plan(),)).route.provider == "yfinance"
    old = COVERAGE.model_copy(
        update={
            "massive_coverage": VerifiedCoverage(
                start=date(2020, 1, 1), end=date(2026, 10, 15), reference="expired fixture"
            )
        }
    )
    assert select_price_route(US_MARKET, (plan(),), old).route.provider == "yfinance"


@pytest.mark.parametrize("adjustment", ["raw", "total_return"])
def test_no_approved_capable_source_is_explicitly_unassessable(adjustment):
    decision = select_price_route(US_MARKET, (plan(adjustment=adjustment),))
    assert decision.route is None
    assert decision.reason.startswith("UNASSESSABLE")


def test_raw_short_history_can_use_massive_but_total_return_cannot():
    assert (
        select_price_route(US_MARKET, (plan(adjustment="raw"),), COVERAGE).route.provider
        == "massive"
    )
    assert select_price_route(US_MARKET, (plan(adjustment="total_return"),), COVERAGE).route is None


def test_missing_calendar_history_cannot_be_silently_shortened():
    p = plan()
    p = p.model_copy(update={"periods": p.periods[-1:]})
    assert select_price_route(US_MARKET, (p,), COVERAGE).route is None


def frame_for(plans):
    start = min(p.history_start for p in plans)
    end = max(p.session_date for p in plans)
    sessions = ExchangeCalendarAdapter().schedule(US_MARKET, "XNAS", start, end)
    return pd.DataFrame(
        {
            "Open": 100.0,
            "High": 102.0,
            "Low": 99.0,
            "Close": 101.0,
            "Volume": 1.5,
            "Adj Close": 98.0,
            "Dividends": 0.25,
            "Stock Splits": 0.0,
        },
        index=pd.DatetimeIndex([s.session for s in sessions]).tz_localize("America/New_York"),
    )


class TickerFixture:
    def __init__(self, frame):
        self.frame = frame
        self.calls = []
        self.metadata = {"currency": "USD", "exchangeTimezoneName": "America/New_York"}

    def history(self, **options):
        self.calls.append(options)
        return self.frame

    def get_history_metadata(self):
        return self.metadata


def adapter(ticker, plans):
    return YFinancePriceProvider(plans, ticker_factory=lambda symbol: ticker, clock=lambda: AS_OF)


def test_daily_and_weekly_use_one_frame_with_explicit_options_and_split_basis():
    plans = (plan(), plan("weekly", 2))
    ticker = TickerFixture(frame_for(plans))
    provider = adapter(ticker, plans)
    instrument = smoke_instrument("AAPL", "XNAS")
    responses = [provider.fetch(instrument, US_MARKET, p) for p in plans]
    assert len(ticker.calls) == 1
    assert ticker.calls[0] == {
        "start": plans[1].history_start.isoformat(),
        "end": "2026-10-17",
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
    for response, p in zip(responses, plans, strict=True):
        bars, findings, count = validate_prices(response, p)
        assert count == p.requirement.required_bars
        assert {f.code for f in findings} == {"PUBLICATION_UNVERIFIED"}
        assert all(b.close == 101 for b in bars)  # Never substitute Adj Close.
        payload = json.loads(response.payload)
        assert payload["daily_source"][0]["Dividends"] == "0.25"
        assert payload["request_options"]["repair"] is False
        assert "not total return" in payload["price_basis"]
    weekly = json.loads(responses[1].payload)["bars"]
    assert weekly[-1]["volume"] == "7.5"


def test_missing_constituent_rejects_week_even_when_its_last_session_exists():
    p = plan("weekly", 2)
    frame = frame_for((p,)).drop(frame_for((p,)).index[1])
    response = adapter(TickerFixture(frame), (p,)).fetch(
        smoke_instrument("AAPL", "XNAS"), US_MARKET, p
    )
    bars, findings, count = validate_prices(response, p)
    assert count == len(bars) == 1
    assert {"INSUFFICIENT_HISTORY", "MISSING_SESSION", "STALE_DATA"} <= {f.code for f in findings}
    assert len(json.loads(response.payload)["daily_source"]) == len(frame)


def test_cache_refresh_honors_short_window_and_bypasses_in_memory_frame():
    plans = (plan(), plan("weekly", 2))
    ticker = TickerFixture(frame_for(plans))
    provider = adapter(ticker, plans)
    instrument = smoke_instrument("AAPL", "XNAS")
    provider.fetch(instrument, US_MARKET, plans[0])
    short = plan(n=1)
    ticker.frame = frame_for((short,))
    response = provider.fetch_window(instrument, US_MARKET, short)
    assert len(ticker.calls) == 2
    assert ticker.calls[-1]["start"] == short.history_start.isoformat()
    assert len(json.loads(response.payload)["bars"]) == 1
    provider.fetch_window(instrument, US_MARKET, short)
    assert len(ticker.calls) == 3


def test_insufficient_per_instrument_long_history_is_rejected():
    p = plan("weekly", 260)
    ticker = TickerFixture(frame_for((p,)).iloc[-10:])  # Recent listing, not five years.
    response = adapter(ticker, (p,)).fetch(smoke_instrument("IPO", "XNAS"), US_MARKET, p)
    _, findings, count = validate_prices(response, p)
    assert count <= 2
    assert "INSUFFICIENT_HISTORY" in {f.code for f in findings}


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda t: setattr(t, "frame", t.frame.tz_localize(None)), "INVALID_SOURCE_TIMEZONE"),
        (lambda t: t.metadata.update(currency="INR"), "CURRENCY_MISMATCH"),
        (lambda t: t.metadata.update(exchangeTimezoneName="UTC"), "TIMEZONE_MISMATCH"),
        (
            lambda t: setattr(t, "frame", pd.concat([t.frame, t.frame.iloc[:1]])),
            "DUPLICATE_OR_UNORDERED_SOURCE_SESSION",
        ),
        (
            lambda t: setattr(t, "frame", t.frame.assign(Volume=float("nan"))),
            "INVALID_SOURCE_OHLCV",
        ),
    ],
)
def test_invalid_source_metadata_and_values_block_quality(mutation, code):
    p = plan()
    ticker = TickerFixture(frame_for((p,)))
    mutation(ticker)
    response = adapter(ticker, (p,)).fetch(smoke_instrument("AAPL", "XNAS"), US_MARKET, p)
    _, findings, _ = validate_prices(response, p)
    assert code in {f.code for f in findings}


def test_transport_failure_is_safe_and_never_changes_provider():
    p = plan()
    calls = []

    def failing(symbol):
        calls.append(symbol)
        raise OSError("secret transport details")

    provider = YFinancePriceProvider((p,), ticker_factory=failing)
    with pytest.raises(YFinanceError, match="source remains yfinance") as exc:
        provider.fetch(smoke_instrument("AAPL", "XNAS"), US_MARKET, p)
    assert "secret" not in str(exc.value)
    assert calls == ["AAPL"]


def test_new_provider_instance_refetches_instead_of_reusing_old_snapshots():
    p = plan()
    ticker = TickerFixture(frame_for((p,)))
    instrument = smoke_instrument("AAPL", "XNAS")
    for _ in range(2):
        adapter(ticker, (p,)).fetch(instrument, US_MARKET, p)
    assert len(ticker.calls) == 2


@pytest.mark.parametrize("symbol,venue", [("AAPL", "XNAS"), ("SPY", "ARCX")])
@pytest.mark.parametrize("weekly_bars", [400, 1000])
def test_long_smoke_history_reaches_yfinance_for_both_timeframes(symbol, venue, weekly_bars):
    plans = (plan(venue=venue), plan("weekly", weekly_bars, venue=venue))
    assert plans[1].history_start < date(2020, 1, 1)
    ticker = TickerFixture(frame_for(plans))
    provider = adapter(ticker, plans)
    instrument = smoke_instrument(symbol, venue)
    for p in plans:  # Daily goes first and shares the long weekly request window.
        response = provider.fetch(instrument, US_MARKET, p)
        bars, findings, count = validate_prices(response, p)
        assert count == len(bars) == p.requirement.required_bars
        assert {f.code for f in findings} == {"PUBLICATION_UNVERIFIED"}
    assert len(ticker.calls) == 1
    assert ticker.calls[0]["start"] == plans[1].history_start.isoformat()
