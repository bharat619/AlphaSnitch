"""Version 1 domain contracts shared by providers, workflows and report readers."""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, pattern=r"\S")]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1"] = "1"


class SymbolMapping(Contract):
    provider: Text
    symbol: Text
    effective_from: date
    effective_to: date | None = None

    @model_validator(mode="after")
    def valid_interval(self) -> Self:
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("Symbol intervals are half-open and must have positive duration")
        return self

    def contains(self, session: date) -> bool:
        return self.effective_from <= session and (
            self.effective_to is None or session < self.effective_to
        )


class ExternalIdentifier(Contract):
    scheme: Text
    value: Text


class Security(Contract):
    security_id: UUID
    name: Text
    security_type: Text = "equity"
    external_ids: tuple[ExternalIdentifier, ...] = ()


class Instrument(Contract):
    instrument_id: UUID
    security_id: UUID
    market_id: Text
    venue: Text
    symbol: Text
    series: Text = "common"
    quote_currency: Currency
    quantity_increment: Decimal = Field(
        default=Decimal("0.00000001"), gt=0, max_digits=28, decimal_places=8
    )
    price_increment: Decimal = Field(
        default=Decimal("0.00000001"), gt=0, max_digits=28, decimal_places=8
    )
    provider_symbols: tuple[SymbolMapping, ...] = ()

    @model_validator(mode="after")
    def nonoverlapping_symbols(self) -> Self:
        for index, left in enumerate(self.provider_symbols):
            for right in self.provider_symbols[index + 1 :]:
                if (
                    left.provider == right.provider
                    and (left.effective_to is None or right.effective_from < left.effective_to)
                    and (right.effective_to is None or left.effective_from < right.effective_to)
                ):
                    raise ValueError("Provider symbol intervals overlap")
        return self

    def provider_symbol(self, provider: str, session: date) -> str:
        for mapping in self.provider_symbols:
            if mapping.provider == provider and mapping.contains(session):
                return mapping.symbol
        raise ValueError("No provider symbol mapping for this session")


class CalendarReference(Contract):
    venue: Text
    reference: Text
    version: Text


class FeedCoverage(Contract):
    feed_id: Text
    kind: Literal["consolidated", "venue", "synthetic", "discovery"]
    venues: tuple[Text, ...] = Field(min_length=1)


class ProviderCapabilities(Contract):
    provider: Text
    daily_prices: bool = False
    weekly_prices: bool = False
    corporate_actions: bool = False
    news: bool = False
    universe: bool = False
    point_in_time_universe: bool = False
    max_history_days: PositiveInt | None = None


class ProviderRoute(Contract):
    role: Literal["prices", "long_history_prices", "universe", "research", "corporate_actions"]
    provider: Text
    feed: FeedCoverage


class BenchmarkMapping(Contract):
    key: Text
    symbol: Text
    venue: Text
    currency: Currency


class MarketContext(Contract):
    market_id: Text
    configuration_version: Text
    currency: Currency
    timezone: Text
    calendars: tuple[CalendarReference, ...] = Field(min_length=1)
    routes: tuple[ProviderRoute, ...] = Field(min_length=1)
    capabilities: tuple[ProviderCapabilities, ...] = Field(min_length=1)
    benchmarks: tuple[BenchmarkMapping, ...] = Field(min_length=1)
    strategy_profile: Text

    @model_validator(mode="after")
    def unique_configuration(self) -> Self:
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown market timezone") from None
        for values in (
            [c.venue for c in self.calendars],
            [r.role for r in self.routes],
            [c.provider for c in self.capabilities],
            [b.key for b in self.benchmarks],
        ):
            if len(values) != len(set(values)):
                raise ValueError("Duplicate market configuration entry")
        providers = {c.provider for c in self.capabilities}
        if any(r.provider not in providers for r in self.routes):
            raise ValueError("Route has no declared provider capabilities")
        return self

    def route(self, role: str) -> ProviderRoute:
        for route in self.routes:
            if route.role == role:
                return route
        raise ValueError("No provider route for this role")

    def validate_instrument(self, instrument: Instrument) -> None:
        if instrument.market_id != self.market_id or instrument.quote_currency != self.currency:
            raise ValueError("Instrument market or currency does not match context")
        if instrument.venue not in {c.venue for c in self.calendars}:
            raise ValueError("Instrument venue has no calendar in context")


class HistoryRequirement(Contract):
    timeframe: Literal["daily", "weekly"]
    minimum_bars: PositiveInt
    warmup_bars: Annotated[int, Field(strict=True, ge=0)] = 0
    mandatory: bool = True
    adjustment: Literal["raw", "split", "total_return"] = "split"

    @property
    def required_bars(self) -> int:
        return self.minimum_bars + self.warmup_bars


class AnalysisRequest(Contract):
    request_id: UUID
    mode: Literal["buy", "sell"]
    market_id: Text
    instrument_ids: tuple[UUID, ...] = ()
    sector: Text | None = None
    horizon: Literal["swing"] = "swing"
    requested_as_of: AwareDatetime
    strategy_version: Text
    portfolio_revision: Text | None = None
    history: tuple[HistoryRequirement, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def valid_scope(self) -> Self:
        if bool(self.instrument_ids) == (self.sector is not None):
            raise ValueError("Specify exactly one of instruments or sector")
        if len(set(self.instrument_ids)) != len(self.instrument_ids):
            raise ValueError("Duplicate instrument IDs")
        if self.mode == "sell" and (self.sector is not None or self.portfolio_revision is None):
            raise ValueError("Sell analysis requires instruments and a portfolio revision")
        if len({h.timeframe for h in self.history}) != len(self.history):
            raise ValueError("Duplicate history timeframes")
        return self


class OperationalStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class DataAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    PARTIAL = "PARTIAL"
    UNAVAILABLE = "UNAVAILABLE"
    NOT_REQUESTED = "NOT_REQUESTED"


class StrategyVerdict(StrEnum):
    NOT_RUN = "NOT_RUN"
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"
    REJECT = "REJECT"
    ABSTAIN = "ABSTAIN"


class StrategyStages(Contract):
    """Foundation stages cannot claim to have executed a strategy."""

    indicators: Literal["NOT_RUN"] = "NOT_RUN"
    setups: Literal["NOT_RUN"] = "NOT_RUN"
    debate: Literal["NOT_RUN"] = "NOT_RUN"
    verdict: Literal[StrategyVerdict.NOT_RUN] = StrategyVerdict.NOT_RUN


class SourceRecord(Contract):
    provider: Text
    provider_symbol: Text
    retrieved_at: AwareDatetime
    reference: Text
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    # Inclusive local session dates; required for price evidence, optional for news/actions.
    session_start: date | None = None
    session_end: date | None = None

    @model_validator(mode="after")
    def valid_sessions(self) -> Self:
        if (self.session_start is None) != (self.session_end is None):
            raise ValueError("Source session interval requires both endpoints")
        if self.session_start is not None and self.session_end < self.session_start:
            raise ValueError("Source session interval is reversed")
        return self


class EvidenceMetadata(Contract):
    snapshot_id: UUID
    instrument: Instrument
    market: MarketContext
    currency: Currency
    session_cutoff: AwareDatetime
    feed: FeedCoverage
    timeframe: Literal["daily", "weekly"]
    adjustment: Literal["raw", "split", "total_return"]
    availability: DataAvailability
    requested_bars: PositiveInt
    returned_bars: Annotated[int, Field(strict=True, ge=0)]
    sources: tuple[SourceRecord, ...] = ()
    quality_flags: tuple[Text, ...] = ()
    reason: Text | None = None

    @model_validator(mode="after")
    def consistent_evidence(self) -> Self:
        self.market.validate_instrument(self.instrument)
        if self.currency != self.instrument.quote_currency:
            raise ValueError("Evidence currency does not match instrument")
        providers = {
            r.provider
            for r in self.market.routes
            if r.feed == self.feed and r.role in ("prices", "long_history_prices")
        }
        if not providers:
            raise ValueError("Evidence feed is not configured for market prices")
        session = self.session_cutoff.astimezone(ZoneInfo(self.market.timezone)).date()
        for source in self.sources:
            if source.provider not in providers:
                raise ValueError("Evidence source does not match configured price provider")
            if source.session_start is None or source.session_end is None:
                raise ValueError("Price source requires a covered session interval")
            if source.session_end > session:
                raise ValueError("Source session interval exceeds evidence cutoff")
            if not any(
                mapping.provider == source.provider
                and mapping.symbol == source.provider_symbol
                and mapping.contains(source.session_start)
                and mapping.contains(source.session_end)
                for mapping in self.instrument.provider_symbols
            ):
                raise ValueError("Evidence source symbol does not match effective provider mapping")
        if self.returned_bars > 0 and not self.sources:
            raise ValueError("Returned bars require source provenance")
        if self.availability == DataAvailability.AVAILABLE and (
            self.returned_bars < self.requested_bars or not self.sources
        ):
            raise ValueError("Available evidence requires sufficient bars and source provenance")
        if self.availability == DataAvailability.UNAVAILABLE and self.returned_bars != 0:
            raise ValueError("Unavailable evidence cannot contain bars")
        if self.availability == DataAvailability.NOT_REQUESTED and self.returned_bars:
            raise ValueError("Unrequested evidence cannot contain bars")
        if self.availability != DataAvailability.AVAILABLE and self.reason is None:
            raise ValueError("Missing or partial evidence requires a reason")
        return self


class ReportArtifact(Contract):
    artifact_id: UUID
    run_id: UUID
    format: Literal["json", "markdown"]
    reference: Text
    sha256: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    created_at: AwareDatetime


def validate_request_coverage(
    request: AnalysisRequest,
    evidence: tuple[EvidenceMetadata, ...],
    resolved_instrument_ids: tuple[UUID, ...],
) -> None:
    """Require every resolved listing's mandatory history before claiming AVAILABLE.

    Counts belong to a single evidence snapshot; overlapping snapshots cannot be
    added together to manufacture sufficient history.
    """
    instrument_ids = request.instrument_ids or resolved_instrument_ids
    if not instrument_ids:
        raise ValueError("Available sector report requires resolved instrument IDs")
    for instrument_id in instrument_ids:
        if not any(e.instrument.instrument_id == instrument_id for e in evidence):
            raise ValueError("Available report is missing a requested instrument")
        for requirement in request.history:
            if requirement.mandatory and not any(
                e.instrument.instrument_id == instrument_id
                and e.timeframe == requirement.timeframe
                and e.adjustment == requirement.adjustment
                and e.availability == DataAvailability.AVAILABLE
                and e.returned_bars >= requirement.required_bars
                for e in evidence
            ):
                raise ValueError("Available report does not satisfy mandatory history requirements")


class DataReadinessReport(Contract):
    run_id: UUID
    request: AnalysisRequest
    status: OperationalStatus = OperationalStatus.NOT_RUN
    availability: DataAvailability = DataAvailability.NOT_REQUESTED
    stages: StrategyStages = Field(default_factory=StrategyStages)
    evidence: tuple[EvidenceMetadata, ...] = ()
    resolved_instrument_ids: tuple[UUID, ...] = ()
    artifacts: tuple[ReportArtifact, ...] = ()
    reason: Text | None = None

    @model_validator(mode="after")
    def consistent_scope(self) -> Self:
        if len(set(self.resolved_instrument_ids)) != len(self.resolved_instrument_ids):
            raise ValueError("Duplicate resolved instrument IDs")
        if self.request.instrument_ids and self.resolved_instrument_ids:
            if set(self.resolved_instrument_ids) != set(self.request.instrument_ids):
                raise ValueError("Resolved instruments differ from requested instruments")
        scope = self.request.instrument_ids or self.resolved_instrument_ids
        for evidence in self.evidence:
            if evidence.market.market_id != self.request.market_id:
                raise ValueError("Report evidence belongs to another market")
            if scope and evidence.instrument.instrument_id not in scope:
                raise ValueError("Report evidence is outside requested instruments")
            if evidence.session_cutoff > self.request.requested_as_of:
                raise ValueError("Evidence cutoff is after requested as-of time")
        if self.availability == DataAvailability.AVAILABLE and (
            not self.evidence
            or any(e.availability != DataAvailability.AVAILABLE for e in self.evidence)
        ):
            raise ValueError("Available report requires available evidence")
        if self.availability == DataAvailability.AVAILABLE:
            validate_request_coverage(self.request, self.evidence, self.resolved_instrument_ids)
        if any(a.run_id != self.run_id for a in self.artifacts):
            raise ValueError("Artifact belongs to another run")
        return self


class PriceBar(Contract):
    session: date
    open: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    high: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    low: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    close: Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
    volume: Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]

    @model_validator(mode="after")
    def valid_ohlc(self) -> Self:
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Inconsistent OHLC prices")
        return self
