"""Synthetic fixed-anchor ATR walkthrough; no recommendations or provider calls."""

import argparse
from datetime import timedelta
from pathlib import Path

from stock_analyser.atr import (
    ATRCheckpoint,
    ATRProvenance,
    declare_atr_series,
    observe_atr,
)
from stock_analyser.collection import encode, immutable_write
from stock_analyser.indicator_example import example_inputs, freeze_example
from stock_analyser.sma_example import sma_example_inputs


def atr_example_inputs(count=261):
    calendar, rows = sma_example_inputs(count)
    for row in rows:
        row.update(open="100", high="101", low="99", close="100")
    return calendar, rows


def example_series(calendar):
    market, instrument, _, _ = example_inputs()
    return declare_atr_series(
        instrument,
        market,
        market.route("prices").provider,
        market.route("prices").feed,
        calendar,
        calendar.sessions[259].session,
        policy_series_id="synthetic-breakout-1",
    )


def freeze_atr_example(directory, *, calendar, rows, day, revision="synthetic-split-1", **kwargs):
    stamp = next(s.closes_at for s in calendar.sessions if s.session == day) + timedelta(hours=1)
    snapshot, provenance, _, _ = freeze_example(
        directory,
        calendar=calendar,
        rows=rows,
        minimum_bars=260,
        as_of=stamp,
        **kwargs,
    )
    provenance = ATRProvenance(
        **provenance.model_dump(),
        adjustment_revision=revision,
        adjustment_verification_reference="synthetic://adjustment-audit-1",
    )
    return snapshot, provenance, calendar, stamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    calendar, rows = atr_example_inputs()
    series = example_series(calendar)
    print("Synthetic ATR14 observations; no trading recommendation or live qualification.")
    print(f"Declared t0={series.first_assessment_session}; fixed anchor={series.anchor_session}")
    checkpoint = None
    for count in (14, 15, 259, 260, 261):
        if count == 261:
            rows[-1].update(open="102", high="104", low="102", close="103")
        day = calendar.sessions[count - 1].session
        snapshot, provenance, _, stamp = freeze_atr_example(
            args.output / str(count),
            calendar=calendar,
            rows=rows[:count],
            day=day,
        )
        result = observe_atr(
            snapshot,
            series,
            provenance,
            calendar,
            day,
            as_of=stamp,
            calculated_at=stamp,
            checkpoint=checkpoint if count == 261 else None,
        )
        print(
            f"bar {count}: {result.status}; ATR={result.value}; updates={result.update_count}; "
            f"ready={result.strategy_ready}; mode={result.calculation_mode}; "
            f"reasons={result.readiness_reasons}"
        )
        immutable_write(args.output / f"bar-{count}.json", encode(result.model_dump(mode="json")))
        if count == 260:
            checkpoint = ATRCheckpoint(observation=result, created_at=stamp)
            immutable_write(
                args.output / "checkpoint.json", encode(checkpoint.model_dump(mode="json"))
            )
    rows[20]["high"] = "103"
    snapshot, provenance, _, stamp = freeze_atr_example(
        args.output / "correction",
        calendar=calendar,
        rows=rows,
        day=day,
        revision="synthetic-split-2",
    )
    corrected = observe_atr(
        snapshot,
        series,
        provenance,
        calendar,
        day,
        as_of=stamp,
        calculated_at=stamp,
        checkpoint=checkpoint,
    )
    print(
        f"corrected revision: mode={corrected.calculation_mode}; ATR={corrected.value}; "
        f"checkpoint reasons={corrected.checkpoint_rejections}"
    )
    immutable_write(args.output / "corrected.json", encode(corrected.model_dump(mode="json")))


if __name__ == "__main__":
    main()
