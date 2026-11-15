"""Fixed-anchor Wilder ATR14 over retained evidence; no trading policy is enabled."""

import json
from datetime import date, datetime
from decimal import Decimal
from fractions import Fraction
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, TypeAdapter, model_validator

from stock_analyser.cache import digest, load_snapshot, verified
from stock_analyser.calendars import CutoffPolicy, HistoryResolver, SessionCalendar
from stock_analyser.contracts import (
    Contract,
    FeedCoverage,
    HistoryRequirement,
    Instrument,
    MarketContext,
    Text,
)
from stock_analyser.indicators import InputProvenance, SMAObservation, SMASpec, _decimal, observe
from stock_analyser.persistence.records import SnapshotReference

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Count = Annotated[int, Field(strict=True, ge=0)]
ATRValue = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class ATRSpec(Contract):
    feature_id: Literal["atr14"] = "atr14"
    formula_version: Literal["wilder-real-previous-close-1"] = "wilder-real-previous-close-1"
    implementation_version: Literal["atr14-1"] = "atr14-1"
    numeric_version: Literal["fraction-single-round-half-even-50-1"] = (
        "fraction-single-round-half-even-50-1"
    )
    timeframe: Literal["daily"] = "daily"
    price_basis: Literal["split"] = "split"
    period: Literal[14] = 14
    warmup_updates: Literal[245] = 245
    digits: Literal[50] = 50
    units: Literal["quote_currency_per_share"] = "quote_currency_per_share"
    # Versioned engineering limits, not economic thresholds or risk defaults.
    max_bars: Literal[25000] = 25000
    max_input_digits: Literal[200] = 200
    max_abs_exponent: Literal[200] = 200

    @property
    def history_requirement(self) -> HistoryRequirement:
        return HistoryRequirement(
            timeframe="daily", minimum_bars=15, warmup_bars=245, adjustment="split"
        )


class ATRProvenance(InputProvenance):
    """Explicit adjustment revision/audit assertion tied to the snapshot hash."""

    adjustment_revision: Text
    adjustment_verification_reference: Text


class ATRSeries(Contract):
    specification: ATRSpec = ATRSpec()
    policy_series_id: Text
    instrument: Instrument
    market: MarketContext
    provider: Text
    feed: FeedCoverage
    calendar_data_version: Text
    first_assessment_session: date
    anchor_session: date

    @property
    def identity(self) -> str:
        return digest(self.model_dump(mode="json"))


def declare_atr_series(
    instrument: Instrument,
    market: MarketContext,
    provider: str,
    feed: FeedCoverage,
    calendar: SessionCalendar,
    first_assessment_session: date,
    *,
    policy_series_id: str,
) -> ATRSeries:
    """Declare t0 before inspecting outcomes. Resolve dates without loading prices."""
    market.validate_instrument(instrument)
    current = calendar.schedule(
        market, instrument.venue, first_assessment_session, first_assessment_session
    )
    if len(current) != 1 or current[0].session != first_assessment_session:
        raise ValueError("First assessment must be an expected exchange session")
    resolution = HistoryResolver(calendar, CutoffPolicy(daily_grace_seconds=0)).resolve(
        market, instrument.venue, ATRSpec().history_requirement, current[0].closes_at
    )
    if len(resolution.periods) != 260 or resolution.session_date != first_assessment_session:
        raise ValueError("Cannot resolve the fixed 260-session initialization window")
    return ATRSeries(
        policy_series_id=policy_series_id,
        instrument=instrument,
        market=market,
        provider=provider,
        feed=feed,
        calendar_data_version=resolution.calendar_data_version,
        first_assessment_session=first_assessment_session,
        anchor_session=resolution.history_start,
    )


def canonical_atr(ranges: tuple[Fraction, ...]) -> Decimal:
    """Reference seed plus one exact rational expression and one rounding per update."""
    if len(ranges) < 14 or any(value < 0 for value in ranges):
        raise ValueError("ATR requires fourteen nonnegative true ranges")
    state = _decimal(sum(ranges[:14], Fraction()) / 14)
    for value in ranges[14:]:
        state = _decimal((13 * Fraction(state) + value) / 14)
    return state


class ATRObservation(Contract):
    series: ATRSeries
    series_sha256: Hash
    snapshot: SnapshotReference
    provenance: ATRProvenance
    provenance_sha256: Hash
    observed_session: date
    as_of: AwareDatetime
    calculated_at: AwareDatetime
    constituent_evidence: SMAObservation | None
    consumed_prefix_sha256: Hash | None
    seed_session: date | None
    update_count: Count | None
    previous_close: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)] | None
    value: ATRValue | None
    available_at: AwareDatetime | None
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    reasons: tuple[Text, ...]
    calculation_mode: Literal["FULL", "CONTINUED", "REPLAYED", "UNAVAILABLE"]
    checkpoint_id: Hash | None = None
    checkpoint_rejections: tuple[Text, ...] = ()

    @property
    def strategy_ready(self) -> bool:
        return self.status == "AVAILABLE" and self.update_count >= 245

    @property
    def readiness_reasons(self) -> tuple[str, ...]:
        if self.status != "AVAILABLE":
            return self.reasons
        return () if self.strategy_ready else ("WARMUP_INCOMPLETE",)

    @model_validator(mode="after")
    def consistent(self):
        if self.series_sha256 != self.series.identity:
            raise ValueError("ATR series hash mismatch")
        if self.provenance_sha256 != digest(self.provenance.model_dump(mode="json")):
            raise ValueError("ATR provenance hash mismatch")
        if self.calculated_at < self.as_of:
            raise ValueError("Calculation precedes assessment cutoff")
        if self.status == "UNAVAILABLE":
            if (
                not self.reasons
                or self.calculation_mode != "UNAVAILABLE"
                or any(
                    v is not None
                    for v in (
                        self.value,
                        self.previous_close,
                        self.update_count,
                        self.seed_session,
                        self.available_at,
                        self.consumed_prefix_sha256,
                    )
                )
            ):
                raise ValueError("Unavailable ATR requires reasons and no usable state")
            return self
        evidence = self.constituent_evidence
        if (
            self.reasons
            or evidence is None
            or evidence.status != "AVAILABLE"
            or self.value is None
            or self.consumed_prefix_sha256 is None
            or self.available_at is None
            or self.available_at > self.as_of
            or self.calculation_mode == "UNAVAILABLE"
        ):
            raise ValueError("Available ATR requires validated state and evidence")
        labels = evidence.window_sessions
        if (
            len(labels) < 15
            or labels[0] != self.series.anchor_session
            or labels[-1] != self.observed_session
            or self.seed_session != labels[14]
            or self.update_count != len(labels) - 15
            or self.previous_close != evidence.assessment_close
            or evidence.snapshot_sha256 != self.snapshot.sha256
            or evidence.snapshot_id != self.snapshot.metadata.snapshot_id
            or evidence.instrument != self.series.instrument
            or evidence.market != self.series.market
            or evidence.provider != self.series.provider
            or evidence.feed != self.series.feed
            or evidence.calendar_data_version != self.series.calendar_data_version
            or evidence.as_of != self.as_of
            or evidence.calculated_at != self.calculated_at
            or evidence.available_at != self.available_at
            or evidence.provenance != _price_provenance(self.provenance)
        ):
            raise ValueError("ATR state differs from its evidence")
        if self.calculation_mode == "CONTINUED" and (
            not self.checkpoint_id or self.checkpoint_rejections
        ):
            raise ValueError("Continuation requires an accepted checkpoint")
        if self.calculation_mode == "REPLAYED" and not self.checkpoint_rejections:
            raise ValueError("Replay must retain its checkpoint rejection reason")
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


class ATRCheckpoint(Contract):
    checkpoint_version: Literal["atr-checkpoint-1"] = "atr-checkpoint-1"
    observation: ATRObservation
    created_at: AwareDatetime

    @model_validator(mode="after")
    def usable(self):
        if (
            self.observation.status != "AVAILABLE"
            or self.created_at < self.observation.calculated_at
        ):
            raise ValueError("Checkpoint requires available state and a valid creation time")
        return self

    @property
    def identity(self) -> str:
        return digest(self.model_dump(mode="json"))


def _price_provenance(provenance: ATRProvenance) -> InputProvenance:
    return InputProvenance.model_validate(
        provenance.model_dump(
            exclude={
                "adjustment_revision",
                "adjustment_verification_reference",
            }
        )
    )


def _labels(series, calendar, observed_session):
    # Recheck initialization even when a serialized series was supplied directly.
    declared = declare_atr_series(
        series.instrument,
        series.market,
        series.provider,
        series.feed,
        calendar,
        series.first_assessment_session,
        policy_series_id=series.policy_series_id,
    )
    if declared != series:
        raise ValueError("Series anchor/calendar differs from its declared t0")
    if observed_session < series.anchor_session:
        raise ValueError("Assessment precedes fixed anchor")
    if (observed_session - series.anchor_session).days > 366 * 150:
        raise ValueError("History exceeds resource limits")
    sessions = calendar.schedule(
        series.market, series.instrument.venue, series.anchor_session, observed_session
    )
    labels = tuple(s.session for s in sessions)
    if (
        not labels
        or labels != tuple(sorted(set(labels)))
        or labels[0] != series.anchor_session
        or labels[-1] != observed_session
        or len(labels) > series.specification.max_bars
    ):
        raise ValueError("Invalid or excessive fixed-anchor session history")
    return labels


def atr_history_requirement(
    series: ATRSeries,
    calendar: SessionCalendar,
    observed_session: date,
) -> HistoryRequirement:
    """Expand the initial split-price request to preserve the fixed recurrence anchor.

    The 260-bar readiness floor stays independent of any raw-price selection
    requirement. Later requests grow; they never substitute a rolling seed.
    """
    labels = _labels(series, calendar, observed_session)
    return HistoryRequirement(
        timeframe="daily", minimum_bars=max(260, len(labels)), adjustment="split", mandatory=True
    )


def _prefix(series, provenance, refs, rows):
    # Whole source hashes and retrieval times change on append. Row hashes, ordered
    # session closes and publication assertions authenticate the consumed prefix.
    return digest(
        {
            "series": series.identity,
            "adjustment_revision": provenance.adjustment_revision,
            "adjustment_verification_reference": provenance.adjustment_verification_reference,
            "inputs": [
                {
                    "session": str(r.session),
                    "row": rows[r.source_row],
                    "closes_at": r.closes_at.isoformat(),
                    "publication": r.publication.model_dump(mode="json"),
                }
                for r in refs
            ],
        }
    )


def _state_signature(observation):
    return (
        observation.series_sha256,
        observation.provenance_sha256,
        observation.snapshot,
        observation.observed_session,
        observation.consumed_prefix_sha256,
        str(observation.value),
        str(observation.previous_close),
        observation.seed_session,
        observation.update_count,
        observation.available_at,
        observation.constituent_evidence,
    )


def authenticate_checkpoint(checkpoint: ATRCheckpoint, calendar: SessionCalendar) -> None:
    """A checksum is insufficient: reproduce state from the original retained evidence."""
    checkpoint = ATRCheckpoint.model_validate(checkpoint.model_dump(mode="json"))
    old = checkpoint.observation
    replay = observe_atr(
        old.snapshot,
        old.series,
        old.provenance,
        calendar,
        old.observed_session,
        as_of=old.as_of,
        calculated_at=old.calculated_at,
    )
    if replay.status != "AVAILABLE" or _state_signature(replay) != _state_signature(old):
        raise ValueError("Checkpoint state differs from authenticated original replay")


def observe_atr(
    snapshot: SnapshotReference,
    series: ATRSeries,
    provenance: ATRProvenance,
    calendar: SessionCalendar,
    observed_session: date,
    *,
    as_of: datetime,
    calculated_at: datetime,
    checkpoint: ATRCheckpoint | None = None,
    checkpoint_id: str | None = None,
    checkpoint_rejections: tuple[str, ...] = (),
) -> ATRObservation:
    """Calculate from the same anchor; an invalid continuation falls back to full replay.

    The full consumed history is required in this first implementation. It is
    authenticated even during continuation. Missing anchor coverage never rolls
    the seed. Hash-chain failures of the current snapshot raise, as with SMA.
    """
    as_of = TypeAdapter(AwareDatetime).validate_python(as_of)
    calculated_at = TypeAdapter(AwareDatetime).validate_python(calculated_at)
    packet = load_snapshot(snapshot)
    meta = packet.metadata
    reasons, rejections = [], list(checkpoint_rejections)
    evidence = None
    labels = ()
    if (
        meta.instrument != series.instrument
        or meta.market != series.market
        or meta.feed != series.feed
        or any(s.provider != series.provider for s in meta.sources)
    ):
        reasons.append("INCOMPATIBLE_SERIES_IDENTITY")
    if provenance.snapshot_sha256 != snapshot.sha256:
        reasons.append("INCOMPATIBLE_PROVENANCE")
    try:
        labels = _labels(series, calendar, observed_session)
    except (ValueError, KeyError, OverflowError):
        reasons.append("INVALID_ANCHOR_OR_CALENDAR")
    source = next((s for s in meta.sources if s.reference == packet.raw_reference), None)
    if source is None:
        raise ValueError("Packet raw source is not authenticated by its metadata")
    rows = []
    try:
        rows = json.loads(
            verified(source.reference, source.sha256), parse_float=Decimal, parse_constant=Decimal
        )["bars"]
        if not isinstance(rows, list):
            raise ValueError
        # Bound arithmetic before the evidence carrier calculates its exact mean.
        label_set = set(labels)
        for row in rows:
            if date.fromisoformat(row["session"]) not in label_set:
                continue
            for field in ("open", "high", "low", "close"):
                value = Decimal(str(row[field]))
                if not value.is_finite() or value <= 0:
                    reasons.append("INVALID_OHLC")
                elif (
                    len(value.as_tuple().digits) > series.specification.max_input_digits
                    or abs(value.as_tuple().exponent) > series.specification.max_abs_exponent
                ):
                    reasons.append("NUMERIC_RESOURCE_LIMIT")
        # Normalize JSON decimals exactly as the shared row-hash contract does.
        rows = json.loads(json.dumps(rows, default=str))
    except (ValueError, TypeError, KeyError, ArithmeticError):
        reasons.append("INVALID_PRICE_PAYLOAD")
    if labels and not reasons:
        evidence = observe(
            snapshot,
            SMASpec(period=len(labels)),
            _price_provenance(provenance),
            calendar,
            observed_session,
            as_of=as_of,
            calculated_at=calculated_at,
        )
        reasons.extend(evidence.reasons)
        if evidence.window_sessions != labels:
            reasons.append("INCOMPATIBLE_ANCHOR_HISTORY")
    if len(labels) < 15:
        reasons.append("INSUFFICIENT_SEED_HISTORY")
    value = previous_close = seed_session = update_count = prefix = available_at = None
    mode = "UNAVAILABLE"
    if checkpoint is not None:
        checkpoint_id = checkpoint_id or checkpoint.identity
        try:
            if checkpoint.identity != checkpoint_id:
                raise ValueError("Checkpoint identity mismatch")
            authenticate_checkpoint(checkpoint, calendar)
        except (ValueError, OSError, ArithmeticError, KeyError):
            rejections.append("CHECKPOINT_AUTHENTICATION_FAILED")
        else:
            old = checkpoint.observation
            if old.series != series:
                rejections.append("CHECKPOINT_CONFIGURATION_MISMATCH")
            if checkpoint.created_at > calculated_at:
                rejections.append("CHECKPOINT_CREATED_AFTER_CALCULATION")
            if not labels or len(labels) < 2 or old.observed_session != labels[-2]:
                rejections.append("CHECKPOINT_NOT_NEXT_SESSION")
            if evidence is not None and evidence.status == "AVAILABLE":
                old_refs = tuple(
                    r for r in evidence.contributors if r.session <= old.observed_session
                )
                if _prefix(series, provenance, old_refs, rows) != old.consumed_prefix_sha256:
                    rejections.append("CHECKPOINT_CONSUMED_HISTORY_CHANGED")
            else:
                rejections.append("CHECKPOINT_PREFIX_UNVERIFIABLE")
    elif checkpoint_id is not None and not rejections:
        rejections.append("CHECKPOINT_NOT_FOUND")
    if not reasons:
        try:
            bars = [rows[r.source_row] for r in evidence.contributors]
            prices = [
                (
                    Fraction(Decimal(str(r["high"]))),
                    Fraction(Decimal(str(r["low"]))),
                    Fraction(Decimal(str(r["close"]))),
                )
                for r in bars
            ]
            ranges = tuple(
                max(high - low, abs(high - prices[i - 1][2]), abs(low - prices[i - 1][2]))
                for i, (high, low, _) in enumerate(prices)
                if i
            )
            if checkpoint is not None and not rejections:
                value = _decimal((13 * Fraction(checkpoint.observation.value) + ranges[-1]) / 14)
                mode = "CONTINUED"
            else:
                value = canonical_atr(ranges)
                mode = "REPLAYED" if rejections else "FULL"
            previous_close = Decimal(str(bars[-1]["close"]))
            seed_session, update_count = labels[14], len(labels) - 15
            prefix = _prefix(series, provenance, evidence.contributors, rows)
            available_at = evidence.available_at
        except (ValueError, ArithmeticError):
            reasons.append("NUMERIC_FAILURE")
            value = previous_close = seed_session = update_count = prefix = available_at = None
            mode = "UNAVAILABLE"
    return ATRObservation(
        series=series,
        series_sha256=series.identity,
        snapshot=snapshot,
        provenance=provenance,
        provenance_sha256=digest(provenance.model_dump(mode="json")),
        observed_session=observed_session,
        as_of=as_of,
        calculated_at=calculated_at,
        constituent_evidence=evidence,
        consumed_prefix_sha256=prefix,
        seed_session=seed_session,
        update_count=update_count,
        previous_close=previous_close,
        value=value,
        available_at=available_at,
        status="UNAVAILABLE" if reasons else "AVAILABLE",
        reasons=tuple(dict.fromkeys(reasons)),
        calculation_mode=mode,
        checkpoint_id=checkpoint_id,
        checkpoint_rejections=tuple(dict.fromkeys(rejections)),
    )


def save_atr_checkpoint(database, checkpoint: ATRCheckpoint, calendar: SessionCalendar) -> str:
    """Authenticate outside the write transaction, then bind to a registered snapshot."""
    authenticate_checkpoint(checkpoint, calendar)
    with database.write() as repo:
        repo.put_atr_checkpoint(checkpoint)
    return checkpoint.identity


def observe_atr_with_checkpoint(
    database,
    snapshot,
    series,
    provenance,
    calendar,
    observed_session,
    *,
    as_of,
    calculated_at,
    checkpoint_id,
) -> ATRObservation:
    """Resolve an immutable checkpoint from the database trust root, or replay."""
    checkpoint = None
    rejections = ()
    try:
        with database.read() as repo:
            checkpoint = repo.get_atr_checkpoint(checkpoint_id)
            original = checkpoint.observation.snapshot
            if repo.get_snapshot(original.metadata.snapshot_id) != original:
                raise ValueError("Checkpoint snapshot differs from registered evidence")
    except LookupError:
        rejections = ("CHECKPOINT_NOT_FOUND",)
    except ValueError:
        checkpoint = None
        rejections = ("CHECKPOINT_PERSISTED_RECORD_INVALID",)
    return observe_atr(
        snapshot,
        series,
        provenance,
        calendar,
        observed_session,
        as_of=as_of,
        calculated_at=calculated_at,
        checkpoint=checkpoint,
        checkpoint_id=checkpoint_id,
        checkpoint_rejections=rejections,
    )
