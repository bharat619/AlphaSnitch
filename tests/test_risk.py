import os
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_analyser.atr import ATRProvenance, observe_atr
from stock_analyser.breakout import qualify_breakout
from stock_analyser.breakout_example import breakout_example_inputs, freeze_breakout_example
from stock_analyser.calendars import ExchangeCalendarAdapter
from stock_analyser.fixtures import FixtureSessionCalendar
from stock_analyser.risk import RiskAssessment, assess_risk, replay_risk, risk_arithmetic
from stock_analyser.risk_example import example_inputs


@pytest.mark.parametrize(
    "entry,passes",
    [("101.80", True), ("101.90", True), ("101.900001", False), ("102", False), ("103", False)],
)
def test_accepted_cost_aware_example(entry, passes):
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
    assert values["signal_stop"] == "99" and values["signal_target"] == "108"
    assert values["maximum_entry_exact"] == "1019/10"
    assert checks["net_reward_risk_at_least_two"] == passes


def test_exact_arithmetic_ignores_ambient_decimal_rounding():
    kwargs = dict(
        factor=Fraction(2, 3),
        tick=Decimal("0.01"),
        win_cost=Decimal("0.07"),
        loss_cost=Decimal("0.11"),
        entry=Decimal("67.8"),
    )
    expected = risk_arithmetic(101, 102, 2, **kwargs)
    with localcontext() as ctx:
        ctx.prec = 2
        ctx.traps[Inexact] = True
        assert risk_arithmetic(101, 102, 2, **kwargs) == expected


def test_strict_lower_bound_and_cap_floor():
    checks, values = risk_arithmetic(
        101, 102, 2, factor=1, tick=Decimal("0.03"), win_cost=0, loss_cost=Decimal("0.1"), entry=101
    )
    assert checks["entry_above_breakout"] is False
    assert Fraction(values["maximum_entry_tick"]) == Fraction("101.91")
    assert Fraction(values["maximum_entry_tick"]) <= Fraction(values["maximum_entry_exact"])
    assert Fraction(values["minimum_entry_tick"]) == Fraction("101.01")


@pytest.mark.parametrize("atr", [0, 101, 102])
def test_invalid_risk_levels_retain_failures(atr):
    checks, _ = risk_arithmetic(101, 102, atr, factor=1, tick=1, win_cost=0, loss_cost=0, entry=102)
    assert checks["atr_positive"] == (atr > 0)
    assert checks["stop_positive"] == (atr < 101)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"factor": 0},
        {"factor": 1, "tick": 0},
        {"factor": 1, "win_cost": -1, "loss_cost": 0},
        {"factor": 0.5},
    ],
)
def test_invalid_arithmetic_inputs_rejected(kwargs):
    with pytest.raises(ValueError):
        risk_arithmetic(101, 102, 2, **kwargs)


def test_empty_range_and_missing_inputs_do_not_hide_known_failure():
    checks, _ = risk_arithmetic(101, 102, 2, factor=1, tick=1, win_cost=10, loss_cost=10, entry=101)
    assert checks["entry_range_nonempty"] is False
    checks, _ = risk_arithmetic(101, 102, None, factor=1, tick=1, entry=101)
    assert checks["entry_above_breakout"] is False
    assert checks["net_reward_risk_at_least_two"] is None


@pytest.mark.parametrize("close,tick,nonempty", [(363, 9, False), (374, 10, False), (363, 1, None)])
def test_range_can_be_provably_empty_without_costs(close, tick, nonempty):
    checks, values = risk_arithmetic(362, close, 2, factor=1, tick=tick)
    assert checks["levels_on_tick"] is True
    assert checks["entry_range_nonempty"] is nonempty
    assert "maximum_entry_tick" not in values


@pytest.mark.parametrize(
    "kwargs",
    [
        {"entry": "1e-5000"},
        {"tick": Decimal("1e-5000")},
        {"factor": Fraction(1, 10**400)},
        {"win_cost": Decimal("1e5000"), "loss_cost": 0},
    ],
)
def test_arithmetic_rejects_oversized_exact_inputs(kwargs):
    with pytest.raises(ValueError, match="risk numeric limits"):
        risk_arithmetic(101, 102, 2, **({"factor": 1} | kwargs))


@pytest.fixture(scope="module")
def frozen(tmp_path_factory):
    return example_inputs(tmp_path_factory.mktemp("risk-source"))


def calculate(frozen, **changes):
    setup, atr, mapping, costs, calendar = frozen
    kwargs = dict(
        setup=setup,
        atr=atr,
        mapping=mapping,
        costs=costs,
        proposed_entry=Decimal("359.9"),
        calendar=calendar,
        calculated_at=setup.as_of,
    )
    kwargs.update(changes)
    return assess_risk(**kwargs)


def test_authenticated_assessment_and_replay(frozen):
    result = calculate(frozen)
    assert result.status == "PASS"
    assert result.atr.strategy_ready and result.atr.value == 2
    assert result.exact_values["signal_stop"] == "357"
    assert result.exact_values["signal_target"] == "366"
    assert result.exact_values["net_reward_risk"] == "2"
    assert result.recommendation_status == "NOT_EVALUATED"
    assert RiskAssessment.model_validate_json(result.model_dump_json()) == result
    replay = replay_risk(result, frozen[-1], calculated_at=result.calculated_at + timedelta(days=1))
    assert replay.semantic_bytes() == result.semantic_bytes()


@pytest.mark.parametrize("missing", ["atr", "mapping", "costs", "proposed_entry"])
def test_required_inputs_unavailable(frozen, missing):
    assert calculate(frozen, **{missing: None}).status == "UNAVAILABLE"


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"verified": False}, "PRICE_MAPPING_UNVERIFIED"),
        ({"verification_reference": None}, "PRICE_MAPPING_UNVERIFIED"),
        ({"adjustment_revision": "other"}, "PRICE_MAPPING_IDENTITY_MISMATCH"),
        ({"currency": "EUR"}, "PRICE_MAPPING_IDENTITY_MISMATCH"),
        ({"instrument_id": uuid4()}, "PRICE_MAPPING_IDENTITY_MISMATCH"),
        ({"signal_snapshot_sha256": "0" * 64}, "PRICE_MAPPING_IDENTITY_MISMATCH"),
        ({"execution_session": date(2026, 11, 3)}, "PRICE_MAPPING_IDENTITY_MISMATCH"),
    ],
)
def test_mapping_must_match_frozen_evidence(frozen, change, reason):
    result = calculate(frozen, mapping=frozen[2].model_copy(update=change))
    assert result.status == "UNAVAILABLE"
    assert reason in result.unavailable_reasons


@pytest.mark.parametrize(
    "change",
    [
        {"currency": "EUR"},
        {"execution_basis": "other"},
        {"execution_session": date(2026, 11, 3)},
        {"instrument_id": uuid4()},
    ],
)
def test_cost_identity_must_match_mapping(frozen, change):
    result = calculate(frozen, costs=frozen[3].model_copy(update=change))
    assert result.status == "UNAVAILABLE"
    assert "COST_ALLOWANCES_IDENTITY_MISMATCH" in result.unavailable_reasons


def test_future_mapping_and_costs_do_not_supply_comparisons(frozen):
    for field, index in (("mapping", 2), ("costs", 3)):
        result = calculate(
            frozen,
            **{
                field: frozen[index].model_copy(
                    update={"available_at": frozen[0].as_of + timedelta(seconds=1)}
                )
            },
        )
        assert result.status == "UNAVAILABLE"
        assert result.comparisons["net_reward_risk_at_least_two"] is None


def test_costs_are_execution_per_share_not_rescaled_signal_costs(frozen):
    mapping = frozen[2].model_copy(update={"factor_denominator": 2})
    result = calculate(frozen, mapping=mapping, proposed_entry=Decimal("179.9"))
    assert result.exact_values["execution_stop"] == "357/2"
    assert result.exact_values["execution_target"] == "183"
    assert result.exact_values["planned_loss"] == "3/2"
    assert result.exact_values["planned_reward"] == "3"
    assert result.status == "PASS"


def test_off_tick_levels_are_not_silently_rounded(frozen):
    result = calculate(
        frozen, mapping=frozen[2].model_copy(update={"price_increment": Decimal("0.7")})
    )
    assert result.status == "UNAVAILABLE"
    assert "EXECUTABLE_LEVEL_ROUNDING_UNRESOLVED" in result.unavailable_reasons
    assert result.exact_values["execution_stop"] == "357"


@pytest.mark.parametrize("missing", ["costs", "atr"])
def test_failures_survive_missing_inputs(frozen, missing):
    result = calculate(frozen, **{missing: None}, proposed_entry=Decimal("359"))
    assert result.status == "UNAVAILABLE"
    assert "entry_above_breakout" in result.failed_conditions


def test_tampering_and_json_key_order(frozen):
    result = calculate(frozen)
    payload = result.model_dump(mode="json")
    payload["comparisons"] = dict(reversed(tuple(payload["comparisons"].items())))
    assert RiskAssessment.model_validate(payload) == result
    payload["exact_values"]["signal_target"] = "999"
    with pytest.raises(ValidationError, match="retained inputs"):
        RiskAssessment.model_validate(payload)


def test_atr_cannot_be_fabricated_to_improve_ratio(frozen):
    with pytest.raises(ValueError, match="authenticated"):
        calculate(frozen, atr=frozen[1].model_copy(update={"value": Decimal("5")}))


def test_unavailable_atr_retains_checkpoint_diagnostics(tmp_path, frozen):
    original, ready_atr, _, _, calendar = frozen
    _, _, _, _, rows = breakout_example_inputs()
    snapshot, provenance, _, stamp = freeze_breakout_example(
        tmp_path, market=calendar.market, calendar=calendar, rows=rows[1:]
    )
    setup = qualify_breakout(
        snapshot,
        provenance,
        calendar,
        original.policy,
        as_of=stamp,
        calculated_at=stamp,
        cutoff_policy=original.cutoff_policy,
    )
    atr_provenance = ATRProvenance(
        **provenance.model_dump(),
        adjustment_revision=ready_atr.provenance.adjustment_revision,
        adjustment_verification_reference=ready_atr.provenance.adjustment_verification_reference,
    )
    unavailable = observe_atr(
        snapshot,
        ready_atr.series,
        atr_provenance,
        calendar,
        setup.observed_session,
        as_of=stamp,
        calculated_at=stamp,
        checkpoint_id="0" * 64,
    )
    assert setup.status == "PASS"
    assert unavailable.status == "UNAVAILABLE"
    assert unavailable.checkpoint_rejections == ("CHECKPOINT_NOT_FOUND",)
    result = calculate(frozen, setup=setup, atr=unavailable, mapping=None, costs=None)
    assert result.status == "UNAVAILABLE"
    assert "MISSING_SESSION" in result.unavailable_reasons
    assert result.atr == unavailable
    assert replay_risk(result, calendar, calculated_at=stamp) == result

    fabricated = unavailable.model_copy(update={"reasons": ("DIFFERENT_BLOCKER",)})
    with pytest.raises(ValueError, match="Unavailable ATR differs from authenticated replay"):
        calculate(frozen, setup=setup, atr=fabricated, mapping=None, costs=None)


def test_unsupported_calendar_preserves_signal_blockers(frozen):
    original = frozen[0]
    calendar = ExchangeCalendarAdapter()
    setup = qualify_breakout(
        original.stock_snapshot,
        original.stock_provenance,
        calendar,
        original.policy,
        as_of=original.as_of,
        calculated_at=original.calculated_at,
        cutoff_policy=original.cutoff_policy,
    )
    assert setup.status == "UNAVAILABLE"
    result = calculate(frozen, setup=setup, atr=None, calendar=calendar)
    assert result.status == "UNAVAILABLE"
    assert result.entry_session is result.calendar_data_version is None
    assert "UNSUPPORTED_CALENDAR" in result.unavailable_reasons
    assert "EXECUTION_SESSION_UNAVAILABLE" in result.unavailable_reasons
    assert replay_risk(result, calendar, calculated_at=setup.as_of) == result


def test_oversized_proposed_entry_rejected_before_arithmetic(frozen):
    with pytest.raises(ValueError, match="risk numeric limits"):
        calculate(frozen, proposed_entry=Decimal("1e-5000"))


def test_missing_execution_calendar_blocks(frozen):
    old = frozen[-1]
    calendar = FixtureSessionCalendar(old.market, old.sessions[:-1])
    result = calculate(frozen, calendar=calendar)
    assert result.status == "UNAVAILABLE"
    assert "EXECUTION_SESSION_UNAVAILABLE" in result.unavailable_reasons
    with pytest.raises(ValueError, match="authenticated replay"):
        replay_risk(calculate(frozen), calendar, calculated_at=frozen[0].as_of)


def test_source_corruption_blocks_authentication(tmp_path):
    inputs = example_inputs(tmp_path)
    ref = inputs[0].stock_snapshot.metadata.sources[0]
    path = Path(ref.reference)
    path.chmod(0o644)
    path.write_bytes(b"{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        calculate(inputs)


def test_walkthrough_reproduces_in_fresh_process(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    cmd = [sys.executable, "-m", "stock_analyser.risk_example", "--output", str(tmp_path)]
    first = subprocess.run(cmd, env=env, text=True, capture_output=True, check=True)
    original = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    second = subprocess.run(cmd, env=env, text=True, capture_output=True, check=True)
    assert second.stdout == first.stdout
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == original
    assert "pass: PASS" in first.stdout and "higher-entry: FAIL" in first.stdout
