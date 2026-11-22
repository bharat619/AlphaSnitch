"""Exact research risk levels over authenticated breakout and ATR evidence."""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import AfterValidator, AwareDatetime, Field, TypeAdapter, model_validator

from stock_analyser.atr import ATRCheckpoint, ATRObservation, authenticate_checkpoint, observe_atr
from stock_analyser.breakout import BreakoutAssessment, replay_breakout
from stock_analyser.cache import digest
from stock_analyser.calendars import CalendarSession, SessionCalendar
from stock_analyser.collection import encode
from stock_analyser.contracts import Contract, Currency, Text

_MAX_COEFFICIENT_DIGITS = 200
_MAX_ABS_EXPONENT = 200
_MAX_RATIONAL_DIGITS = _MAX_COEFFICIENT_DIGITS + _MAX_ABS_EXPONENT
_MAX_RATIONAL_COMPONENT = 10**_MAX_RATIONAL_DIGITS - 1
_NUMERIC_LIMIT_ERROR = "Input exceeds risk numeric limits"


def _bounded_decimal(value: Decimal) -> Decimal:
    if not value.is_finite():
        raise ValueError("Risk inputs must be finite")
    parts = value.as_tuple()
    if len(parts.digits) > _MAX_COEFFICIENT_DIGITS or abs(parts.exponent) > _MAX_ABS_EXPONENT:
        raise ValueError(_NUMERIC_LIMIT_ERROR)
    return value


Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False), AfterValidator(_bounded_decimal)]
Cost = Annotated[Decimal, Field(ge=0, allow_inf_nan=False), AfterValidator(_bounded_decimal)]
FactorComponent = Annotated[int, Field(strict=True, gt=0, le=_MAX_RATIONAL_COMPONENT)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
CHECKS = (
    "atr_positive",
    "stop_positive",
    "levels_on_tick",
    "entry_range_nonempty",
    "entry_above_breakout",
    "entry_above_stop",
    "entry_below_target",
    "entry_on_tick",
    "planned_loss_positive",
    "net_reward_risk_at_least_two",
)


class RiskPolicy(Contract):
    policy_version: Literal["breakout-risk-1"] = "breakout-risk-1"
    stop_atr_multiple: Literal[1] = 1
    target_atr_multiple: Literal[3] = 3
    minimum_net_reward_risk: Literal[2] = 2
    entry_cap_rounding: Literal["floor_to_tick"] = "floor_to_tick"
    level_rounding: Literal["require_aligned"] = "require_aligned"
    scope: Literal["research_risk_only"] = "research_risk_only"


class ExecutionPriceMapping(Contract):
    """Explicit multiplicative split-price conversion, including identity mappings."""

    instrument_id: UUID
    signal_snapshot_sha256: Hash
    adjustment_revision: Text
    execution_session: date
    currency: Currency
    execution_basis: Text
    factor_numerator: FactorComponent
    factor_denominator: FactorComponent
    price_increment: Price
    verified: bool
    verification_reference: Text | None
    available_at: AwareDatetime

    @property
    def factor(self):
        return Fraction(self.factor_numerator, self.factor_denominator)


class FixedCostAllowances(Contract):
    """Explicit total round-trip allowances in execution-currency units per share."""

    model_version: Literal["fixed-per-share-round-trip-1"] = "fixed-per-share-round-trip-1"
    currency: Currency
    execution_basis: Text
    execution_session: date
    instrument_id: UUID
    winning_exit: Cost
    losing_exit: Cost
    assumptions_reference: Text
    available_at: AwareDatetime


def _fraction(value) -> Fraction:
    if isinstance(value, (float, bool)):
        raise ValueError("Risk inputs must use exact decimal or rational values")
    if isinstance(value, Decimal):
        _bounded_decimal(value)
    elif isinstance(value, str):
        if "/" in value:
            if len(value.strip()) > 4 * _MAX_RATIONAL_DIGITS + 3:
                raise ValueError(_NUMERIC_LIMIT_ERROR)
        else:
            try:
                _bounded_decimal(Decimal(value))
            except InvalidOperation as exc:
                raise ValueError("Risk inputs must use exact decimal or rational values") from exc
    fraction = Fraction(value)
    if max(abs(fraction.numerator), fraction.denominator) > _MAX_RATIONAL_COMPONENT:
        raise ValueError(_NUMERIC_LIMIT_ERROR)
    return fraction


def risk_arithmetic(
    breakout, close, atr, *, factor=None, tick=None, win_cost=None, loss_cost=None, entry=None
):
    """Exact arithmetic only; callers must separately validate evidence and readiness."""
    checks, values = dict.fromkeys(CHECKS), {}
    b, c, a = (_fraction(v) if v is not None else None for v in (breakout, close, atr))
    if any(v is not None and v <= 0 for v in (b, c)) or (a is not None and a < 0):
        raise ValueError("Signal prices must be positive and ATR nonnegative")
    if a is not None:
        checks["atr_positive"] = a > 0
    s = b - a if b is not None and a is not None else None
    t = c + 3 * a if c is not None and a is not None else None
    values.update(signal_breakout=b, signal_close=c, signal_atr=a, signal_stop=s, signal_target=t)
    if s is not None:
        checks["stop_positive"] = s > 0
    if factor is not None:
        factor = _fraction(factor)
        if factor <= 0:
            raise ValueError("Price conversion factor must be positive")
        b, s, t = (v * factor if v is not None else None for v in (b, s, t))
        values.update(execution_breakout=b, execution_stop=s, execution_target=t)
        e = _fraction(entry) if entry is not None else None
        if e is not None:
            if e <= 0:
                raise ValueError("Entry must be positive")
            if b is not None:
                checks["entry_above_breakout"] = e > b
            if s is not None:
                checks["entry_above_stop"] = e > s
            if t is not None:
                checks["entry_below_target"] = e < t
        first_tick = None
        if tick is not None:
            tick = _fraction(tick)
            if tick <= 0:
                raise ValueError("Price increment must be positive")
            if b is not None:
                first_tick = (b // tick + 1) * tick
            if s is not None and t is not None:
                checks["levels_on_tick"] = (s / tick).denominator == (t / tick).denominator == 1
                # Nonnegative costs can only lower the entry cap.
                if first_tick is not None and (
                    not s < first_tick < t or 3 * first_tick > t + 2 * s
                ):
                    checks["entry_range_nonempty"] = False
            if e is not None:
                checks["entry_on_tick"] = (e / tick).denominator == 1
        if win_cost is not None and loss_cost is not None and s is not None and t is not None:
            cw, cl = _fraction(win_cost), _fraction(loss_cost)
            if cw < 0 or cl < 0:
                raise ValueError("Cost allowances must be nonnegative")
            cap = (t + 2 * s - cw - 2 * cl) / 3
            values["maximum_entry_exact"] = cap
            if first_tick is not None:
                cap_tick = (cap // tick) * tick
                values.update(maximum_entry_tick=cap_tick, minimum_entry_tick=first_tick)
                checks["entry_range_nonempty"] = (
                    checks["entry_range_nonempty"] is not False and first_tick <= cap_tick
                )
            if e is not None:
                reward, loss = t - e - cw, e - s + cl
                values.update(planned_reward=reward, planned_loss=loss)
                checks["planned_loss_positive"] = loss > 0
                checks["net_reward_risk_at_least_two"] = loss > 0 and reward >= 2 * loss
                values["net_reward_risk"] = reward / loss if loss > 0 else None
    return checks, {key: str(value) if value is not None else None for key, value in values.items()}


def _evaluate(setup, atr, mapping, costs, entry, entry_session):
    reasons = list(setup.context_reasons)
    prices = setup.exact_prices
    if prices is None:
        reasons.append("SIGNAL_PRICES_UNAVAILABLE")
    a = None
    if atr is None:
        reasons.append("ATR_UNAVAILABLE")
    elif (
        atr.snapshot != setup.stock_snapshot
        or atr.observed_session != setup.observed_session
        or atr.as_of != setup.as_of
        or atr.provenance.model_dump(
            exclude={"adjustment_revision", "adjustment_verification_reference"}
        )
        != setup.stock_provenance.model_dump()
    ):
        reasons.append("ATR_SIGNAL_IDENTITY_MISMATCH")
    elif not atr.strategy_ready:
        reasons.extend(("ATR_NOT_STRATEGY_READY", *atr.readiness_reasons))
    else:
        a = atr.value
    if entry_session is None:
        reasons.append("EXECUTION_SESSION_UNAVAILABLE")
    elif setup.as_of >= entry_session.opens_at:
        reasons.append("EXECUTION_SESSION_ALREADY_OPEN")
    valid_mapping = False
    if mapping is None:
        reasons.append("PRICE_MAPPING_UNAVAILABLE")
    elif not mapping.verified or not mapping.verification_reference:
        reasons.append("PRICE_MAPPING_UNVERIFIED")
    elif mapping.available_at > setup.as_of:
        reasons.append("PRICE_MAPPING_NOT_YET_AVAILABLE")
    elif (
        mapping.instrument_id != setup.stock_snapshot.metadata.instrument.instrument_id
        or mapping.signal_snapshot_sha256 != setup.stock_snapshot.sha256
        or mapping.currency != setup.stock_snapshot.metadata.currency
        or entry_session is None
        or mapping.execution_session != entry_session.session
        or (atr is not None and mapping.adjustment_revision != atr.provenance.adjustment_revision)
    ):
        reasons.append("PRICE_MAPPING_IDENTITY_MISMATCH")
    else:
        valid_mapping = True
    valid_costs = False
    if costs is None:
        reasons.append("COST_ALLOWANCES_UNAVAILABLE")
    elif costs.available_at > setup.as_of:
        reasons.append("COST_ALLOWANCES_NOT_YET_AVAILABLE")
    elif not valid_mapping or (
        costs.currency != mapping.currency
        or costs.execution_basis != mapping.execution_basis
        or costs.execution_session != mapping.execution_session
        or costs.instrument_id != mapping.instrument_id
    ):
        reasons.append("COST_ALLOWANCES_IDENTITY_MISMATCH")
    else:
        valid_costs = True
    if entry is None:
        reasons.append("ENTRY_PRICE_UNAVAILABLE")
    checks, values = risk_arithmetic(
        prices.breakout_level if prices else None,
        prices.signal_close if prices else None,
        a,
        factor=mapping.factor if valid_mapping else None,
        tick=mapping.price_increment if valid_mapping else None,
        win_cost=costs.winning_exit if valid_costs else None,
        loss_cost=costs.losing_exit if valid_costs else None,
        entry=entry,
    )
    if checks["levels_on_tick"] is False:
        reasons.append("EXECUTABLE_LEVEL_ROUNDING_UNRESOLVED")
    failed = tuple(k for k in CHECKS if checks[k] is False)
    status = "UNAVAILABLE" if reasons or None in checks.values() else "FAIL" if failed else "PASS"
    return dict(
        status=status,
        comparisons=checks,
        exact_values=values,
        failed_conditions=failed,
        unavailable_reasons=tuple(dict.fromkeys(reasons)),
    )


class RiskAssessment(Contract):
    policy: RiskPolicy
    policy_sha256: Hash
    setup: BreakoutAssessment
    atr: ATRObservation | None
    mapping: ExecutionPriceMapping | None
    costs: FixedCostAllowances | None
    proposed_entry: Price | None
    entry_session: CalendarSession | None
    calendar_data_version: Text | None
    calculated_at: AwareDatetime
    status: Literal["PASS", "FAIL", "UNAVAILABLE"]
    comparisons: dict[str, bool | None]
    exact_values: dict[str, str | None]
    failed_conditions: tuple[Text, ...]
    unavailable_reasons: tuple[Text, ...]
    recommendation_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"

    @model_validator(mode="after")
    def consistent(self):
        if self.policy_sha256 != digest(self.policy.model_dump(mode="json")):
            raise ValueError("Risk policy hash mismatch")
        if self.calculated_at < self.setup.as_of:
            raise ValueError("Calculation precedes signal cutoff")
        resolution = self.setup.resolution
        if self.calendar_data_version != (resolution.calendar_data_version if resolution else None):
            raise ValueError("Execution calendar version differs from signal resolution")
        if self.entry_session is not None:
            zone = ZoneInfo(self.setup.stock_snapshot.metadata.market.timezone)
            if (
                self.setup.observed_session is None
                or self.entry_session.session <= self.setup.observed_session
                or any(
                    stamp.astimezone(zone).date() != self.entry_session.session
                    for stamp in (self.entry_session.opens_at, self.entry_session.closes_at)
                )
            ):
                raise ValueError("Execution session differs from signal or market timezone")
        expected = _evaluate(
            self.setup, self.atr, self.mapping, self.costs, self.proposed_entry, self.entry_session
        )
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise ValueError("Risk assessment differs from retained inputs")
        return self

    def semantic_bytes(self):
        return encode(self.model_dump(mode="json", exclude={"calculated_at"}))


def assess_risk(
    setup: BreakoutAssessment,
    atr: ATRObservation | None,
    mapping: ExecutionPriceMapping | None,
    costs: FixedCostAllowances | None,
    proposed_entry: Decimal | None,
    calendar: SessionCalendar,
    *,
    calculated_at: datetime,
    policy: RiskPolicy | None = None,
):
    """Authenticate signal evidence before deriving frozen research levels."""
    setup = BreakoutAssessment.model_validate_json(setup.model_dump_json())
    replay_breakout(setup, calendar, calculated_at=setup.calculated_at)
    if atr is not None:
        atr = ATRObservation.model_validate_json(atr.model_dump_json())
        if atr.status == "AVAILABLE":
            authenticate_checkpoint(
                ATRCheckpoint(observation=atr, created_at=atr.calculated_at), calendar
            )
        else:
            replay = observe_atr(
                atr.snapshot,
                atr.series,
                atr.provenance,
                calendar,
                atr.observed_session,
                as_of=atr.as_of,
                calculated_at=atr.calculated_at,
            )
            # Price evidence cannot reproduce the caller's checkpoint lookup.
            diagnostics = {"checkpoint_id", "checkpoint_rejections"}
            if replay.model_dump(exclude=diagnostics) != atr.model_dump(exclude=diagnostics):
                raise ValueError("Unavailable ATR differs from authenticated replay")
    if mapping is not None:
        mapping = ExecutionPriceMapping.model_validate_json(mapping.model_dump_json())
    if costs is not None:
        costs = FixedCostAllowances.model_validate_json(costs.model_dump_json())
    if proposed_entry is not None:
        _fraction(proposed_entry)
        proposed_entry = TypeAdapter(Price).validate_python(proposed_entry)
    policy = RiskPolicy.model_validate_json((policy or RiskPolicy()).model_dump_json())
    meta = setup.stock_snapshot.metadata
    sessions = ()
    if setup.observed_session:
        start = setup.observed_session + timedelta(days=1)
        sessions = calendar.schedule(
            meta.market, meta.instrument.venue, start, start + timedelta(days=31)
        )
        if [s.session for s in sessions] != sorted({s.session for s in sessions}) or any(
            not start <= s.session <= start + timedelta(days=31) for s in sessions
        ):
            raise ValueError("Invalid execution calendar schedule")
    entry_session = sessions[0] if sessions else None
    return RiskAssessment(
        policy=policy,
        policy_sha256=digest(policy.model_dump(mode="json")),
        setup=setup,
        atr=atr,
        mapping=mapping,
        costs=costs,
        proposed_entry=proposed_entry,
        entry_session=entry_session,
        calendar_data_version=setup.resolution.calendar_data_version if setup.resolution else None,
        calculated_at=calculated_at,
        **_evaluate(setup, atr, mapping, costs, proposed_entry, entry_session),
    )


def replay_risk(assessment, calendar, *, calculated_at):
    replay = assess_risk(
        assessment.setup,
        assessment.atr,
        assessment.mapping,
        assessment.costs,
        assessment.proposed_entry,
        calendar,
        calculated_at=calculated_at,
        policy=assessment.policy,
    )
    if replay.semantic_bytes() != assessment.semantic_bytes():
        raise ValueError("Risk assessment differs from authenticated replay")
    return replay
