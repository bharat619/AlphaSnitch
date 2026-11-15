"""Synthetic technical setup walkthrough; no recommendation or provider calls."""

import argparse
from datetime import date, timedelta
from pathlib import Path

from stock_analyser.breakout import BreakoutPolicy, qualify_breakout, resolve_spy_binding
from stock_analyser.calendars import CutoffPolicy
from stock_analyser.collection import encode, immutable_write
from stock_analyser.contracts import BenchmarkMapping, CalendarReference, SymbolMapping
from stock_analyser.fixtures import FixtureSessionCalendar, fixture_id
from stock_analyser.indicator_example import example_inputs, freeze_example
from stock_analyser.markets import InstrumentRegistry
from stock_analyser.sma_example import sma_example_inputs

EXAMPLE_CUTOFF = CutoffPolicy(publication_timing="synthetic")


def breakout_example_inputs(count=260):
    market, stock, _, _ = example_inputs()
    feed = market.route("prices").feed.model_copy(update={"venues": ("XNAS", "ARCX")})
    market = market.model_copy(
        update={
            "configuration_version": "synthetic-breakout-1",
            "routes": tuple(r.model_copy(update={"feed": feed}) for r in market.routes),
            "calendars": (
                *market.calendars,
                CalendarReference(venue="ARCX", reference="fixture-us", version="synthetic-1"),
            ),
            "benchmarks": (
                BenchmarkMapping(key="broad_market", symbol="SPY", venue="ARCX", currency="USD"),
            ),
        }
    )
    spy = stock.model_copy(
        update={
            "instrument_id": fixture_id("breakout:SPY:listing"),
            "security_id": fixture_id("breakout:SPY:security"),
            "symbol": "SPY",
            "venue": "ARCX",
            "provider_symbols": (
                SymbolMapping(
                    provider=market.route("prices").provider,
                    symbol="SPY.US",
                    effective_from=date(2020, 1, 1),
                ),
            ),
        }
    )
    original, rows = sma_example_inputs(count)
    sessions = tuple(
        s.model_copy(update={"opens_at": s.opens_at - timedelta(minutes=30)})
        for s in original.sessions
    )
    calendar = FixtureSessionCalendar(market, sessions)
    # At count=260, prior high=359 and current close=360; RVOL is exactly 1.5.
    rows[-1].update(
        open=str(100 + count), high=str(101 + count), close=str(100 + count), volume="1500000"
    )
    return market, stock, spy, calendar, rows


def freeze_breakout_example(
    directory, *, rows=None, calendar=None, market=None, instrument=None, count=260, **kwargs
):
    default_market, stock, _, default_calendar, default_rows = breakout_example_inputs(count)
    return freeze_example(
        directory,
        rows=default_rows if rows is None else rows,
        calendar=default_calendar if calendar is None else calendar,
        minimum_bars=200,
        market_instrument=(market or default_market, instrument or stock),
        **kwargs,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(
        "Synthetic technical breakout qualification; recommendation eligibility is NOT EVALUATED."
    )
    print(
        "Daily gates: prior-high breakout + stock trend + RVOL >= 1.5; "
        "weekly context is informational."
    )
    for name in ("pass", "equal-high", "low-volume", "missing-volume", "spy-opposes"):
        market, stock, spy, calendar, rows = breakout_example_inputs()
        if name == "equal-high":
            rows[-1]["close"] = rows[-2]["high"]
        elif name == "low-volume":
            rows[-1]["volume"] = "1499600"
        elif name == "missing-volume":
            rows[-1].pop("volume")
        snapshot, provenance, _, stamp = freeze_breakout_example(
            args.output / name,
            rows=rows,
            calendar=calendar,
            market=market,
        )
        kwargs = {}
        if name == "spy-opposes":
            for row in rows:
                row.update(open="100", high="101", low="99", close="100", volume="1")
            benchmark, bp, _, _ = freeze_breakout_example(
                args.output / "spy", rows=rows, calendar=calendar, market=market, instrument=spy
            )
            kwargs = dict(
                benchmark_binding=resolve_spy_binding(
                    market,
                    InstrumentRegistry((stock, spy)),
                    verification_reference="synthetic://SPY-registry",
                ),
                benchmark_snapshot=benchmark,
                benchmark_provenance=bp,
            )
        result = qualify_breakout(
            snapshot,
            provenance,
            calendar,
            BreakoutPolicy(profile="stock_with_spy" if kwargs else "stock"),
            as_of=stamp,
            calculated_at=stamp,
            cutoff_policy=EXAMPLE_CUTOFF,
            **kwargs,
        )
        print(
            f"{name}: {result.status}; t={result.observed_session}; "
            f"groups={[(g.group, g.status) for g in result.gates]}; "
            f"weekly={result.weekly_context.classification}; "
            f"recommendation={result.recommendation_status}"
        )
        if result.exact_prices:
            print(
                f"  close={result.exact_prices.signal_close}; "
                f"B={result.exact_prices.breakout_level}"
            )
        immutable_write(args.output / f"{name}.json", encode(result.model_dump(mode="json")))


if __name__ == "__main__":
    main()
