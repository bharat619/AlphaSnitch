"""Versioned technical breakout qualification; never a trading recommendation."""

import json
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from stock_analyser.cache import digest, load_snapshot, verified
from stock_analyser.calendars import CutoffPolicy, HistoryResolver, ResolvedHistory, SessionCalendar
from stock_analyser.contracts import (
    BenchmarkMapping,
    Contract,
    HistoryRequirement,
    Instrument,
    Text,
)
from stock_analyser.indicators import (
    DailyTrendObservation,
    InputProvenance,
    Observation,
    ObservationSpec,
    _decimal,
    daily_trend_specs,
    observe,
    observe_daily_trend,
    sma_history_requirement,
)
from stock_analyser.markets import InstrumentRegistry
from stock_analyser.persistence.records import SnapshotReference
from stock_analyser.universe import SelectionPolicy
from stock_analyser.weekly_context import (
    WeeklyContextObservation,
    WeeklyContextSpec,
    observe_weekly_context,
)

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Price = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
Volume = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
Status = Literal["PASS", "FAIL", "UNAVAILABLE", "NOT_ENABLED"]


class BreakoutPolicy(Contract):
    policy_id: Literal["daily-breakout"] = "daily-breakout"
    policy_version: Literal["daily-breakout-1"] = "daily-breakout-1"
    implementation_version: Literal["exact-breakout-1"] = "exact-breakout-1"
    profile: Literal["stock", "stock_with_spy"]
    price_lookback: Literal[20] = 20
    volume_lookback: Literal[20] = 20
    regular_session_seconds: Literal[23400] = 23400
    rvol_threshold: Literal["1.5"] = "1.5"
    weekly_role: Literal["informational"] = "informational"
    relative_strength: Literal["NOT_ENABLED"] = "NOT_ENABLED"
    scope: Literal["technical_setup_only"] = "technical_setup_only"

    @property
    def stock_history(self) -> HistoryRequirement:
        return sma_history_requirement(daily_trend_specs())

    def observation_spec(self, feature):
        return ObservationSpec(
            feature_id=feature,
            price_lookback=self.price_lookback,
            volume_lookback=self.volume_lookback,
            regular_session_seconds=self.regular_session_seconds,
        )


class BreakoutDependencies(Contract):
    stock_mandatory: tuple[HistoryRequirement, ...]
    benchmark_mandatory: tuple[HistoryRequirement, ...]
    stock_optional: tuple[HistoryRequirement, ...]


def _merge_requirements(requirements):
    merged = {}
    for req in requirements:
        if not req.mandatory:
            raise ValueError("Foundation requirements must be mandatory")
        key = (req.timeframe, req.adjustment)
        if key not in merged or req.required_bars > merged[key].required_bars:
            merged[key] = req
    return tuple(merged[key] for key in sorted(merged))


def breakout_dependencies(
    policy: BreakoutPolicy,
    selection: SelectionPolicy,
    benchmark_foundation: tuple[HistoryRequirement, ...],
) -> BreakoutDependencies:
    """Add technical dependencies without dropping raw or foundation benchmark gates.

    Existing benchmark requirements must be supplied explicitly even when the SPY
    strategy filter is off. Weekly context never enters mandatory selection history.
    """
    return BreakoutDependencies(
        stock_mandatory=_merge_requirements((*selection.requirements(), policy.stock_history)),
        benchmark_mandatory=_merge_requirements(
            (
                *benchmark_foundation,
                *((policy.stock_history,) if policy.profile == "stock_with_spy" else ()),
            )
        ),
        stock_optional=(
            WeeklyContextSpec().history_requirement.model_copy(update={"mandatory": False}),
        ),
    )


class BenchmarkBinding(Contract):
    market_sha256: Hash
    mapping: BenchmarkMapping
    instrument: Instrument
    verification_reference: Text

    @model_validator(mode="after")
    def matches(self):
        if (self.mapping.key, self.mapping.symbol, self.mapping.venue, self.mapping.currency) != (
            "broad_market",
            "SPY",
            "ARCX",
            "USD",
        ) or (
            self.instrument.market_id,
            self.instrument.symbol,
            self.instrument.venue,
            self.instrument.quote_currency,
        ) != ("US", "SPY", "ARCX", "USD"):
            raise ValueError("SPY research variant requires the configured US broad-market listing")
        return self


def resolve_spy_binding(market, registry: InstrumentRegistry, *, verification_reference: str):
    """Bind the configured mapping to an immutable registry identity, not a bare ticker."""
    mapping = next((m for m in market.benchmarks if m.key == "broad_market"), None)
    if mapping is None or market.market_id != "US":
        raise ValueError("US broad-market mapping is unavailable")
    instrument = registry.resolve(market.market_id, mapping.symbol, mapping.venue)
    market.validate_instrument(instrument)
    return BenchmarkBinding(
        market_sha256=digest(market.model_dump(mode="json")),
        mapping=mapping,
        instrument=instrument,
        verification_reference=verification_reference,
    )


class ExactBreakoutInputs(Contract):
    prior_highs: Annotated[tuple[Price, ...], Field(min_length=20, max_length=20)]
    signal_close: Price

    @property
    def breakout_level(self) -> Decimal:
        return max(self.prior_highs)


class ExactVolumeInputs(Contract):
    prior_volumes: Annotated[tuple[Volume, ...], Field(min_length=20, max_length=20)]
    current_volume: Volume

    @model_validator(mode="after")
    def positive_baseline(self):
        if self.prior_sum <= 0:
            raise ValueError("Relative volume requires a positive prior baseline")
        return self

    @property
    def prior_sum(self) -> Fraction:
        return sum((Fraction(v) for v in self.prior_volumes), Fraction())

    @property
    def passes(self) -> bool:
        return 40 * Fraction(self.current_volume) >= 3 * self.prior_sum


CHECKS = {
    "breakout": ("close_above_prior_high",),
    "stock_trend": ("close_above_sma50", "sma50_above_sma200", "sma50_rising"),
    "volume": ("rvol_at_least_1_5",),
    "spy_trend": ("close_above_sma50", "sma50_above_sma200", "sma50_rising"),
}


def _status(checks, unavailable=()):
    return (
        "UNAVAILABLE"
        if unavailable or any(v is None for v in checks)
        else "PASS"
        if all(checks)
        else "FAIL"
    )


def _failed_conditions(group, comparisons):
    return tuple(key for key in CHECKS[group] if comparisons.get(key) is False)


class GateResult(Contract):
    group: Literal["breakout", "stock_trend", "volume", "spy_trend"]
    status: Status
    comparisons: dict[str, bool | None]
    unavailable_reasons: tuple[Text, ...] = ()
    failed_conditions: tuple[Text, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        if self.status == "NOT_ENABLED":
            if (
                self.group != "spy_trend"
                or self.comparisons
                or self.unavailable_reasons
                or self.failed_conditions
            ):
                raise ValueError("Only the SPY research gate can be explicitly disabled")
            return self
        if set(self.comparisons) != set(CHECKS[self.group]):
            raise ValueError("Gate comparisons differ from the versioned policy")
        if self.status != _status(tuple(self.comparisons.values()), self.unavailable_reasons):
            raise ValueError("Gate status differs from its comparisons and evidence")
        if self.failed_conditions != _failed_conditions(self.group, self.comparisons):
            raise ValueError("Gate must retain all failed comparisons")
        if any(v is None for v in self.comparisons.values()) and not self.unavailable_reasons:
            raise ValueError("Unavailable comparisons require evidence reasons")
        return self


def _gate(group, comparisons, reasons=()):
    return GateResult(
        group=group,
        status=_status(tuple(comparisons.values()), reasons),
        comparisons=comparisons,
        unavailable_reasons=tuple(dict.fromkeys(reasons)),
        failed_conditions=_failed_conditions(group, comparisons),
    )


def _trend_gate(group, trend, reasons=()):
    if trend is None:
        return _gate(group, dict.fromkeys(CHECKS[group]), reasons or ("TREND_EVIDENCE_MISSING",))
    reasons = (
        *reasons,
        *(
            f"SMA{o.specification.period}[t-{o.specification.session_offset}]:{r}"
            for o in trend.observations
            for r in o.reasons
        ),
    )
    return _gate(group, trend.comparisons(), reasons)


def _gates(policy, price_observation, trend, volume_observation, prices, volumes, spy, spy_reasons):
    return (
        _gate(
            "breakout",
            {"close_above_prior_high": price_observation.value if prices else None},
            price_observation.reasons if price_observation else ("PRICE_EVIDENCE_MISSING",),
        ),
        _trend_gate("stock_trend", trend),
        _gate(
            "volume",
            {"rvol_at_least_1_5": volumes.passes if volumes else None},
            volume_observation.reasons if volume_observation else ("VOLUME_EVIDENCE_MISSING",),
        ),
        _trend_gate("spy_trend", spy, spy_reasons)
        if policy.profile == "stock_with_spy"
        else GateResult(group="spy_trend", status="NOT_ENABLED", comparisons={}),
    )


def _assessment_status(gates, context_reasons):
    active = tuple(g for g in gates if g.status != "NOT_ENABLED")
    if context_reasons or any(g.status == "UNAVAILABLE" for g in active):
        return "UNAVAILABLE"
    return "FAIL" if any(g.status == "FAIL" for g in active) else "PASS"


def _assessment_availability(status, resolution, price, volume, trend, spy):
    """Only mandatory evidence can determine technical assessment availability."""
    if status == "UNAVAILABLE":
        return None
    observations = (price, volume, *trend.observations, *(spy.observations if spy else ()))
    return max(resolution.periods[-1].available_at, *(o.available_at for o in observations))


class BreakoutAssessment(Contract):
    policy: BreakoutPolicy
    policy_sha256: Hash
    cutoff_policy: CutoffPolicy
    resolution: ResolvedHistory | None
    observed_session: date | None
    as_of: AwareDatetime
    calculated_at: AwareDatetime
    stock_snapshot: SnapshotReference
    stock_provenance: InputProvenance
    breakout_observation: Observation | None
    stock_trend: DailyTrendObservation | None
    volume_observation: Observation | None
    exact_prices: ExactBreakoutInputs | None
    exact_volumes: ExactVolumeInputs | None
    weekly_context: WeeklyContextObservation
    benchmark_binding: BenchmarkBinding | None
    benchmark_snapshot: SnapshotReference | None
    benchmark_provenance: InputProvenance | None
    spy_trend: DailyTrendObservation | None
    spy_reasons: tuple[Text, ...]
    gates: tuple[GateResult, GateResult, GateResult, GateResult]
    context_reasons: tuple[Text, ...]
    status: Literal["PASS", "FAIL", "UNAVAILABLE"]
    available_at: AwareDatetime | None
    recommendation_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"

    @model_validator(mode="after")
    def consistent(self):
        if self.policy_sha256 != digest(self.policy.model_dump(mode="json")):
            raise ValueError("Policy hash mismatch")
        if self.calculated_at < self.as_of:
            raise ValueError("Calculation precedes assessment cutoff")
        if self.resolution is not None and (
            self.resolution.requested_as_of != self.as_of
            or self.resolution.policy != self.cutoff_policy
            or self.resolution.session_date != self.observed_session
            or self.resolution.market_id != self.stock_snapshot.metadata.market.market_id
            or self.resolution.market_configuration_version
            != self.stock_snapshot.metadata.market.configuration_version
            or self.resolution.venue != self.stock_snapshot.metadata.instrument.venue
            or self.resolution.requirement != HistoryRequirement(timeframe="daily", minimum_bars=1)
        ):
            raise ValueError("Assessment session differs from calendar resolution")
        if self.resolution is None and self.observed_session is not None:
            raise ValueError("Assessment session requires calendar resolution")
        if self.context_reasons != _context_reasons(self.stock_snapshot, self.resolution):
            raise ValueError("Calendar/market blockers must be retained")
        stock_observations = tuple(
            o
            for o in (
                self.breakout_observation,
                self.volume_observation,
                *(self.stock_trend.observations if self.stock_trend else ()),
            )
            if o
        )
        for o in stock_observations:
            _check_observation(
                o,
                self.stock_snapshot,
                self.stock_provenance,
                self.observed_session,
                self.as_of,
                self.calculated_at,
            )
        for o, feature in (
            (self.breakout_observation, "close_above_prior_high"),
            (self.volume_observation, "relative_volume"),
        ):
            if o is not None and o.specification != self.policy.observation_spec(feature):
                raise ValueError("Observation parameters differ from breakout policy")
        if (self.exact_prices is not None) != (
            self.breakout_observation is not None
            and self.breakout_observation.status == "AVAILABLE"
        ):
            raise ValueError("Exact price inputs require available breakout evidence")
        if self.exact_prices and self.breakout_observation.value != (
            self.exact_prices.signal_close > self.exact_prices.breakout_level
        ):
            raise ValueError("Breakout comparison differs from exact inputs")
        if (self.exact_volumes is not None) != (
            self.volume_observation is not None and self.volume_observation.status == "AVAILABLE"
        ):
            raise ValueError("Exact volumes require available volume evidence")
        if self.exact_volumes and self.volume_observation.value != _decimal(
            20 * Fraction(self.exact_volumes.current_volume) / self.exact_volumes.prior_sum
        ):
            raise ValueError("RVOL observation differs from exact volumes")
        weekly = self.weekly_context
        if (
            weekly.snapshot != self.stock_snapshot
            or weekly.provenance != self.stock_provenance
            or weekly.as_of != self.as_of
            or weekly.calculated_at != self.calculated_at
            or weekly.cutoff_policy != self.cutoff_policy
        ):
            raise ValueError("Weekly information differs from stock evidence/cutoff")
        if self.policy.profile == "stock":
            if (
                any(
                    x is not None
                    for x in (
                        self.benchmark_binding,
                        self.benchmark_snapshot,
                        self.benchmark_provenance,
                        self.spy_trend,
                    )
                )
                or self.spy_reasons
            ):
                raise ValueError("Disabled SPY gate cannot retain an evaluated trend")
        if self.spy_trend:
            if not self.benchmark_snapshot or not self.benchmark_provenance:
                raise ValueError("SPY trend requires retained benchmark inputs")
            for o in self.spy_trend.observations:
                _check_observation(
                    o,
                    self.benchmark_snapshot,
                    self.benchmark_provenance,
                    self.observed_session,
                    self.as_of,
                    self.calculated_at,
                )
            binding_reasons = _benchmark_reasons(
                self.stock_snapshot, self.benchmark_binding, self.benchmark_snapshot
            )
            if any(r not in self.spy_reasons for r in binding_reasons):
                raise ValueError("SPY identity/source conflicts must remain blocking")
        expected = _gates(
            self.policy,
            self.breakout_observation,
            self.stock_trend,
            self.volume_observation,
            self.exact_prices,
            self.exact_volumes,
            self.spy_trend,
            self.spy_reasons,
        )
        if self.gates != expected:
            raise ValueError("Gates differ from retained input comparisons")
        status = _assessment_status(self.gates, self.context_reasons)
        if self.status != status:
            raise ValueError("Aggregate status differs from mandatory gates")
        available = _assessment_availability(
            status,
            self.resolution,
            self.breakout_observation,
            self.volume_observation,
            self.stock_trend,
            self.spy_trend,
        )
        if self.available_at != available or (available is not None and available > self.as_of):
            raise ValueError("Assessment availability differs from mandatory evidence")
        return self

    def semantic_bytes(self) -> bytes:
        from stock_analyser.collection import encode

        return encode(
            self.model_dump(
                mode="json",
                exclude={
                    "calculated_at": True,
                    "breakout_observation": {"calculated_at"},
                    "volume_observation": {"calculated_at"},
                    "stock_trend": {"observations": {"__all__": {"calculated_at"}}},
                    "spy_trend": {"observations": {"__all__": {"calculated_at"}}},
                    "weekly_context": {
                        "calculated_at": True,
                        "constituent_evidence": {"calculated_at"},
                    },
                },
            )
        )


def _check_observation(o, snapshot, provenance, day, as_of, calculated_at):
    if (
        o.snapshot_id != snapshot.metadata.snapshot_id
        or o.snapshot_sha256 != snapshot.sha256
        or o.specification_sha256 != digest(o.specification.model_dump(mode="json"))
        or o.provenance != provenance
        or o.provenance_sha256 != digest(provenance.model_dump(mode="json"))
        or o.instrument != snapshot.metadata.instrument
        or o.market != snapshot.metadata.market
        or o.feed != snapshot.metadata.feed
        or o.price_basis != snapshot.metadata.adjustment
        or o.provider not in {s.provider for s in snapshot.metadata.sources}
        or o.observed_session != day
        or o.as_of != as_of
        or o.calculated_at != calculated_at
    ):
        raise ValueError("Observation differs from the frozen assessment inputs")


def _benchmark_reasons(stock, binding, benchmark):
    reasons = []
    if binding is None:
        reasons.append("BENCHMARK_IDENTITY_UNVERIFIED")
    elif (
        binding.market_sha256 != digest(stock.metadata.market.model_dump(mode="json"))
        or binding.mapping not in stock.metadata.market.benchmarks
        or (benchmark and benchmark.metadata.instrument != binding.instrument)
    ):
        reasons.append("INCOMPATIBLE_BENCHMARK_IDENTITY")
    if benchmark is None:
        reasons.append("BENCHMARK_EVIDENCE_MISSING")
    elif (
        benchmark.metadata.market != stock.metadata.market
        or benchmark.metadata.feed != stock.metadata.feed
        or {s.provider for s in benchmark.metadata.sources}
        != {s.provider for s in stock.metadata.sources}
        or benchmark.metadata.adjustment != stock.metadata.adjustment
    ):
        reasons.append("INCOMPATIBLE_BENCHMARK_SOURCE_OR_BASIS")
    return tuple(reasons)


def _context_reasons(snapshot, resolution):
    reasons = []
    if snapshot.metadata.market.market_id != "US" or snapshot.metadata.currency != "USD":
        reasons.append("UNSUPPORTED_STRATEGY_MARKET")
    if resolution is None:
        reasons.append("UNSUPPORTED_CALENDAR")
    else:
        reasons.extend(
            r.value
            for r in resolution.reasons
            if r.value in {"INSUFFICIENT_HISTORY", "PUBLICATION_UNVERIFIED"}
        )
    if resolution is None or resolution.session_date is None:
        reasons.append("ASSESSMENT_SESSION_UNAVAILABLE")
    return tuple(reasons)


def qualify_breakout(
    snapshot: SnapshotReference,
    provenance: InputProvenance,
    calendar: SessionCalendar,
    policy: BreakoutPolicy,
    *,
    as_of: datetime,
    calculated_at: datetime,
    cutoff_policy: CutoffPolicy | None = None,
    benchmark_binding: BenchmarkBinding | None = None,
    benchmark_snapshot: SnapshotReference | None = None,
    benchmark_provenance: InputProvenance | None = None,
) -> BreakoutAssessment:
    """Assess the latest expected completed session; never fall back past missing t.

    All stock gates are evaluated so a failed gate cannot hide missing evidence.
    Weekly context is informational. The SPY gate is evaluated only for its
    explicitly selected research profile. No foundation or recommendation gate
    is cleared by a technical PASS.
    """
    as_of = TypeAdapter(AwareDatetime).validate_python(as_of)
    calculated_at = TypeAdapter(AwareDatetime).validate_python(calculated_at)
    policy = BreakoutPolicy.model_validate(policy.model_dump(mode="json"))
    packet = load_snapshot(snapshot)
    meta = packet.metadata
    cutoff = cutoff_policy or CutoffPolicy()
    resolution = None
    day = None
    try:
        resolution = HistoryResolver(calendar, cutoff).resolve(
            meta.market,
            meta.instrument.venue,
            HistoryRequirement(timeframe="daily", minimum_bars=1),
            as_of,
        )
        day = resolution.session_date
    except (ValueError, KeyError, OverflowError):
        resolution = None
    reasons = _context_reasons(snapshot, resolution)
    weekly = observe_weekly_context(
        snapshot,
        provenance,
        calendar,
        as_of=as_of,
        calculated_at=calculated_at,
        cutoff_policy=cutoff,
    )
    price_observation = volume_observation = trend = prices = volumes = spy = None
    if day is not None:
        price_observation = observe(
            snapshot,
            policy.observation_spec("close_above_prior_high"),
            provenance,
            calendar,
            day,
            as_of=as_of,
            calculated_at=calculated_at,
        )
        trend = observe_daily_trend(
            snapshot, provenance, calendar, day, as_of=as_of, calculated_at=calculated_at
        )
        volume_observation = observe(
            snapshot,
            policy.observation_spec("relative_volume"),
            provenance,
            calendar,
            day,
            as_of=as_of,
            calculated_at=calculated_at,
        )
        rows = []
        if price_observation.status == "AVAILABLE" or volume_observation.status == "AVAILABLE":
            source = next(s for s in meta.sources if s.reference == packet.raw_reference)
            rows = json.loads(
                verified(source.reference, source.sha256),
                parse_float=Decimal,
                parse_constant=Decimal,
            )["bars"]
        if price_observation.status == "AVAILABLE":
            selected = [rows[r.source_row] for r in price_observation.contributors]
            prices = ExactBreakoutInputs(
                prior_highs=tuple(Decimal(str(r["high"])) for r in selected[:-1]),
                signal_close=Decimal(str(selected[-1]["close"])),
            )
        if volume_observation.status == "AVAILABLE":
            selected = [rows[r.source_row] for r in volume_observation.contributors]
            volumes = ExactVolumeInputs(
                prior_volumes=tuple(Decimal(str(r["volume"])) for r in selected[:-1]),
                current_volume=Decimal(str(selected[-1]["volume"])),
            )
    spy_reasons = ()
    if policy.profile == "stock_with_spy":
        if benchmark_snapshot:
            load_snapshot(benchmark_snapshot)
        spy_reasons = _benchmark_reasons(snapshot, benchmark_binding, benchmark_snapshot)
        if benchmark_provenance is None:
            spy_reasons = (*spy_reasons, "BENCHMARK_PROVENANCE_MISSING")
        if day is not None and benchmark_snapshot is not None and benchmark_provenance is not None:
            spy = observe_daily_trend(
                benchmark_snapshot,
                benchmark_provenance,
                calendar,
                day,
                as_of=as_of,
                calculated_at=calculated_at,
            )
    else:
        benchmark_binding = benchmark_snapshot = benchmark_provenance = None
    gates = _gates(
        policy, price_observation, trend, volume_observation, prices, volumes, spy, spy_reasons
    )
    status = _assessment_status(gates, reasons)
    available = _assessment_availability(
        status,
        resolution,
        price_observation,
        volume_observation,
        trend,
        spy,
    )
    return BreakoutAssessment(
        policy=policy,
        policy_sha256=digest(policy.model_dump(mode="json")),
        cutoff_policy=cutoff,
        resolution=resolution,
        observed_session=day,
        as_of=as_of,
        calculated_at=calculated_at,
        stock_snapshot=snapshot,
        stock_provenance=provenance,
        breakout_observation=price_observation,
        stock_trend=trend,
        volume_observation=volume_observation,
        exact_prices=prices,
        exact_volumes=volumes,
        weekly_context=weekly,
        benchmark_binding=benchmark_binding,
        benchmark_snapshot=benchmark_snapshot,
        benchmark_provenance=benchmark_provenance,
        spy_trend=spy,
        spy_reasons=spy_reasons,
        gates=gates,
        context_reasons=tuple(dict.fromkeys(reasons)),
        status=status,
        available_at=available,
    )


def replay_breakout(
    assessment: BreakoutAssessment,
    calendar: SessionCalendar,
    *,
    calculated_at: datetime,
) -> BreakoutAssessment:
    """Recalculate retained evidence; schema validity alone does not authenticate outputs."""
    replay = qualify_breakout(
        assessment.stock_snapshot,
        assessment.stock_provenance,
        calendar,
        assessment.policy,
        as_of=assessment.as_of,
        calculated_at=calculated_at,
        cutoff_policy=assessment.cutoff_policy,
        benchmark_binding=assessment.benchmark_binding,
        benchmark_snapshot=assessment.benchmark_snapshot,
        benchmark_provenance=assessment.benchmark_provenance,
    )
    if replay.semantic_bytes() != assessment.semantic_bytes():
        raise ValueError("Breakout assessment differs from authenticated evidence replay")
    return replay
