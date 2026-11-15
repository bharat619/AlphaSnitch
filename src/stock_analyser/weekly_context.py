"""Informational weekly trend over authenticated, complete daily constituents."""

import json
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from stock_analyser.cache import digest, load_snapshot, verified
from stock_analyser.calendars import (
    CutoffPolicy,
    HistoryResolver,
    ResolvedHistory,
    SessionCalendar,
)
from stock_analyser.contracts import Contract, HistoryRequirement, Text
from stock_analyser.indicators import (
    ExactSum,
    InputProvenance,
    SMAObservation,
    SMASpec,
    _decimal,
    observe,
)
from stock_analyser.persistence.records import SnapshotReference


class WeeklyContextSpec(Contract):
    feature_id: Literal["weekly_trend_context"] = "weekly_trend_context"
    formula_version: Literal["weekly-sma30-slope4-1"] = "weekly-sma30-slope4-1"
    implementation_version: Literal["weekly-context-1"] = "weekly-context-1"
    aggregation_version: Literal["exchange-week-last-close-1"] = "exchange-week-last-close-1"
    timeframe: Literal["weekly"] = "weekly"
    price_basis: Literal["split"] = "split"
    period: Literal[30] = 30
    week_offset: Literal[4] = 4
    division_digits: Literal[50] = 50
    comparison_tolerance: Literal["0"] = "0"
    role: Literal["informational"] = "informational"
    units: Literal["quote_currency"] = "quote_currency"

    @property
    def history_requirement(self) -> HistoryRequirement:
        return HistoryRequirement(timeframe="weekly", minimum_bars=34, adjustment="split")


class WeeklyClose(Contract):
    session_start: date
    session_end: date
    sessions: tuple[date, ...]
    closes_at: AwareDatetime
    available_at: AwareDatetime
    close: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def consistent(self):
        if not self.sessions or self.sessions != tuple(sorted(set(self.sessions))):
            raise ValueError("Weekly constituents must be unique and ascending")
        if (self.session_start, self.session_end) != (self.sessions[0], self.sessions[-1]):
            raise ValueError("Weekly boundaries differ from constituents")
        if self.available_at < self.closes_at:
            raise ValueError("Week cannot be available before its final close")
        return self


def _classification(close: Decimal, current: Fraction, previous: Fraction) -> str:
    scaled_close = 30 * Fraction(close)
    if scaled_close > current and current > previous:
        return "SUPPORTS"
    if scaled_close < current and current < previous:
        return "OPPOSES"
    return "MIXED"


class WeeklyContextObservation(Contract):
    specification: WeeklyContextSpec
    specification_sha256: Text
    snapshot: SnapshotReference
    provenance: InputProvenance
    provenance_sha256: Text
    cutoff_policy: CutoffPolicy
    as_of: AwareDatetime
    calculated_at: AwareDatetime
    daily_session: date | None
    resolution: ResolvedHistory | None
    # The daily mean is only an existing validated-window evidence carrier.
    # Weekly calculations use the final close of each complete week, never this mean.
    constituent_evidence: SMAObservation | None
    weeks: tuple[WeeklyClose, ...]
    current_sum: ExactSum | None
    previous_sum: ExactSum | None
    sma30: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None
    sma30_four_weeks_earlier: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None
    available_at: AwareDatetime | None
    classification: Literal["SUPPORTS", "OPPOSES", "MIXED", "UNAVAILABLE"]
    reasons: tuple[Text, ...]

    @model_validator(mode="after")
    def consistent(self):
        if self.calculated_at < self.as_of:
            raise ValueError("Calculation precedes assessment cutoff")
        if self.specification_sha256 != digest(self.specification.model_dump(mode="json")):
            raise ValueError("Specification hash mismatch")
        if self.provenance_sha256 != digest(self.provenance.model_dump(mode="json")):
            raise ValueError("Provenance hash mismatch")
        if self.resolution is not None and (
            self.resolution.requested_as_of != self.as_of
            or self.resolution.policy != self.cutoff_policy
            or self.resolution.requirement != self.specification.history_requirement
        ):
            raise ValueError("Weekly resolution differs from calculation contract")
        if self.classification == "UNAVAILABLE":
            if (
                not self.reasons
                or self.weeks
                or any(
                    value is not None
                    for value in (
                        self.current_sum,
                        self.previous_sum,
                        self.sma30,
                        self.sma30_four_weeks_earlier,
                        self.available_at,
                    )
                )
            ):
                raise ValueError("Unavailable context requires reasons and no aggregate values")
            return self
        evidence = self.constituent_evidence
        if self.reasons or len(self.weeks) != 34 or self.resolution is None or evidence is None:
            raise ValueError("Weekly context requires 34 validated weeks")
        if (
            evidence.status != "AVAILABLE"
            or evidence.snapshot_sha256 != self.snapshot.sha256
            or evidence.snapshot_id != self.snapshot.metadata.snapshot_id
            or evidence.provenance_sha256 != self.provenance_sha256
            or self.provenance.snapshot_sha256 != self.snapshot.sha256
            or evidence.instrument != self.snapshot.metadata.instrument
            or evidence.market != self.snapshot.metadata.market
            or evidence.as_of != self.as_of
            or evidence.calculated_at != self.calculated_at
            or evidence.calendar_data_version != self.resolution.calendar_data_version
            or evidence.window_sessions != tuple(d for w in self.weeks for d in w.sessions)
            or evidence.specification.session_offset != 0
            or evidence.assessment_close != self.weeks[-1].close
        ):
            raise ValueError("Weekly context differs from its daily input evidence")
        refs = {r.session: r for r in evidence.contributors}
        for week, period in zip(self.weeks, self.resolution.periods, strict=True):
            if (
                week.session_start != period.session_start
                or week.session_end != period.session_end
                or week.closes_at != period.closes_at
                or week.available_at
                != max(period.available_at, *(refs[d].first_proven_at for d in week.sessions))
            ):
                raise ValueError("Weekly boundaries or availability differ from evidence")
        current = sum((Fraction(w.close) for w in self.weeks[-30:]), Fraction())
        previous = sum((Fraction(w.close) for w in self.weeks[:30]), Fraction())
        if (
            self.current_sum is None
            or self.current_sum.fraction() != current
            or self.previous_sum is None
            or self.previous_sum.fraction() != previous
            or self.sma30 != _decimal(current / 30)
            or self.sma30_four_weeks_earlier != _decimal(previous / 30)
            or self.classification != _classification(self.weeks[-1].close, current, previous)
            or self.available_at != max(w.available_at for w in self.weeks)
            or self.available_at > self.as_of
        ):
            raise ValueError("Weekly result differs from exact inputs")
        return self

    def semantic_bytes(self) -> bytes:
        from stock_analyser.collection import encode

        return encode(
            self.model_dump(
                mode="json",
                exclude={
                    "calculated_at": True,
                    "constituent_evidence": {"calculated_at"},
                },
            )
        )


def observe_weekly_context(
    snapshot: SnapshotReference,
    provenance: InputProvenance,
    calendar: SessionCalendar,
    *,
    as_of: datetime,
    calculated_at: datetime,
    cutoff_policy: CutoffPolicy | None = None,
) -> WeeklyContextObservation:
    """Describe the latest expected complete week; never fall back past missing evidence.

    Calendar grace is an explicit policy, not proof of publication. Every daily
    constituent must also pass the existing authenticated price-window checks.
    Integrity failures raise; unavailable data or calendars produce explicit reasons.
    """
    as_of = TypeAdapter(AwareDatetime).validate_python(as_of)
    calculated_at = TypeAdapter(AwareDatetime).validate_python(calculated_at)
    packet = load_snapshot(snapshot)
    meta = packet.metadata
    spec = WeeklyContextSpec()
    policy = cutoff_policy or CutoffPolicy()
    resolution = None
    daily_session = None
    evidence = None
    weeks = ()
    reasons = []
    labels = ()
    try:
        resolver = HistoryResolver(calendar, policy)
        resolution = resolver.resolve(
            meta.market, meta.instrument.venue, spec.history_requirement, as_of
        )
        daily_session = resolver.resolve(
            meta.market,
            meta.instrument.venue,
            HistoryRequirement(timeframe="daily", minimum_bars=1),
            as_of,
        ).session_date
        reasons.extend(
            r.value
            for r in resolution.reasons
            if r.value in {"INSUFFICIENT_HISTORY", "PUBLICATION_UNVERIFIED"}
        )
        if len(resolution.periods) == 34:
            sessions = calendar.schedule(
                meta.market,
                meta.instrument.venue,
                resolution.periods[0].session_start,
                resolution.periods[-1].session_end,
            )
            labels = tuple(s.session for s in sessions)
            if not labels or labels != tuple(sorted(set(labels))):
                raise ValueError("Invalid constituent calendar")
    except (ValueError, KeyError, OverflowError):
        reasons.append("UNSUPPORTED_CALENDAR")
    if labels and not reasons:
        evidence = observe(
            snapshot,
            SMASpec(period=len(labels)),
            provenance,
            calendar,
            labels[-1],
            as_of=as_of,
            calculated_at=calculated_at,
        )
        if evidence.status != "AVAILABLE":
            reasons.extend(evidence.reasons)
        elif evidence.window_sessions != labels:
            reasons.append("INCOMPATIBLE_WEEKLY_CONSTITUENTS")
        else:
            # Read only authenticated rows that the daily window validated above.
            source = next(s for s in meta.sources if s.reference == packet.raw_reference)
            rows = json.loads(verified(source.reference, source.sha256), parse_float=Decimal)[
                "bars"
            ]
            refs = {r.session: r for r in evidence.contributors}
            built = []
            for period in resolution.periods:
                days = tuple(d for d in labels if period.session_start <= d <= period.session_end)
                built.append(
                    WeeklyClose(
                        session_start=period.session_start,
                        session_end=period.session_end,
                        sessions=days,
                        closes_at=period.closes_at,
                        available_at=max(
                            period.available_at, *(refs[d].first_proven_at for d in days)
                        ),
                        close=Decimal(str(rows[refs[period.session_end].source_row]["close"])),
                    )
                )
            weeks = tuple(built)
    current_sum = previous_sum = sma30 = previous_sma30 = available_at = None
    classification = "UNAVAILABLE"
    if weeks and not reasons:
        current = sum((Fraction(w.close) for w in weeks[-30:]), Fraction())
        previous = sum((Fraction(w.close) for w in weeks[:30]), Fraction())
        current_sum = ExactSum(
            numerator=str(current.numerator), denominator=str(current.denominator)
        )
        previous_sum = ExactSum(
            numerator=str(previous.numerator), denominator=str(previous.denominator)
        )
        sma30, previous_sma30 = _decimal(current / 30), _decimal(previous / 30)
        available_at = max(w.available_at for w in weeks)
        classification = _classification(weeks[-1].close, current, previous)
    return WeeklyContextObservation(
        specification=spec,
        specification_sha256=digest(spec.model_dump(mode="json")),
        snapshot=snapshot,
        provenance=provenance,
        provenance_sha256=digest(provenance.model_dump(mode="json")),
        cutoff_policy=policy,
        as_of=as_of,
        calculated_at=calculated_at,
        daily_session=daily_session,
        resolution=resolution,
        constituent_evidence=evidence,
        weeks=weeks,
        current_sum=current_sum,
        previous_sum=previous_sum,
        sma30=sma30,
        sma30_four_weeks_earlier=previous_sma30,
        available_at=available_at,
        classification=classification,
        reasons=tuple(dict.fromkeys(reasons)),
    )
