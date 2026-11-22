"""Synthetic risk arithmetic and authenticated observation walkthrough."""

import argparse
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from stock_analyser.atr import ATRProvenance, declare_atr_series, observe_atr
from stock_analyser.breakout import BreakoutPolicy, qualify_breakout
from stock_analyser.breakout_example import (
    EXAMPLE_CUTOFF,
    breakout_example_inputs,
    freeze_breakout_example,
)
from stock_analyser.collection import encode, immutable_write
from stock_analyser.fixtures import FixtureSessionCalendar
from stock_analyser.risk import (
    ExecutionPriceMapping,
    FixedCostAllowances,
    assess_risk,
    replay_risk,
    risk_arithmetic,
)


def example_inputs(directory):
    market, stock, _, calendar, rows = breakout_example_inputs()
    future = calendar.sessions[-1].model_copy(
        update={
            "session": date(2026, 11, 2),
            "opens_at": calendar.sessions[-1].opens_at + timedelta(days=3),
            "closes_at": calendar.sessions[-1].closes_at + timedelta(days=3),
        }
    )
    calendar = FixtureSessionCalendar(market, (*calendar.sessions, future))
    rows[-1].update(high="360", low="358", open="359", close="360")
    snapshot, provenance, _, stamp = freeze_breakout_example(
        directory, market=market, calendar=calendar, rows=rows
    )
    setup = qualify_breakout(
        snapshot,
        provenance,
        calendar,
        BreakoutPolicy(profile="stock"),
        as_of=stamp,
        calculated_at=stamp,
        cutoff_policy=EXAMPLE_CUTOFF,
    )
    series = declare_atr_series(
        stock,
        market,
        market.route("prices").provider,
        market.route("prices").feed,
        calendar,
        setup.observed_session,
        policy_series_id="risk-fixture",
    )
    ap = ATRProvenance(
        **provenance.model_dump(),
        adjustment_revision="split-1",
        adjustment_verification_reference="synthetic://split-1",
    )
    atr = observe_atr(
        snapshot, series, ap, calendar, setup.observed_session, as_of=stamp, calculated_at=stamp
    )
    mapping = ExecutionPriceMapping(
        instrument_id=stock.instrument_id,
        signal_snapshot_sha256=snapshot.sha256,
        adjustment_revision="split-1",
        execution_session=future.session,
        currency="USD",
        execution_basis="synthetic-execution-1",
        factor_numerator=1,
        factor_denominator=1,
        price_increment=Decimal("0.01"),
        verified=True,
        verification_reference="synthetic://mapping-1",
        available_at=stamp,
    )
    costs = FixedCostAllowances(
        currency="USD",
        execution_basis=mapping.execution_basis,
        execution_session=future.session,
        instrument_id=stock.instrument_id,
        winning_exit=Decimal("0.10"),
        losing_exit=Decimal("0.10"),
        assumptions_reference="synthetic://fixed-costs-1",
        available_at=stamp,
    )
    return setup, atr, mapping, costs, calendar


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print("Synthetic research risk levels; recommendation eligibility remains NOT_EVALUATED.")
    for entry in ("101.80", "101.90", "101.900001", "102", "103"):
        checks, values = risk_arithmetic(
            101,
            102,
            2,
            factor=1,
            tick=Decimal("0.01"),
            win_cost=Decimal("0.10"),
            loss_cost=Decimal("0.10"),
            entry=Decimal(entry),
        )
        print(
            f"B=101 C=102 A=2 E={entry}: RR={values['net_reward_risk']}; "
            f"ratio_pass={checks['net_reward_risk_at_least_two']}; "
            f"on_tick={checks['entry_on_tick']}"
        )
    setup, atr, mapping, costs, calendar = example_inputs(args.output / "sources")
    for name in ("pass", "higher-entry", "missing-costs", "unverified-mapping", "off-tick-levels"):
        selected_mapping = mapping
        if name == "unverified-mapping":
            selected_mapping = mapping.model_copy(update={"verified": False})
        elif name == "off-tick-levels":
            selected_mapping = mapping.model_copy(update={"price_increment": Decimal("0.7")})
        result = assess_risk(
            setup,
            atr,
            selected_mapping,
            None if name == "missing-costs" else costs,
            Decimal("360") if name == "higher-entry" else Decimal("359.9"),
            calendar,
            calculated_at=setup.as_of,
        )
        replay_risk(result, calendar, calculated_at=setup.as_of + timedelta(days=1))
        immutable_write(args.output / f"{name}.json", encode(result.model_dump(mode="json")))
        print(
            f"{name}: {result.status}; failures={result.failed_conditions}; "
            f"unavailable={result.unavailable_reasons}"
        )


if __name__ == "__main__":
    main()
