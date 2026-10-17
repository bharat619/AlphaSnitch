"""Completed-session and independent daily/weekly history resolution.

Weeks are exchange-local Monday–Sunday buckets. A week becomes complete at
the close of its final scheduled session, including holiday-shortened weeks.
Grace periods describe configured policy, not observed provider publication.
"""

from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from importlib.metadata import version
from itertools import groupby
from typing import Annotated, Literal, Protocol, Self
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, Field, field_validator, model_validator

from stock_analyser.contracts import (
    CalendarReference,
    Contract,
    HistoryRequirement,
    MarketContext,
    Text,
)


class ResolutionReason(StrEnum):
    MARKET_CLOSED = "MARKET_CLOSED"
    PUBLICATION_DELAYED = "PUBLICATION_DELAYED"
    PUBLICATION_UNVERIFIED = "PUBLICATION_UNVERIFIED"
    STALE_DATA = "STALE_DATA"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


class CutoffPolicy(Contract):
    policy_version: Literal["completed-sessions-1"] = "completed-sessions-1"
    daily_grace_seconds: Annotated[int, Field(strict=True, ge=0)] = 900
    weekly_grace_seconds: Annotated[int, Field(strict=True, ge=0)] = 900
    publication_timing: Literal["unverified", "synthetic", "observed"] = "unverified"
    # An explicit lower bound, never an assumption about a listing's inception.
    earliest_session: date = date(1990, 1, 1)


class CalendarSession(Contract):
    session: date
    opens_at: AwareDatetime
    closes_at: AwareDatetime

    @field_validator("opens_at", "closes_at")
    @classmethod
    def utc_instant(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def positive_duration(self) -> Self:
        if self.opens_at >= self.closes_at:
            raise ValueError("Session open must precede close")
        return self


class HistoryPeriod(Contract):
    session_start: date
    session_end: date
    closes_at: AwareDatetime
    available_at: AwareDatetime

    @model_validator(mode="after")
    def valid_period(self) -> Self:
        if self.session_start > self.session_end or self.available_at < self.closes_at:
            raise ValueError("Invalid history period boundaries")
        return self


class ResolvedHistory(Contract):
    requested_as_of: AwareDatetime
    market_id: Text
    market_configuration_version: Text
    venue: Text
    timezone: Text
    calendar: CalendarReference
    calendar_data_version: Text
    policy: CutoffPolicy
    requirement: HistoryRequirement
    periods: tuple[HistoryPeriod, ...]
    reasons: tuple[ResolutionReason, ...] = ()

    @field_validator("requested_as_of")
    @classmethod
    def utc_request(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def valid_resolution(self) -> Self:
        if self.venue != self.calendar.venue:
            raise ValueError("Resolved venue differs from calendar")
        ends = [period.session_end for period in self.periods]
        if ends != sorted(set(ends)) or len(ends) > self.requirement.required_bars:
            raise ValueError("Invalid resolved history ordering or count")
        for period in self.periods:
            if period.available_at > self.requested_as_of:
                raise ValueError("Resolved history includes an unpublished period")
            if period.closes_at.astimezone(ZoneInfo(self.timezone)).date() != period.session_end:
                raise ValueError("Resolved close differs from local session date")
            if self.requirement.timeframe == "daily" and period.session_start != period.session_end:
                raise ValueError("Daily periods must describe one session")
        return self

    @property
    def session_cutoff(self) -> datetime | None:
        return self.periods[-1].closes_at if self.periods else None

    @property
    def session_date(self) -> date | None:
        return self.periods[-1].session_end if self.periods else None

    @property
    def history_start(self) -> date | None:
        return self.periods[0].session_start if self.periods else None


class SessionCalendar(Protocol):
    def data_version(self, market: MarketContext, venue: str) -> str: ...

    def schedule(
        self, market: MarketContext, venue: str, start: date, end: date
    ) -> tuple[CalendarSession, ...]: ...


def calendar_reference(market: MarketContext, venue: str) -> CalendarReference:
    for reference in market.calendars:
        if reference.venue == venue:
            return reference
    raise ValueError("Venue has no calendar in this market")


class ExchangeCalendarAdapter:
    """US regular equity sessions from locked pandas-market-calendars data.

    ARCX uses the NYSE regular equity schedule. Extended-hours sessions are
    deliberately excluded. Unknown market/reference versions fail closed.
    """

    _names = {"XNYS": "NYSE", "XNAS": "NASDAQ", "ARCX": "NYSE"}

    def _name(self, market: MarketContext, venue: str) -> str:
        reference = calendar_reference(market, venue)
        if (
            market.market_id != "US"
            or market.timezone != "America/New_York"
            or reference.version != "us-calendar-contract-1"
            or reference.reference != venue
            or venue not in self._names
        ):
            raise ValueError("Unsupported maintained calendar reference")
        return self._names[venue]

    def data_version(self, market: MarketContext, venue: str) -> str:
        package_version = version("pandas-market-calendars")
        return f"pandas-market-calendars:{package_version}:{self._name(market, venue)}"

    def schedule(
        self, market: MarketContext, venue: str, start: date, end: date
    ) -> tuple[CalendarSession, ...]:
        import pandas_market_calendars as mcal

        schedule = mcal.get_calendar(self._name(market, venue)).schedule(start, end)
        return tuple(
            CalendarSession(
                session=label.date(),
                opens_at=row.market_open.to_pydatetime(),
                closes_at=row.market_close.to_pydatetime(),
            )
            for label, row in schedule.iterrows()
        )

    def latest_completed_session(
        self, market: MarketContext, venue: str, as_of: datetime
    ) -> datetime:
        """Compatibility with MarketCalendarProvider; applies default grace policy."""
        result = HistoryResolver(self).resolve(
            market, venue, HistoryRequirement(timeframe="daily", minimum_bars=1), as_of
        )
        if result.session_cutoff is None:
            raise ValueError("No completed session within calendar policy range")
        return result.session_cutoff


class HistoryResolver:
    def __init__(self, calendar: SessionCalendar, policy: CutoffPolicy | None = None) -> None:
        self.calendar = calendar
        self.policy = policy or CutoffPolicy()

    def resolve(
        self,
        market: MarketContext,
        venue: str,
        requirement: HistoryRequirement,
        requested_as_of: datetime,
    ) -> ResolvedHistory:
        if requested_as_of.tzinfo is None or requested_as_of.utcoffset() is None:
            raise ValueError("Requested time must be timezone aware")
        requested = requested_as_of.astimezone(UTC)
        local_day = requested.astimezone(ZoneInfo(market.timezone)).date()
        reference = calendar_reference(market, venue)
        data_version = self.calendar.data_version(market, venue)
        # Query the rest of the week too: otherwise Wednesday could look like
        # the final scheduled session of an unfinished week.
        end = local_day + timedelta(days=6 - local_day.weekday())
        span = max(32, requirement.required_bars * (3 if requirement.timeframe == "daily" else 14))
        while True:
            start = max(self.policy.earliest_session, local_day - timedelta(days=span))
            if requirement.timeframe == "weekly":
                start = max(self.policy.earliest_session, start - timedelta(days=start.weekday()))
            sessions = self.calendar.schedule(market, venue, start, end) if start <= end else ()
            labels = [session.session for session in sessions]
            if labels != sorted(set(labels)):
                raise ValueError("Calendar sessions must be unique and ascending")
            if any(not start <= label <= end for label in labels):
                raise ValueError("Calendar returned a session outside requested range")
            periods = self._periods(sessions, requirement)
            eligible = tuple(period for period in periods if period.available_at <= requested)
            if len(eligible) >= requirement.required_bars or start == self.policy.earliest_session:
                break
            span *= 2
        reasons = []
        if not any(s.opens_at <= requested < s.closes_at for s in sessions):
            reasons.append(ResolutionReason.MARKET_CLOSED)
        if any(p.closes_at <= requested < p.available_at for p in periods):
            reasons.append(ResolutionReason.PUBLICATION_DELAYED)
        if len(eligible) < requirement.required_bars:
            reasons.append(ResolutionReason.INSUFFICIENT_HISTORY)
        if self.policy.publication_timing == "unverified":
            reasons.append(ResolutionReason.PUBLICATION_UNVERIFIED)
        return ResolvedHistory(
            requested_as_of=requested,
            market_id=market.market_id,
            market_configuration_version=market.configuration_version,
            venue=venue,
            timezone=market.timezone,
            calendar=reference,
            calendar_data_version=data_version,
            policy=self.policy,
            requirement=requirement,
            periods=eligible[-requirement.required_bars :],
            reasons=tuple(reasons),
        )

    def _periods(
        self, sessions: tuple[CalendarSession, ...], requirement: HistoryRequirement
    ) -> tuple[HistoryPeriod, ...]:
        groups = (
            ((session,) for session in sessions)
            if requirement.timeframe == "daily"
            else (
                tuple(group)
                for _, group in groupby(
                    sessions, key=lambda s: s.session - timedelta(days=s.session.weekday())
                )
            )
        )
        grace = timedelta(
            seconds=self.policy.daily_grace_seconds
            if requirement.timeframe == "daily"
            else self.policy.weekly_grace_seconds
        )
        return tuple(
            HistoryPeriod(
                session_start=group[0].session,
                session_end=group[-1].session,
                closes_at=group[-1].closes_at,
                available_at=group[-1].closes_at + grace,
            )
            for group in groups
            # Never count an initial week truncated by the configured lower bound.
            if requirement.timeframe == "daily"
            or group[0].session - timedelta(days=group[0].session.weekday())
            >= self.policy.earliest_session
        )


def assess_history(
    resolution: ResolvedHistory, returned_sessions: tuple[date, ...]
) -> tuple[ResolutionReason, ...]:
    """Assess normalized bar labels (weekly labels are final local session dates).

    Missing latest or interior expected periods are stale; short usable history
    is insufficient. A holiday/weekend does not itself make completed data stale.
    Older bars are ignored when counting usable history. Incomplete/future bars
    are rejected, never counted towards warm-up.
    """
    if list(returned_sessions) != sorted(set(returned_sessions)):
        raise ValueError("Returned sessions must be unique and ascending")
    expected = {period.session_end for period in resolution.periods}
    if any(
        session not in expected and (not expected or session >= min(expected))
        for session in returned_sessions
    ):
        raise ValueError("Returned bar is outside the resolved completed history window")
    usable = expected.intersection(returned_sessions)
    reasons = list(resolution.reasons)
    if expected and (not usable or any(s >= min(usable) for s in expected.difference(usable))):
        reasons.append(ResolutionReason.STALE_DATA)
    if len(usable) < resolution.requirement.required_bars:
        reasons.append(ResolutionReason.INSUFFICIENT_HISTORY)
    return tuple(dict.fromkeys(reasons))
