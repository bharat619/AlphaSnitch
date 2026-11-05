"""Offline descriptive daily observations over authenticated retained evidence.

No trading thresholds, provider calls, database writes or Phase 1 schema changes.
"""

import json
from datetime import date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from fractions import Fraction
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, TypeAdapter, ValidationError, model_validator

from stock_analyser.cache import digest, load_snapshot, verified
from stock_analyser.calendars import CutoffPolicy, HistoryResolver, SessionCalendar
from stock_analyser.contracts import (
    Contract,
    FeedCoverage,
    HistoryRequirement,
    Instrument,
    MarketContext,
    PositiveInt,
    PriceBar,
    Text,
)
from stock_analyser.persistence.records import SnapshotReference

Feature = Literal[
    "prior_high", "prior_mean_volume", "relative_volume", "close_distance", "close_above_prior_high"
]


class ObservationSpec(Contract):
    """Lookbacks and full-session duration must be chosen explicitly by the caller."""

    feature_id: Feature
    formula_version: Literal["prior-session-1"] = "prior-session-1"
    implementation_version: Literal["decimal-fraction-1"] = "decimal-fraction-1"
    price_lookback: PositiveInt
    volume_lookback: PositiveInt
    regular_session_seconds: PositiveInt
    timeframe: Literal["daily"] = "daily"
    price_basis: Literal["split"] = "split"
    volume_scope: Literal["full-regular-sessions-only-1"] = "full-regular-sessions-only-1"
    division_digits: Literal[50] = 50
    comparison_tolerance: Literal["0"] = "0"

    @property
    def units(self) -> str:
        return {
            "prior_high": "quote_currency",
            "close_distance": "quote_currency",
            "prior_mean_volume": "shares",
            "relative_volume": "ratio",
            "close_above_prior_high": "boolean",
        }[self.feature_id]


class PublicationEvidence(Contract):
    session: date
    claimed_at: AwareDatetime | None = None
    verified_at: AwareDatetime | None = None
    # An audit reference is required for a verified claim. Mere claimed_at is insufficient.
    verification_reference: Text | None = None

    @model_validator(mode="after")
    def proof_required(self):
        if self.verified_at is not None and self.verification_reference is None:
            raise ValueError("Verified publication requires an audit reference")
        return self


class InputProvenance(Contract):
    """Explicit supplemental assertions; hash is retained with every observation.

    Existing packets do not assert a verified volume convention or publication.
    Supplying this sidecar never clears an existing PUBLICATION_UNVERIFIED blocker.
    """

    snapshot_sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    volume_adjustment: Literal["split_consistent", "unverified"]
    volume_verification_reference: Text | None = None
    publications: tuple[PublicationEvidence, ...] = ()

    @model_validator(mode="after")
    def consistent(self):
        if self.volume_adjustment == "split_consistent" and not self.volume_verification_reference:
            raise ValueError("Verified volume convention requires an audit reference")
        if len({p.session for p in self.publications}) != len(self.publications):
            raise ValueError("Duplicate publication provenance")
        return self


class BarReference(Contract):
    session: date
    source_row: int
    source_sha256: Text
    row_sha256: Text
    closes_at: AwareDatetime
    retrieved_at: AwareDatetime
    publication: PublicationEvidence | None
    first_proven_at: AwareDatetime


class Observation(Contract):
    specification: ObservationSpec
    units: Text
    specification_sha256: Text
    instrument: Instrument
    market: MarketContext
    provider: Text
    feed: FeedCoverage
    price_basis: Text
    volume_basis: Text
    snapshot_id: UUID
    snapshot_sha256: Text
    provenance: InputProvenance
    provenance_sha256: Text
    calendar_data_version: Text
    observed_session: date
    as_of: AwareDatetime
    calculated_at: AwareDatetime
    available_at: AwareDatetime | None
    historical_availability: Literal["verified", "unknown"]
    prior_sessions: tuple[date, ...]
    contributors: tuple[BarReference, ...]
    status: Literal["AVAILABLE", "UNAVAILABLE"]
    reasons: tuple[Text, ...]
    value: Annotated[Decimal, Field(allow_inf_nan=False)] | bool | None

    @model_validator(mode="after")
    def consistent(self):
        if (self.status == "AVAILABLE") != (self.value is not None):
            raise ValueError("Available observations require a value")
        if (self.status == "UNAVAILABLE") != bool(self.reasons):
            raise ValueError("Unavailable observations require reasons")
        if self.status == "AVAILABLE" and (
            self.available_at is None or self.available_at > self.as_of
        ):
            raise ValueError("Observation precedes required inputs")
        if self.calculated_at < self.as_of:
            raise ValueError("Calculation time precedes evaluation as-of")
        return self

    def semantic_bytes(self) -> bytes:
        """Run time is separate; all input, specification and availability lineage remains."""
        from stock_analyser.collection import encode

        return encode(self.model_dump(mode="json", exclude={"calculated_at"}))


def _decimal(value: Fraction) -> Decimal:
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        return Decimal(value.numerator) / Decimal(value.denominator)


def observe(
    snapshot: SnapshotReference,
    specification: ObservationSpec,
    provenance: InputProvenance,
    calendar: SessionCalendar,
    observed_session: date,
    *,
    as_of: datetime,
    calculated_at: datetime,
) -> Observation:
    """Validate the entire hash chain before field-local raw-row interpretation.

    Integrity failures raise ValueError/OSError, never return salvaged observations.
    Calendar failures return explicit unavailability. Future raw rows are ignored
    only after authenticating the whole packet, and cannot contribute to t.
    """
    as_of = TypeAdapter(AwareDatetime).validate_python(as_of)
    calculated_at = TypeAdapter(AwareDatetime).validate_python(calculated_at)
    packet = load_snapshot(snapshot)
    meta = packet.metadata
    spec = specification
    volume = spec.feature_id in ("prior_mean_volume", "relative_volume")
    needs_current = spec.feature_id not in ("prior_high", "prior_mean_volume")
    lookback = spec.volume_lookback if volume else spec.price_lookback
    reasons = []
    if provenance.snapshot_sha256 != snapshot.sha256:
        reasons.append("INCOMPATIBLE_PROVENANCE")
    source = next((s for s in meta.sources if s.reference == packet.raw_reference), None)
    if source is None:
        raise ValueError("Packet raw source is not authenticated by its metadata")
    # Use the verified bytes, not packet.bars: legacy normalization drops OHLC
    # when volume alone is missing. No local finding can excuse a broken hash.
    raw = verified(source.reference, source.sha256)
    resolution = packet.resolution
    if (
        resolution.market_id != meta.market.market_id
        or resolution.market_configuration_version != meta.market.configuration_version
        or resolution.venue != meta.instrument.venue
        or resolution.calendar not in meta.market.calendars
        or resolution.timezone != meta.market.timezone
        or resolution.requirement.adjustment != meta.adjustment
        or packet.response.get("retrieved_at")
        not in (
            source.retrieved_at.isoformat().replace("+00:00", "Z"),
            source.retrieved_at.isoformat(),
        )
        or meta.timeframe != "daily"
        or resolution.requirement.timeframe != "daily"
        or packet.response.get("provider") != source.provider
        or packet.response.get("provider_symbol") != source.provider_symbol
        or packet.response.get("feed") != meta.feed.model_dump(mode="json")
    ):
        reasons.append("INCOMPATIBLE_IDENTITY_OR_FEED")
    if meta.adjustment != "split" or packet.response.get("adjustment") != "split":
        reasons.append("INCOMPATIBLE_PRICE_BASIS")
    if (
        volume
        and meta.feed.kind != "synthetic"
        and (meta.market.market_id != "US" or spec.regular_session_seconds != 23400)
    ):
        reasons.append("UNSUPPORTED_SESSION_SCOPE")
    if volume and provenance.volume_adjustment != "split_consistent":
        reasons.append("UNVERIFIED_VOLUME_BASIS")

    sessions = ()
    prior = ()
    version = "unsupported"
    try:
        version = calendar.data_version(meta.market, meta.instrument.venue)
        if version != resolution.calendar_data_version:
            reasons.append("INCOMPATIBLE_CALENDAR_VERSION")
        current = calendar.schedule(
            meta.market, meta.instrument.venue, observed_session, observed_session
        )
        if len(current) != 1 or current[0].session != observed_session:
            raise ValueError("Assessment is not a unique calendar session")
        if current[0].closes_at > as_of:
            reasons.append("SESSION_INCOMPLETE")
        resolved = HistoryResolver(calendar, CutoffPolicy(daily_grace_seconds=0)).resolve(
            meta.market,
            meta.instrument.venue,
            HistoryRequirement(timeframe="daily", minimum_bars=lookback + 1),
            current[0].closes_at,
        )
        prior = tuple(p.session_end for p in resolved.periods if p.session_end < observed_session)
        if len(prior) != lookback:
            reasons.append("INSUFFICIENT_HISTORY")
        labels = (*prior, observed_session) if needs_current else prior
        sessions = calendar.schedule(
            meta.market,
            meta.instrument.venue,
            resolved.history_start or observed_session,
            observed_session,
        )
        sessions = tuple(s for s in sessions if s.session in labels)
        if any(s.closes_at > as_of for s in sessions):
            reasons.append("SESSION_INCOMPLETE")
        if volume and any(
            (s.closes_at - s.opens_at).total_seconds() != spec.regular_session_seconds
            for s in sessions
        ):
            reasons.append("SHORTENED_OR_INCOMPATIBLE_SESSION")
    except (ValueError, KeyError, OverflowError):
        reasons.append("UNSUPPORTED_CALENDAR")
        labels = ()

    try:
        rows = json.loads(raw, parse_float=Decimal, parse_constant=Decimal)["bars"]
        if not isinstance(rows, list):
            raise ValueError
        # Invalid labels cannot be safely assigned to a field-local session.
        dated = [(date.fromisoformat(row["session"]), i, row) for i, row in enumerate(rows)]
    except (ValueError, TypeError, KeyError):
        dated = []
        reasons.append("INVALID_PAYLOAD")
    if len({d for d, _, _ in dated if d in labels}) < len(labels):
        reasons.append("INSUFFICIENT_HISTORY")
    refs, prices, volumes = [], {}, {}
    pubs = {p.session: p for p in provenance.publications}
    schedule = {s.session: s for s in sessions}
    unknown = not labels
    for day in labels:
        matching = [(i, row) for session, i, row in dated if session == day]
        if len(matching) != 1:
            unknown = True
            reasons.append("MISSING_SESSION" if not matching else "DUPLICATE_SESSION")
            continue
        index, row = matching[0]
        session = schedule[day]
        publication = pubs.get(day)
        proven = source.retrieved_at
        if publication is None or publication.verified_at is None:
            unknown = True
            reasons.append("PUBLICATION_UNVERIFIED")
        else:
            proven = max(proven, publication.verified_at)
            if publication.verified_at < session.closes_at:
                reasons.append("INVALID_PUBLICATION_TIME")
        proven = max(proven, session.closes_at)
        refs.append(
            BarReference(
                session=day,
                source_row=index,
                source_sha256=source.sha256,
                row_sha256=digest(json.loads(json.dumps(row, default=str))),
                closes_at=session.closes_at,
                retrieved_at=source.retrieved_at,
                publication=publication,
                first_proven_at=proven,
            )
        )
        if proven > as_of:
            reasons.append("INPUT_NOT_YET_AVAILABLE")
        try:
            if row.get("completed", True) is not True:
                raise ValueError
            timestamp = datetime.fromisoformat(row["timestamp"])
            if timestamp != session.closes_at or timestamp > source.retrieved_at:
                raise ValueError
            for key, expected in (
                ("instrument_id", str(meta.instrument.instrument_id)),
                ("provider", source.provider),
                ("feed_id", meta.feed.feed_id),
                ("adjustment", "split"),
            ):
                if key in row and row[key] != expected:
                    raise ValueError
        except (ValueError, TypeError, KeyError):
            reasons.append("INVALID_BAR_PROVENANCE")
        if volume:
            if (
                row.get("volume_adjustment", provenance.volume_adjustment)
                != provenance.volume_adjustment
            ):
                reasons.append("INCOMPATIBLE_VOLUME_BASIS")
            try:
                value = Decimal(str(row["volume"]))
                if not value.is_finite() or value < 0:
                    raise ValueError
                volumes[day] = value
            except (ValueError, TypeError, KeyError, ArithmeticError):
                reasons.append("MISSING_VOLUME" if row.get("volume") is None else "INVALID_VOLUME")
        else:
            try:
                prices[day] = PriceBar.model_validate(
                    {
                        "session": day,
                        **{k: row[k] for k in ("open", "high", "low", "close")},
                        "volume": "0",
                    }
                )
            except (ValidationError, KeyError, TypeError):
                reasons.append("INVALID_OHLC")
    # Only errors explicitly revalidated field-by-field may be localized. Unknown
    # quality failures remain packet-wide blockers, including publication uncertainty.
    local_codes = {
        "INVALID_OHLCV",
        "INVALID_TIMESTAMP",
        "MISSING_SESSION",
        "STALE_DATA",
        "INSUFFICIENT_HISTORY",
        "DUPLICATE_SESSION",
        "UNEXPECTED_SESSION",
    }
    for finding in packet.findings:
        if finding.session is not None and finding.session > observed_session:
            continue
        if finding.severity == "error" and finding.code not in local_codes:
            reasons.append(finding.code)
            if finding.code == "PUBLICATION_UNVERIFIED":
                unknown = True
    for flag in meta.quality_flags:
        if flag == "PUBLICATION_UNVERIFIED":
            reasons.append(flag)
            unknown = True
    if "PUBLICATION_UNVERIFIED" in {r.value for r in resolution.reasons}:
        reasons.append("PUBLICATION_UNVERIFIED")
        unknown = True
    value = None
    if not reasons:
        if volume:
            mean = sum((Fraction(volumes[d]) for d in prior), Fraction()) / lookback
            if spec.feature_id == "prior_mean_volume":
                value = _decimal(mean)
            elif mean == 0:
                reasons.append("ZERO_DENOMINATOR")
            else:
                value = _decimal(Fraction(volumes[observed_session]) / mean)
        else:
            high = max(prices[d].high for d in prior)
            if spec.feature_id == "prior_high":
                value = high
            elif spec.feature_id == "close_above_prior_high":
                value = prices[observed_session].close > high
            else:
                close = prices[observed_session].close
                with localcontext() as context:
                    context.prec = max(
                        50,
                        len(close.as_tuple().digits)
                        + len(high.as_tuple().digits)
                        + abs(close.adjusted() - high.adjusted())
                        + 2,
                    )
                    value = close - high
    return Observation(
        specification=spec,
        specification_sha256=digest(spec.model_dump(mode="json")),
        units=spec.units,
        instrument=meta.instrument,
        market=meta.market,
        provider=source.provider,
        feed=meta.feed,
        price_basis=meta.adjustment,
        volume_basis=provenance.volume_adjustment,
        snapshot_id=meta.snapshot_id,
        snapshot_sha256=snapshot.sha256,
        provenance=provenance,
        provenance_sha256=digest(provenance.model_dump(mode="json")),
        calendar_data_version=version,
        observed_session=observed_session,
        as_of=as_of,
        calculated_at=calculated_at,
        available_at=max((r.first_proven_at for r in refs), default=None),
        historical_availability="unknown" if unknown else "verified",
        prior_sessions=prior,
        contributors=tuple(refs),
        status="UNAVAILABLE" if reasons else "AVAILABLE",
        reasons=tuple(dict.fromkeys(reasons)),
        value=value,
    )
