"""Frozen sector enrichment, deterministic selection, and offline snapshot replay.

US-011 has not qualified live full-sector coverage. Selection is consequently a
prototype even when enumeration of the supplied population is complete.
"""

from collections import defaultdict
from datetime import date
from decimal import Context, Decimal, localcontext
from fractions import Fraction
from typing import Annotated, Literal, Self
from uuid import NAMESPACE_URL, UUID, uuid5
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, model_validator

from stock_analyser.cache import digest, load_snapshot
from stock_analyser.calendars import CutoffPolicy, HistoryResolver, ResolvedHistory
from stock_analyser.contracts import (
    Contract,
    DataAvailability,
    HistoryRequirement,
    Instrument,
    MarketContext,
    SourceRecord,
    Text,
)
from stock_analyser.persistence.records import SnapshotReference
from stock_analyser.providers import PriceHistory
from stock_analyser.universe_spike import POLICY, Enumeration, reference_exclusions

Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class SelectionPolicy(Contract):
    version: Literal["us-technology-selection-1"] = "us-technology-selection-1"
    proposal_version: Literal["us-technology-proposed-1"] = "us-technology-proposed-1"
    thresholds: Literal["evaluation_hypotheses"] = "evaluation_hypotheses"
    taxonomy_mapping: Literal["yahoo-sector-map-1"] = "yahoo-sector-map-1"
    minimum_price_usd: Money = Decimal("5")
    minimum_average_traded_value_usd: Money = Decimal("10000000")
    traded_value_sessions: Literal[60] = 60
    minimum_daily_bars: Annotated[int, Field(strict=True, ge=260)] = 260
    selection_count: Literal[10] = 10
    mandatory_history: tuple[HistoryRequirement, ...] = ()

    @model_validator(mode="after")
    def mandatory_only(self) -> Self:
        keys = [(h.timeframe, h.adjustment) for h in self.mandatory_history]
        if len(keys) != len(set(keys)) or any(not h.mandatory for h in self.mandatory_history):
            raise ValueError("Selection history must contain unique mandatory requirements")
        return self

    def requirements(self) -> tuple[HistoryRequirement, ...]:
        requirements = {(h.timeframe, h.adjustment): h for h in self.mandatory_history}
        requirements["daily", "raw"] = HistoryRequirement(
            timeframe="daily",
            adjustment="raw",
            minimum_bars=max(
                [self.minimum_daily_bars]
                + [h.required_bars for h in self.mandatory_history if h.timeframe == "daily"]
            ),
        )
        return tuple(requirements[key] for key in sorted(requirements))


class ReferenceListing(Contract):
    ticker: Text
    active: bool | None = None
    market: str | None = None
    locale: str | None = None
    type: str | None = None
    primary_exchange: str | None = None
    currency_name: str | None = None
    share_class_figi: str | None = None
    composite_figi: str | None = None
    last_updated_utc: str | None = None


class Classification(Contract):
    source: SourceRecord
    taxonomy: Literal["Yahoo sector"] = "Yahoo sector"
    original_sector: str | None = None
    original_industry: str | None = None
    effective_date: date | None = None
    status: Literal["OBSERVED", "UNKNOWN", "CONFLICTING", "FAILED"] = "UNKNOWN"

    @property
    def mapped_sector(self) -> str | None:
        if self.status != "OBSERVED":
            return None
        if self.original_sector == "Technology":
            return "technology"
        if self.original_sector in POLICY["other_known_sectors"]:
            return "other"
        return None


class UniverseCandidate(Contract):
    reference: ReferenceListing
    instrument: Instrument | None = None
    classification: Classification | None = None
    # Resolver must verify FIGI-to-UUID and effective provider symbol mappings.
    identity_verified: bool = False


class UniversePopulation(Contract):
    source: SourceRecord
    scope: Text
    enumeration_status: Literal["COMPLETE", "TRUNCATED", "FAILED"]
    enumeration_reason: Text
    reference_pages: Annotated[int, Field(strict=True, ge=0)]
    candidates: tuple[UniverseCandidate, ...]
    full_sector_accepted: Literal[False] = False

    @model_validator(mode="after")
    def unique_listings(self) -> Self:
        tickers = [c.reference.ticker for c in self.candidates]
        if len(tickers) != len(set(tickers)):
            raise ValueError("Duplicate reference tickers; enumeration must reconcile them")
        return self


class ReferenceUniverseAdapter:
    """Enrich every reference row, retaining failures instead of dropping members.

    Accepts the paginated US-011 enumeration and injected identity/profile
    resolvers. No ticker-only cross-provider identity inference is performed.
    Enrichment may use EvidenceCache.metadata; failure remains unknown.
    """

    def __init__(self, reference: Enumeration, source: SourceRecord, resolve, classify):
        self.reference, self.source = reference, source
        self.resolve, self.classify = resolve, classify

    def enumerate(self) -> UniversePopulation:
        candidates = []
        for row in sorted(self.reference.rows, key=lambda r: r["ticker"]):
            listing = ReferenceListing.model_validate(row)
            instrument, classification = None, None
            # Unsupported rows stay in the snapshot without unnecessary profile calls.
            if not reference_exclusions(row):
                try:
                    instrument = Instrument.model_validate(self.resolve(listing))
                except Exception:
                    pass
                try:
                    classification = Classification.model_validate(
                        self.classify(listing, instrument)
                    )
                except Exception:
                    pass
            candidates.append(
                UniverseCandidate(
                    reference=listing,
                    instrument=instrument,
                    classification=classification,
                    identity_verified=instrument is not None,
                )
            )
        return UniversePopulation(
            source=self.source,
            scope="active US reference query; full-sector gate blocked",
            enumeration_status=self.reference.status,
            enumeration_reason=self.reference.reason,
            reference_pages=len(self.reference.pages),
            candidates=tuple(candidates),
        )


class SelectionRequest(Contract):
    market: MarketContext
    requested_as_of: AwareDatetime
    population: UniversePopulation
    policy: SelectionPolicy = Field(default_factory=SelectionPolicy)
    cutoff_policy: CutoffPolicy = Field(default_factory=CutoffPolicy)
    histories: tuple[SnapshotReference, ...] = ()

    @model_validator(mode="after")
    def unique_histories(self) -> Self:
        if self.market.market_id != "US" or self.market.currency != "USD":
            raise ValueError("Technology selection currently supports US/USD only")
        keys = [
            (h.metadata.instrument.instrument_id, h.metadata.timeframe, h.metadata.adjustment)
            for h in self.histories
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("Choose one frozen history generation per instrument/timeframe/basis")
        return self


class TradedValue(Contract):
    session: date
    close: Money
    volume: Money
    value: Money


class CandidateDecision(Contract):
    ticker: Text
    instrument_id: UUID | None
    security_id: UUID | None
    reasons: tuple[Text, ...]
    average_traded_value: Money | None = None
    latest_price: Money | None = None
    observations: tuple[TradedValue, ...] = ()
    history_snapshot_ids: tuple[UUID, ...] = ()


class SelectionResult(Contract):
    selected: tuple[Instrument, ...]
    ranked_instrument_ids: tuple[UUID, ...]
    decisions: tuple[CandidateDecision, ...]
    shortfall: Annotated[int, Field(strict=True, ge=0, le=10)]
    enumeration_complete: bool
    unresolved_classifications: tuple[Text, ...]
    full_sector_accepted: Literal[False] = False
    scope: Literal["PROTOTYPE"] = "PROTOTYPE"


class UniverseSnapshot(Contract):
    snapshot_id: UUID
    inputs_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    request: SelectionRequest
    resolutions: tuple[ResolvedHistory, ...]
    result: SelectionResult
    implementation_version: Literal["sector-selection-1"] = "sector-selection-1"

    @model_validator(mode="after")
    def consistent_identity(self) -> Self:
        expected = selection_digest(self.request, self.resolutions)
        if self.inputs_sha256 != expected or self.snapshot_id != universe_id(expected):
            raise ValueError("Universe snapshot input identity mismatch")
        expected_keys = {
            (venue, h.timeframe, h.adjustment)
            for venue in {
                c.instrument.venue for c in self.request.population.candidates if c.instrument
            }
            & {c.venue for c in self.request.market.calendars}
            for h in self.request.policy.requirements()
        }
        keys = [
            (p.venue, p.requirement.timeframe, p.requirement.adjustment) for p in self.resolutions
        ]
        if set(keys) != expected_keys or len(keys) != len(set(keys)):
            raise ValueError("Universe snapshot is missing history resolutions")
        for p in self.resolutions:
            if (
                p.requested_as_of != self.request.requested_as_of
                or p.market_id != self.request.market.market_id
                or p.market_configuration_version != self.request.market.configuration_version
                or p.timezone != self.request.market.timezone
                or p.calendar not in self.request.market.calendars
                or p.policy != self.request.cutoff_policy
                or p.requirement not in self.request.policy.requirements()
            ):
                raise ValueError("Universe resolution differs from frozen request")
        return self


def canonical_request(request: SelectionRequest) -> SelectionRequest:
    """Input ordering is not a policy input; normalize it before freezing."""
    population = request.population.model_copy(
        update={
            "candidates": tuple(
                sorted(request.population.candidates, key=lambda c: c.reference.ticker)
            )
        }
    )
    return request.model_copy(
        update={
            "population": population,
            "histories": tuple(
                sorted(
                    request.histories,
                    key=lambda h: (
                        str(h.metadata.instrument.instrument_id),
                        h.metadata.timeframe,
                        h.metadata.adjustment,
                    ),
                )
            ),
        }
    )


def resolve_selection(request: SelectionRequest, resolver: HistoryResolver):
    venues = {c.instrument.venue for c in request.population.candidates if c.instrument}
    supported = {c.venue for c in request.market.calendars}
    return tuple(
        resolver.resolve(request.market, venue, requirement, request.requested_as_of)
        for venue in sorted(venues & supported)
        for requirement in request.policy.requirements()
    )


def _history(reference, instrument, request, resolution):
    packet = load_snapshot(reference)
    metadata = packet.metadata
    if (
        metadata.instrument != instrument
        or metadata.market != request.market
        or metadata.availability != DataAvailability.AVAILABLE
        or metadata.feed.kind == "discovery"
        or instrument.venue not in metadata.feed.venues
        or metadata.timeframe != resolution.requirement.timeframe
        or metadata.adjustment != resolution.requirement.adjustment
        or metadata.session_cutoff != resolution.session_cutoff
        or packet.resolution.policy != resolution.policy
        or packet.resolution.calendar != resolution.calendar
        or packet.resolution.calendar_data_version != resolution.calendar_data_version
        or packet.resolution.requested_as_of > request.requested_as_of
        or any(f.severity == "error" for f in packet.findings)
        or len(resolution.periods) != resolution.requirement.required_bars
    ):
        raise ValueError("History does not satisfy frozen selection window")
    history = PriceHistory(metadata=metadata, bars=packet.bars)
    periods = {p.session_end: p for p in packet.resolution.periods}
    bars = {b.session: b for b in history.bars}
    if any(
        periods.get(p.session_end) != p or p.session_end not in bars for p in resolution.periods
    ):
        raise ValueError("History is missing expected sessions")
    return packet, tuple(bars[p.session_end] for p in resolution.periods)


def evaluate(
    request: SelectionRequest, resolutions: tuple[ResolvedHistory, ...]
) -> SelectionResult:
    """No providers, clocks or database: only frozen inputs and verified cache files."""
    candidates = request.population.candidates
    policy = request.policy
    reasons = {
        c.reference.ticker: reference_exclusions(c.reference.model_dump()) for c in candidates
    }
    unresolved = []
    # Conflicting identities block every affected member, not just the later row.
    identities = defaultdict(set)
    security_ids = defaultdict(set)
    figis = defaultdict(set)
    instrument_ids = defaultdict(set)
    sectors = defaultdict(set)
    for c in candidates:
        r, i = c.reference, c.instrument
        if r.composite_figi:
            identities[r.composite_figi].add((r.share_class_figi, r.primary_exchange, r.ticker))
        if i:
            security_ids[r.share_class_figi].add(i.security_id)
            figis[i.security_id].add(r.share_class_figi)
            instrument_ids[i.instrument_id].add((r.composite_figi, r.ticker))
        if r.share_class_figi and c.classification and c.classification.mapped_sector:
            sectors[r.share_class_figi].add(c.classification.mapped_sector)
    groups = defaultdict(list)
    for c in candidates:
        r, i, classification = c.reference, c.instrument, c.classification
        why = reasons[r.ticker]
        if r.composite_figi and len(identities[r.composite_figi]) > 1:
            why.append("CONFLICTING_LISTING_IDENTITY")
        if i is None or not c.identity_verified:
            why.append("UNVERIFIED_INSTRUMENT_IDENTITY")
        elif (
            len(security_ids[r.share_class_figi]) > 1
            or len(figis[i.security_id]) > 1
            or len(instrument_ids[i.instrument_id]) > 1
            or i.symbol != r.ticker
            or i.venue != r.primary_exchange
            or i.series != "common"
        ):
            why.append("CONFLICTING_INSTRUMENT_IDENTITY")
        if i:
            try:
                request.market.validate_instrument(i)
            except ValueError:
                why.append("UNSUPPORTED_MARKET_OR_CALENDAR")
        if not why:
            groups[r.share_class_figi].append(c)
        if len(sectors[r.share_class_figi]) > 1:
            unresolved.append(r.ticker)
            why.append("CONFLICTING_SECURITY_CLASSIFICATION")
        if reference_exclusions(r.model_dump()):
            continue
        if classification is not None and i is not None:
            mapping_date = (
                classification.effective_date
                or classification.source.retrieved_at.astimezone(
                    ZoneInfo(request.market.timezone)
                ).date()
            )
            try:
                symbol = i.provider_symbol(classification.source.provider, mapping_date)
                if symbol != classification.source.provider_symbol:
                    raise ValueError("Classification belongs to another provider symbol")
            except ValueError:
                unresolved.append(r.ticker)
                why.append("UNVERIFIED_CLASSIFICATION_IDENTITY")
        if classification is None or classification.mapped_sector is None:
            unresolved.append(r.ticker)
            why.append("UNKNOWN_CLASSIFICATION")
        elif (
            classification.effective_date
            and classification.effective_date > request.requested_as_of.date()
        ):
            unresolved.append(r.ticker)
            why.append("FUTURE_CLASSIFICATION")
        elif classification.mapped_sector != "technology":
            why.append("OUTSIDE_SECTOR")
    for group in groups.values():
        ordered = sorted(
            group,
            key=lambda c: (
                POLICY["listing_order"].index(c.reference.primary_exchange),
                c.reference.composite_figi,
                c.reference.ticker,
            ),
        )
        for c in ordered[1:]:
            reasons[c.reference.ticker].append("DUPLICATE_SECURITY")
    histories = {
        (h.metadata.instrument.instrument_id, h.metadata.timeframe, h.metadata.adjustment): h
        for h in request.histories
    }
    plans = {(p.venue, p.requirement.timeframe, p.requirement.adjustment): p for p in resolutions}
    decisions, eligible = [], []
    raw_feeds = set()
    raw_windows = set()
    for c in candidates:
        i, why = c.instrument, reasons[c.reference.ticker]
        observations, used = (), []
        average, price = None, None
        if not why:
            raw_bars = None
            for requirement in policy.requirements():
                key = (requirement.timeframe, requirement.adjustment)
                reference = histories.get((i.instrument_id, *key))
                plan = plans.get((i.venue, *key))
                if reference is None or plan is None:
                    why.append("MISSING_MANDATORY_HISTORY")
                    continue
                try:
                    packet, bars = _history(reference, i, request, plan)
                    used.append(packet.metadata.snapshot_id)
                    if key == ("daily", "raw"):
                        raw_bars = bars[-60:]
                        raw_windows.add(tuple(b.session for b in raw_bars))
                        raw_feeds.add(digest(packet.metadata.feed.model_dump(mode="json")))
                except (OSError, ValueError):
                    why.append("INVALID_MANDATORY_HISTORY")
            if raw_bars is not None:
                # Isolate precision, rounding and traps from the caller's Decimal context.
                precision = max(
                    80,
                    max(
                        len(b.close.as_tuple().digits) + len(b.volume.as_tuple().digits)
                        for b in raw_bars
                    ),
                )
                with localcontext(Context(prec=precision)):
                    observations = tuple(
                        TradedValue(
                            session=b.session,
                            close=b.close,
                            volume=b.volume,
                            value=b.close * b.volume,
                        )
                        for b in raw_bars
                    )
                    total = sum((Fraction(o.value) for o in observations), Fraction(0))
                    average = Decimal(total.numerator) / Decimal(total.denominator * 60)
                price = raw_bars[-1].close
                if price < policy.minimum_price_usd:
                    why.append("BELOW_MINIMUM_PRICE")
                if total < Fraction(policy.minimum_average_traded_value_usd) * 60:
                    why.append("BELOW_MINIMUM_LIQUIDITY")
            if not why:
                eligible.append((average, i))
        decisions.append(
            CandidateDecision(
                ticker=c.reference.ticker,
                instrument_id=i.instrument_id if i else None,
                security_id=i.security_id if i else None,
                reasons=tuple(sorted(set(why))),
                average_traded_value=average,
                latest_price=price,
                observations=observations,
                history_snapshot_ids=tuple(used),
            )
        )
    if len(raw_feeds) > 1 or len(raw_windows) > 1:
        eligible = []
        decisions = [
            d.model_copy(update={"reasons": (*d.reasons, "INCOMPARABLE_LIQUIDITY_INPUTS")})
            if d.average_traded_value is not None
            else d
            for d in decisions
        ]
    # Sort by sums rather than a rounded mean; every candidate has exactly 60 terms.
    totals = {
        d.instrument_id: sum((Fraction(o.value) for o in d.observations), Fraction(0))
        for d in decisions
        if not d.reasons
    }
    eligible.sort(key=lambda pair: str(pair[1].instrument_id))
    eligible.sort(key=lambda pair: totals[pair[1].instrument_id], reverse=True)
    selected = tuple(i for _, i in eligible[: policy.selection_count])
    return SelectionResult(
        selected=selected,
        ranked_instrument_ids=tuple(i.instrument_id for _, i in eligible),
        decisions=tuple(decisions),
        shortfall=policy.selection_count - len(selected),
        enumeration_complete=request.population.enumeration_status == "COMPLETE",
        unresolved_classifications=tuple(sorted(set(unresolved))),
    )


def select_universe(request: SelectionRequest, resolver: HistoryResolver) -> UniverseSnapshot:
    request = canonical_request(SelectionRequest.model_validate(request.model_dump(mode="json")))
    if resolver.policy != request.cutoff_policy:
        raise ValueError("Resolver differs from frozen cutoff policy")
    resolutions = resolve_selection(request, resolver)
    return freeze_selection(request, resolutions)


def selection_digest(request, resolutions) -> str:
    return digest(
        {
            "request": request.model_dump(mode="json"),
            "resolutions": [p.model_dump(mode="json") for p in resolutions],
            "implementation": "sector-selection-1",
        }
    )


def universe_id(inputs_sha256):
    return uuid5(NAMESPACE_URL, f"alphasnitch:universe:{inputs_sha256}")


def freeze_selection(request, resolutions) -> UniverseSnapshot:
    inputs_sha256 = selection_digest(request, resolutions)
    return UniverseSnapshot(
        snapshot_id=universe_id(inputs_sha256),
        inputs_sha256=inputs_sha256,
        request=request,
        resolutions=resolutions,
        result=evaluate(request, resolutions),
    )


def replay_universe(snapshot: UniverseSnapshot) -> UniverseSnapshot:
    replayed = freeze_selection(snapshot.request, snapshot.resolutions)
    if replayed != snapshot:
        raise ValueError("Universe replay differs from the saved selection or evidence")
    return replayed


class FrozenUniverseAdapter:
    """UniverseProvider for an explicit prototype; preserves cached ranking histories."""

    def __init__(self, snapshot: UniverseSnapshot):
        self.snapshot = replay_universe(snapshot)

    def instruments(self, market, as_of):
        if market != self.snapshot.request.market or as_of != self.snapshot.request.requested_as_of:
            raise ValueError("Frozen universe differs from requested market/cutoff")
        return self.snapshot.result.selected

    def histories(self, instrument_id: UUID) -> tuple[SnapshotReference, ...]:
        if instrument_id not in {i.instrument_id for i in self.snapshot.result.selected}:
            raise ValueError("Instrument was not selected")
        used = next(
            d.history_snapshot_ids
            for d in self.snapshot.result.decisions
            if d.instrument_id == instrument_id and not d.reasons
        )
        return tuple(h for h in self.snapshot.request.histories if h.metadata.snapshot_id in used)


def save_universe(database, directory, snapshot: UniverseSnapshot):
    """Commit immutable DB inputs first; an interrupted file publication is retryable."""
    from stock_analyser.collection import encode, immutable_write

    replay_universe(snapshot)
    with database.write() as repo:
        repo.put_universe(snapshot)
    path = directory / "universes" / str(snapshot.snapshot_id) / "snapshot.json"
    immutable_write(path, encode(snapshot.model_dump(mode="json")))
    return path
