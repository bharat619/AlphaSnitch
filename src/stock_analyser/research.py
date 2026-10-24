"""Locally retained discovery metadata and explicitly unresolved event evidence.

No article bodies, provider summaries, sentiment prose, or model calls belong here.
News association and material relevance are independent facts.
"""

import re
from datetime import date
from typing import Literal, Self
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from stock_analyser.contracts import Contract, Instrument, Text

FORWARD_EARNINGS_GAP = (
    "Forward earnings coverage is unverified; news discovery is not an earnings calendar."
)
EARNINGS_FOLLOW_UP = (
    "Before strategy acceptance, check the issuer investor-relations earnings calendar/release "
    "and corroborate the next announcement with an independent dated source; retain both URLs, "
    "retrieval times, original timezones and exchange-local dates, and resolve disagreements."
)


def source_url(value: str) -> str:
    """Keep public article locations, removing credential/tracking queries and fragments."""
    url = urlsplit(value)
    if url.scheme not in ("https", "http") or not url.hostname or url.username or url.password:
        raise ValueError("Research requires a public HTTP(S) source URL")
    if any(c.isspace() or ord(c) < 32 for c in value):
        raise ValueError("Invalid source URL")
    query = urlencode(
        [
            (key, value)
            for key, value in parse_qsl(url.query, keep_blank_values=True)
            if not re.search(
                r"key|token|secret|password|credential|authorization|signature", key, re.I
            )
            and not key.lower().startswith("utm_")
            and key.lower() not in ("fbclid", "gclid")
        ]
    )
    return urlunsplit((url.scheme, url.netloc.lower(), url.path, query, ""))


class NewsItem(Contract):
    evidence_id: UUID
    provider_id: Text
    source_url: Text
    published_at: AwareDatetime
    retrieved_at: AwareDatetime
    ticker_associated: bool
    relevance: Literal["UNASSESSED", "RELEVANT", "IRRELEVANT"] = "UNASSESSED"
    relevance_reason: Text = "Ticker association does not establish material relevance."
    relevance_method: Literal["unreviewed", "synthetic-review"] = "unreviewed"

    _url = field_validator("source_url")(source_url)

    @model_validator(mode="after")
    def valid_item(self) -> Self:
        if self.published_at > self.retrieved_at:
            raise ValueError("News publication is after retrieval")
        if self.relevance != "UNASSESSED" and self.relevance_method == "unreviewed":
            raise ValueError("Material relevance requires an explicit review")
        return self


class NewsCoverage(Contract):
    provider: Text
    provider_symbol: Text
    window_start: AwareDatetime
    window_end: AwareDatetime
    retrieved_at: AwareDatetime
    status: Literal["COMPLETE", "EMPTY", "PARTIAL", "TRUNCATED", "FAILED", "NOT_REQUESTED"]
    reason: Text
    items: tuple[NewsItem, ...] = ()
    pages: int = Field(default=0, ge=0)
    duplicates: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)
    findings: tuple[Text, ...] = ()
    collection_stopped: bool = False
    retention_policy: Literal["discovery-metadata-1"] = "discovery-metadata-1"
    point_in_time: Literal["publication-filter-only"] = "publication-filter-only"

    @model_validator(mode="after")
    def consistent_coverage(self) -> Self:
        if self.window_start > self.window_end:
            raise ValueError("Invalid research window")
        if self.status in ("EMPTY", "NOT_REQUESTED") and self.items:
            raise ValueError("Empty or unrequested coverage cannot contain news")
        if self.status == "COMPLETE" and (not self.items or self.rejected):
            raise ValueError("Complete news coverage requires valid items")
        if self.status in ("COMPLETE", "EMPTY") and not self.pages:
            raise ValueError("Completed news coverage requires a retrieved page")
        if self.status == "EMPTY" and self.rejected:
            raise ValueError("Rejected news is not an empty result")
        ids = [i.evidence_id for i in self.items]
        if len(set(ids)) != len(ids):
            raise ValueError("Duplicate news evidence IDs")
        if any(
            not self.window_start <= i.published_at <= self.window_end
            or i.retrieved_at > self.retrieved_at
            for i in self.items
        ):
            raise ValueError("News is outside the recorded coverage window")
        return self


class EarningsObservation(Contract):
    evidence_id: UUID
    source_url: Text
    retrieved_at: AwareDatetime
    published_at: AwareDatetime | None = None
    original_date: date
    original_timezone: Text
    original_timestamp: AwareDatetime | None = None
    exchange_timezone: Text
    normalized_session_date: date | None
    corroboration_source: Text | None = None

    _url = field_validator("source_url")(source_url)

    @field_validator("corroboration_source")
    @classmethod
    def valid_corroboration(cls, value):
        return source_url(value) if value is not None else None

    @model_validator(mode="after")
    def normalized_date(self) -> Self:
        try:
            original, exchange = ZoneInfo(self.original_timezone), ZoneInfo(self.exchange_timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Unknown event timezone") from None
        if self.original_timestamp is not None:
            stamp = self.original_timestamp
            if (
                stamp.date() != self.original_date
                or stamp.utcoffset() != stamp.astimezone(original).utcoffset()
            ):
                raise ValueError("Original event timestamp/date/timezone disagree")
            normalized = stamp.astimezone(exchange).date()
        else:
            # A date-only observation in another timezone cannot be converted without a time.
            normalized = self.original_date if original == exchange else None
        if self.normalized_session_date != normalized:
            raise ValueError("Event exchange-local date is inconsistent")
        if self.published_at is not None and self.published_at > self.retrieved_at:
            raise ValueError("Event publication is after retrieval")
        return self


class EarningsEvidence(Contract):
    state: Literal["KNOWN", "UNKNOWN", "CONFLICTING"] = "UNKNOWN"
    observations: tuple[EarningsObservation, ...] = ()
    forward_coverage: Literal["UNVERIFIED"] = "UNVERIFIED"
    mandatory_gate_cleared: Literal[False] = False
    gap: Text = FORWARD_EARNINGS_GAP
    follow_up: Text = EARNINGS_FOLLOW_UP

    @model_validator(mode="after")
    def consistent_state(self) -> Self:
        if len({o.evidence_id for o in self.observations}) != len(self.observations):
            raise ValueError("Duplicate earnings evidence IDs")
        dates = {o.normalized_session_date for o in self.observations} - {None}
        times = {o.original_timestamp for o in self.observations if o.original_timestamp}
        expected = (
            "CONFLICTING"
            if len(dates) > 1 or len(times) > 1
            else "KNOWN"
            if dates and all(o.normalized_session_date for o in self.observations)
            else "UNKNOWN"
        )
        if self.state != expected:
            raise ValueError("Earnings state does not match retained observations")
        return self


class ResearchRecord(Contract):
    run_id: UUID
    instrument: Instrument
    news: NewsCoverage
    earnings: EarningsEvidence = Field(default_factory=EarningsEvidence)
    model_use: Literal["NOT_SENT"] = "NOT_SENT"


def render_research(records: tuple[ResearchRecord, ...]) -> list[str]:
    lines = ["", "## News coverage and earnings uncertainty", ""]
    if not records:
        lines.extend(
            [
                "- News: NOT_REQUESTED; no retained research coverage for this run.",
                f"- Earnings: UNKNOWN. {FORWARD_EARNINGS_GAP}",
                f"- Follow-up: {EARNINGS_FOLLOW_UP}",
            ]
        )
    for record in records:
        news, events = record.news, record.earnings
        lines.extend(
            [
                f"- {record.instrument.symbol}: news {news.status}; {news.reason}",
                f"  Provider {news.provider}/{news.provider_symbol}; publication window "
                f"{news.window_start.isoformat()} to {news.window_end.isoformat()}; "
                f"retrieved {news.retrieved_at.isoformat()}; pages {news.pages}; "
                f"duplicates {news.duplicates}; rejected {news.rejected}.",
                f"  Findings: {', '.join(news.findings) or 'none'}; "
                f"retention {news.retention_policy}; model use {record.model_use}.",
            ]
        )
        for item in news.items:
            lines.append(
                f"  - Evidence {item.evidence_id}; provider ID {item.provider_id}; "
                f"source {item.source_url}; published {item.published_at.isoformat()}; "
                f"retrieved {item.retrieved_at.isoformat()}; ticker association "
                f"{item.ticker_associated}; relevance {item.relevance} "
                f"({item.relevance_method}): {item.relevance_reason}"
            )
        lines.extend(
            [
                f"- Earnings {events.state}; forward coverage {events.forward_coverage}; "
                f"mandatory gate cleared: {events.mandatory_gate_cleared}.",
                f"  Gap: {events.gap}",
                f"  Follow-up: {events.follow_up}",
            ]
        )
        for event in events.observations:
            lines.append(
                f"  - Evidence {event.evidence_id}; source {event.source_url}; "
                f"published {event.published_at}; retrieved {event.retrieved_at.isoformat()}; "
                f"original {event.original_timestamp or event.original_date} "
                f"({event.original_timezone}); exchange-local date "
                f"{event.normalized_session_date} ({event.exchange_timezone}); "
                f"corroboration {event.corroboration_source or 'unavailable'}."
            )
    lines.extend(
        [
            "- Empty or failed retrieval does not establish that no news exists. "
            "Completed pagination covers only this provider query, not all news.",
            "- Publication filtering does not prove availability at the historical cutoff; "
            "retrieval timestamps are retained separately. Article content is not retained.",
            "- Unknown or conflicting earnings cannot clear a future mandatory event gate. "
            "A known date alone does not certify forward coverage or clear that gate.",
        ]
    )
    return lines
