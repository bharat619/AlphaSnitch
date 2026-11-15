"""Synthetic daily trend walkthrough over frozen evidence; no recommendation or live calls."""

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from stock_analyser.calendars import CalendarSession
from stock_analyser.collection import encode, immutable_write
from stock_analyser.fixtures import FixtureSessionCalendar
from stock_analyser.indicator_example import example_inputs, freeze_example
from stock_analyser.indicators import observe_daily_trend


def sma_example_inputs(count=200):
    market, _, _, _ = example_inputs()
    day = date(2026, 10, 30)
    dates = []
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day)
        day -= timedelta(days=1)
    # Weekdays here are enumerated synthetic sessions, not a US holiday calendar.
    sessions = tuple(
        CalendarSession(
            session=d,
            opens_at=datetime(d.year, d.month, d.day, 14, tzinfo=UTC),
            closes_at=datetime(d.year, d.month, d.day, 20, tzinfo=UTC),
        )
        for d in reversed(dates)
    )
    rows = [
        dict(
            session=s.session.isoformat(),
            timestamp=s.closes_at.isoformat(),
            open=str(100 + i),
            high=str(101 + i),
            low=str(99 + i),
            close=str(100 + i),
            volume="1000000",
        )
        for i, s in enumerate(sessions)
    ]
    return FixtureSessionCalendar(market, sessions), rows


def freeze_sma_example(directory, *, rows=None, calendar=None, **kwargs):
    default_calendar, default_rows = sma_example_inputs()
    return freeze_example(
        directory,
        rows=default_rows if rows is None else rows,
        calendar=default_calendar if calendar is None else calendar,
        minimum_bars=200,
        **kwargs,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    print("Synthetic daily SMA observations; no BUY/SELL recommendation or live qualification.")
    print("200 enumerated sessions; current and lagged windows use exact closes.")
    for scenario in ("rising", "flat", "missing-old-close"):
        calendar, rows = sma_example_inputs()
        if scenario == "flat":
            for row in rows:
                row.update(open="100", high="101", low="99", close="100")
        elif scenario == "missing-old-close":
            rows[0].pop("close")
        snapshot, provenance, calendar, stamp = freeze_sma_example(
            args.output / scenario,
            calendar=calendar,
            rows=rows,
        )
        trend = observe_daily_trend(
            snapshot,
            provenance,
            calendar,
            stamp.date(),
            as_of=stamp,
            calculated_at=stamp,
        )
        print(f"\n{scenario}")
        for observation in trend.observations:
            spec = observation.specification
            print(
                f"SMA{spec.period}[t-{spec.session_offset}]: "
                f"{observation.status}, value={observation.value}, reasons={observation.reasons}"
            )
        print(json.dumps(trend.comparisons(), sort_keys=True))
        print(f"daily trend confirmed: {trend.confirmed}")
        payload = {
            "observation": trend.model_dump(mode="json"),
            "comparisons": trend.comparisons(),
            "confirmed": trend.confirmed,
        }
        immutable_write(args.output / f"{scenario}-observations.json", encode(payload))


if __name__ == "__main__":
    main()
