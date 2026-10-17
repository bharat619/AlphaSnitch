"""Provider boundaries. Implementations must return normalized versioned contracts."""

from datetime import date
from typing import Protocol, Self
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, model_validator

from stock_analyser.contracts import (
    Contract,
    EvidenceMetadata,
    HistoryRequirement,
    Instrument,
    MarketContext,
    PriceBar,
    SourceRecord,
    Text,
)


class PriceHistory(Contract):
    metadata: EvidenceMetadata
    bars: tuple[PriceBar, ...]

    @model_validator(mode="after")
    def consistent_bars(self) -> Self:
        if len(self.bars) != self.metadata.returned_bars:
            raise ValueError("Returned bar count does not match metadata")
        sessions = [b.session for b in self.bars]
        if sessions != sorted(set(sessions)):
            raise ValueError("Bars must have unique ascending sessions")
        cutoff_date = self.metadata.session_cutoff.astimezone(
            ZoneInfo(self.metadata.market.timezone)
        ).date()
        if any(s > cutoff_date for s in sessions):
            raise ValueError("Bar session exceeds cutoff")
        if any(
            not any(
                source.session_start <= session <= source.session_end
                for source in self.metadata.sources
            )
            for session in sessions
        ):
            raise ValueError("Bar session has no source provenance")
        return self


class ResearchItem(Contract):
    title: Text
    published_at: AwareDatetime
    source: SourceRecord


class CorporateAction(Contract):
    kind: Text
    effective_date: date
    source: SourceRecord


class PriceDataProvider(Protocol):
    def history(
        self,
        instrument: Instrument,
        market: MarketContext,
        requirement: HistoryRequirement,
        cutoff: AwareDatetime,
    ) -> PriceHistory: ...


class UniverseProvider(Protocol):
    def instruments(
        self, market: MarketContext, as_of: AwareDatetime
    ) -> tuple[Instrument, ...]: ...


class ResearchProvider(Protocol):
    def research(
        self,
        instrument: Instrument,
        market: MarketContext,
        cutoff: AwareDatetime,
    ) -> tuple[ResearchItem, ...]: ...


class CorporateActionsProvider(Protocol):
    def corporate_actions(
        self,
        instrument: Instrument,
        market: MarketContext,
        cutoff: AwareDatetime,
    ) -> tuple[CorporateAction, ...]: ...


class MarketCalendarProvider(Protocol):
    def latest_completed_session(
        self,
        market: MarketContext,
        venue: str,
        as_of: AwareDatetime,
    ) -> AwareDatetime: ...


class LLMResponse(Contract):
    provider: Text
    model: Text
    content: Text


class LLMProvider(Protocol):
    def complete(self, prompt: str) -> LLMResponse: ...
