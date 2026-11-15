"""Reproducible synthetic weekly context; no trading recommendation or provider calls."""

import argparse
from datetime import timedelta
from pathlib import Path

from stock_analyser.calendars import CutoffPolicy
from stock_analyser.collection import encode, immutable_write
from stock_analyser.indicator_example import freeze_example
from stock_analyser.sma_example import sma_example_inputs
from stock_analyser.weekly_context import observe_weekly_context


def weekly_example_inputs(closes=None):
    closes = list(range(100, 134)) if closes is None else closes
    calendar, rows = sma_example_inputs(5 * len(closes))
    for i, row in enumerate(rows):
        close = str(closes[i // 5])
        row.update(open=close, high=close, low=close, close=close)
    return calendar, rows


def freeze_weekly_example(directory, *, closes=None, rows=None, calendar=None, **kwargs):
    default_calendar, default_rows = weekly_example_inputs(closes)
    return freeze_example(
        directory,
        rows=default_rows if rows is None else rows,
        calendar=default_calendar if calendar is None else calendar,
        minimum_bars=len(default_rows if rows is None else rows),
        **kwargs,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    scenarios = {
        "supports": list(range(100, 134)),
        "opposes": list(range(133, 99, -1)),
        "flat": [100] * 34,
        "mixed": [*range(100, 133), 110],
        "missing-constituent": list(range(100, 134)),
    }
    print("Synthetic weekly context: informational only; daily qualification is unchanged.")
    print("34 completed synthetic weekday weeks; calendar grace does not prove publication.")
    for name, closes in scenarios.items():
        calendar, rows = weekly_example_inputs(closes)
        if name == "missing-constituent":
            rows.pop(-3)
        snapshot, provenance, calendar, stamp = freeze_weekly_example(
            args.output / name,
            calendar=calendar,
            rows=rows,
        )
        result = observe_weekly_context(
            snapshot,
            provenance,
            calendar,
            as_of=stamp,
            calculated_at=stamp,
            cutoff_policy=CutoffPolicy(publication_timing="synthetic"),
        )
        latest = result.resolution.session_date if result.resolution else None
        print(
            f"{name}: {result.classification}; daily={result.daily_session}; week={latest}; "
            f"SMA30={result.sma30}; SMA30[w-4]={result.sma30_four_weeks_earlier}; "
            f"reasons={result.reasons}"
        )
        replay = observe_weekly_context(
            snapshot,
            provenance,
            calendar,
            as_of=stamp,
            calculated_at=stamp + timedelta(seconds=1),
            cutoff_policy=CutoffPolicy(publication_timing="synthetic"),
        )
        assert replay.semantic_bytes() == result.semantic_bytes()
        immutable_write(
            args.output / f"{name}-observations.json", encode(result.model_dump(mode="json"))
        )


if __name__ == "__main__":
    main()
