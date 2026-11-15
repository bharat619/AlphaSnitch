"""Retained earnings evidence and an offline, versioned exposure gate.

Version 1 can clear synthetic research evidence only. Live source qualification,
recommendations, position sizing and execution remain separate capabilities.
"""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AwareDatetime, Field, field_validator, model_validator

from stock_analyser.cache import digest, verified
from stock_analyser.calendars import CalendarSession, SessionCalendar, calendar_reference
from stock_analyser.collection import encode, immutable_write
from stock_analyser.contracts import Contract, Instrument, MarketContext, PositiveInt, Text
from stock_analyser.research import EarningsObservation, source_url

Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
CHECKS = ("entry_timing", "full_holding_window", "post_event_session")


class EarningsSourcePolicy(Contract):
    source_id: Text
    publisher_id: Text
    role: Literal["issuer", "independent"]
    qualification: Literal["UNVERIFIED", "SYNTHETIC"] = "UNVERIFIED"
    qualification_reference: Text | None = None
    max_age_seconds: PositiveInt | None = None


class EarningsPolicy(Contract):
    policy_version: Literal["earnings-exposure-1"] = "earnings-exposure-1"
    scope: Literal["offline_research"] = "offline_research"
    # Required choices, deliberately without production defaults.
    max_holding_sessions: PositiveInt | None
    pre_open_buffer_seconds: Annotated[int, Field(strict=True, ge=0)] | None
    sources: tuple[EarningsSourcePolicy, ...]

    @model_validator(mode="after")
    def unique_sources(self):
        if len({s.source_id for s in self.sources}) != len(self.sources):
            raise ValueError("Duplicate source policy")
        return self


class EarningsEvent(Contract):
    event_id: Text
    announcement: EarningsObservation
    release_state: Literal["SCHEDULED", "CONFIRMED", "CANCELLED", "UNRESOLVED"]
    released_at: AwareDatetime | None = None
    call_state: Literal["UNKNOWN", "NONE_CONFIRMED", "SCHEDULED", "CONFIRMED"]
    call_completed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def confirmation_times(self):
        if (self.release_state == "CONFIRMED") != (self.released_at is not None):
            raise ValueError("Confirmed release requires its actual occurrence time")
        if (self.call_state == "CONFIRMED") != (self.call_completed_at is not None):
            raise ValueError("Confirmed call requires its actual completion time")
        if self.released_at is not None:
            local = self.released_at.astimezone(ZoneInfo(self.announcement.exchange_timezone))
            if local.date() != self.announcement.normalized_session_date:
                raise ValueError("Actual release differs from the normalized announcement date")
        if self.call_completed_at is not None and (
            self.released_at is None or self.call_completed_at < self.released_at
        ):
            raise ValueError("Call completion precedes the confirmed release")
        if any(
            stamp is not None and stamp > self.announcement.retrieved_at
            for stamp in (self.released_at, self.call_completed_at)
        ):
            raise ValueError("Confirmation includes an event after retrieval")
        return self

    def identity(self):
        """Compare event meaning, excluding source-specific URLs and retrieval IDs."""
        return (
            self.event_id,
            self.announcement.normalized_session_date,
            self.announcement.original_timestamp,
            self.release_state,
            self.released_at,
            self.call_state,
            self.call_completed_at,
        )


class EarningsSourceEvidence(Contract):
    evidence_version: Literal["earnings-source-1"] = "earnings-source-1"
    source_id: Text
    publisher_id: Text
    revision: Text
    instrument_id: UUID
    security_id: UUID
    exchange_timezone: Text
    scope: Literal["synthetic", "observed"]
    coverage_state: Literal["VERIFIED", "UNVERIFIED"]
    coverage_start: date
    coverage_end: date
    # Coverage observation time is separate from a later download of the same data.
    coverage_as_of: AwareDatetime
    published_at: AwareDatetime
    retrieved_at: AwareDatetime
    available_at: AwareDatetime
    coverage_source_url: Text
    coverage_reference: Text
    events: tuple[EarningsEvent, ...] = ()
    recent_event_state: Literal["UNKNOWN", "NONE_CONFIRMED", "RETAINED"]
    recent_event: EarningsEvent | None = None

    _url = field_validator("coverage_source_url")(source_url)

    @model_validator(mode="after")
    def evidence_consistency(self):
        try:
            ZoneInfo(self.exchange_timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Unknown earnings exchange timezone") from None
        if self.coverage_start > self.coverage_end:
            raise ValueError("Reversed forward coverage window")
        if not self.coverage_as_of <= self.published_at <= self.retrieved_at <= self.available_at:
            raise ValueError("Earnings publication/retrieval/availability chronology is invalid")
        if (self.recent_event_state == "RETAINED") != (self.recent_event is not None):
            raise ValueError("Recent-event state differs from retained evidence")
        events = (*self.events, *((self.recent_event,) if self.recent_event else ()))
        if len({e.event_id for e in events}) != len(events):
            raise ValueError("Duplicate earnings event identity")
        if len({e.announcement.evidence_id for e in events}) != len(events):
            raise ValueError("Duplicate earnings observation identity")
        for event in events:
            observation = event.announcement
            if observation.exchange_timezone != self.exchange_timezone:
                raise ValueError("Earnings exchange timezone mismatch")
            if observation.retrieved_at > self.retrieved_at:
                raise ValueError("Event was retrieved after its containing evidence")
            if any(
                stamp is not None and stamp > self.coverage_as_of
                for stamp in (observation.published_at, event.released_at, event.call_completed_at)
            ):
                raise ValueError("Event information postdates the coverage observation")
        return self


class EarningsEvidenceReference(Contract):
    reference: Text
    sha256: Hash


def freeze_earnings_evidence(directory: Path, evidence: EarningsSourceEvidence):
    """Retain canonical normalized evidence; this is not a live-source verifier."""
    evidence = EarningsSourceEvidence.model_validate_json(evidence.model_dump_json())
    content = encode(evidence.model_dump(mode="json"))
    sha = digest(evidence.model_dump(mode="json"))
    path = Path(directory).resolve() / f"{sha}.json"
    immutable_write(path, content)
    return EarningsEvidenceReference(reference=str(path), sha256=sha)


def _load(reference):
    content = verified(reference.reference, reference.sha256)
    evidence = EarningsSourceEvidence.model_validate_json(content)
    if encode(evidence.model_dump(mode="json")) != content:
        raise ValueError("Earnings evidence must use canonical serialization")
    return evidence


class EarningsContext(Contract):
    market: MarketContext
    instrument: Instrument
    signal_session: date
    signal_available_at: AwareDatetime | None
    as_of: AwareDatetime

    @model_validator(mode="after")
    def instrument_matches(self):
        self.market.validate_instrument(self.instrument)
        return self


def _bounds(context, policy, evidence):
    days = [context.signal_session]
    for source in evidence:
        # Unavailable future evidence must not change the historical calendar query.
        if source.available_at <= context.as_of:
            days.extend(
                e.announcement.normalized_session_date
                for e in (*source.events, *((source.recent_event,) if source.recent_event else ()))
                if e.announcement.normalized_session_date is not None
            )
    return min(days) - timedelta(days=14), max(days) + timedelta(
        days=max(32, (policy.max_holding_sessions or 1) * 3)
    )


class EarningsCalendar(Contract):
    data_version: Text
    sessions: tuple[CalendarSession, ...]


def _evaluate(context, policy, evidence, calendar, load_errors):
    reasons = list(load_errors)
    comparisons = dict.fromkeys(CHECKS)
    signal = next((s for s in calendar.sessions if s.session == context.signal_session), None)
    future = tuple(s for s in calendar.sessions if s.session > context.signal_session)
    entry = future[0] if future else None
    holding = None
    if policy.max_holding_sessions is None:
        reasons.append("HOLDING_PERIOD_UNSET")
    elif len(future) >= policy.max_holding_sessions:
        holding = future[policy.max_holding_sessions - 1]
    else:
        reasons.append("HOLDING_CALENDAR_UNAVAILABLE")
    if policy.pre_open_buffer_seconds is None:
        reasons.append("ENTRY_CUTOFF_UNSET")
    if signal is None or entry is None:
        reasons.append("SIGNAL_OR_ENTRY_SESSION_UNAVAILABLE")
    elif context.signal_available_at is None or not (
        signal.closes_at <= context.signal_available_at <= context.as_of
    ):
        reasons.append("SIGNAL_EVIDENCE_UNAVAILABLE")
    else:
        latest = [s for s in calendar.sessions if s.closes_at <= context.as_of]
        if not latest or latest[-1].session != context.signal_session:
            reasons.append("SIGNAL_SESSION_STALE")
        if policy.pre_open_buffer_seconds is not None:
            comparisons["entry_timing"] = context.as_of < entry.opens_at - timedelta(
                seconds=policy.pre_open_buffer_seconds
            )

    selected = []
    if {s.role for s in policy.sources} != {"issuer", "independent"}:
        reasons.append("ISSUER_AND_CORROBORATOR_REQUIRED")
    if len({s.publisher_id for s in policy.sources}) != len(policy.sources):
        reasons.append("SOURCES_NOT_INDEPENDENT")
    if any(e.source_id not in {s.source_id for s in policy.sources} for e in evidence):
        reasons.append("UNCONFIGURED_SOURCE")
    for source_policy in policy.sources:
        sid = source_policy.source_id
        matches = [e for e in evidence if e.source_id == sid]
        if len(matches) != 1:
            reasons.append(f"{sid}:MISSING_OR_DUPLICATE_SOURCE")
            continue
        source = matches[0]
        if (
            source.instrument_id != context.instrument.instrument_id
            or source.security_id != context.instrument.security_id
            or source.exchange_timezone != context.market.timezone
            or source.publisher_id != source_policy.publisher_id
        ):
            reasons.append(f"{sid}:IDENTITY_MISMATCH")
            continue
        if source.available_at > context.as_of:
            reasons.append(f"{sid}:EVIDENCE_NOT_YET_AVAILABLE")
            continue
        selected.append(source)
        if (
            source_policy.qualification != "SYNTHETIC"
            or not source_policy.qualification_reference
            or source.scope != "synthetic"
        ):
            reasons.append(f"{sid}:SOURCE_UNQUALIFIED")
        if source_policy.max_age_seconds is None:
            reasons.append(f"{sid}:FRESHNESS_POLICY_UNSET")
        elif context.as_of - source.coverage_as_of > timedelta(
            seconds=source_policy.max_age_seconds
        ):
            reasons.append(f"{sid}:EVIDENCE_STALE")
        if source.coverage_state != "VERIFIED":
            reasons.append(f"{sid}:FORWARD_COVERAGE_UNVERIFIED")
        if (
            holding
            and not source.coverage_start
            <= context.signal_session
            <= holding.session
            <= source.coverage_end
        ):
            reasons.append(f"{sid}:FORWARD_COVERAGE_INCOMPLETE")
        if source.recent_event_state == "UNKNOWN":
            reasons.append(f"{sid}:RECENT_EVENT_UNKNOWN")

    # No conflict resolution by picking a preferred or more permissive source.
    signatures = {
        (
            tuple(sorted((e.identity() for e in s.events), key=lambda item: item[0])),
            s.recent_event_state,
            s.recent_event.identity() if s.recent_event else None,
        )
        for s in selected
    }
    if len(signatures) > 1:
        reasons.append("EARNINGS_SOURCE_CONFLICT")

    deadlines = []
    post_checks = []
    forward_known = bool(selected)
    for source in selected:
        all_events = (*source.events, *((source.recent_event,) if source.recent_event else ()))
        if any(e.announcement.published_at is None for e in all_events):
            reasons.append(f"{source.source_id}:EVENT_PUBLICATION_UNVERIFIED")
        for event in source.events:
            day = event.announcement.normalized_session_date
            if day is None:
                reasons.append(f"{source.source_id}:EVENT_DATE_UNKNOWN")
                forward_known = False
                continue
            if event.release_state != "SCHEDULED":
                reasons.append(f"{source.source_id}:FORWARD_EVENT_UNRESOLVED")
            if day < context.signal_session:
                reasons.append(f"{source.source_id}:PAST_EVENT_IN_FORWARD_CALENDAR")
            prior = [s for s in calendar.sessions if s.session < day]
            if prior:
                deadlines.append(prior[-1].closes_at)
            else:
                reasons.append("PRE_EVENT_CALENDAR_UNAVAILABLE")
                forward_known = False
        recent = source.recent_event
        if source.recent_event_state == "NONE_CONFIRMED":
            post_checks.append(True)
        elif recent:
            day = recent.announcement.normalized_session_date
            if day is None or day > context.signal_session:
                reasons.append(f"{source.source_id}:RECENT_EVENT_DATE_UNRESOLVED")
            if recent.release_state != "CONFIRMED" or recent.call_state not in (
                "NONE_CONFIRMED",
                "CONFIRMED",
            ):
                reasons.append(f"{source.source_id}:EVENT_COMPLETION_UNCONFIRMED")
            elif signal:
                completed = max(recent.released_at, recent.call_completed_at or recent.released_at)
                post_checks.append(signal.opens_at > completed)
    if holding and forward_known:
        comparisons["full_holding_window"] = not deadlines or holding.closes_at <= min(deadlines)
    if post_checks:
        comparisons["post_event_session"] = all(post_checks)
    reasons = tuple(dict.fromkeys(reasons))
    failed = tuple(key for key in CHECKS if comparisons[key] is False)
    status = (
        "UNAVAILABLE"
        if reasons or any(value is None for value in comparisons.values())
        else "FAIL"
        if failed
        else "PASS"
    )
    available = None
    if status != "UNAVAILABLE":
        available = max(context.signal_available_at, *(s.available_at for s in selected))
    return dict(
        status=status,
        comparisons=comparisons,
        failed_conditions=failed,
        unavailable_reasons=reasons,
        entry_session=entry.session if entry else None,
        holding_deadline=holding.closes_at if holding else None,
        pre_event_deadline=min(deadlines) if deadlines else None,
        available_at=available,
    )


class EarningsAssessment(Contract):
    implementation_version: Literal["earnings-gate-1"] = "earnings-gate-1"
    context: EarningsContext
    policy: EarningsPolicy
    policy_sha256: Hash
    references: tuple[EarningsEvidenceReference, ...]
    evidence: tuple[EarningsSourceEvidence, ...]
    load_errors: tuple[Text, ...]
    calendar: EarningsCalendar
    calculated_at: AwareDatetime
    status: Literal["PASS", "FAIL", "UNAVAILABLE"]
    comparisons: dict[str, bool | None]
    failed_conditions: tuple[Text, ...]
    unavailable_reasons: tuple[Text, ...]
    entry_session: date | None
    holding_deadline: AwareDatetime | None
    pre_event_deadline: AwareDatetime | None
    available_at: AwareDatetime | None
    recommendation_status: Literal["NOT_EVALUATED"] = "NOT_EVALUATED"

    @model_validator(mode="after")
    def consistent(self):
        if self.policy_sha256 != digest(self.policy.model_dump(mode="json")):
            raise ValueError("Earnings policy hash mismatch")
        if self.calculated_at < self.context.as_of:
            raise ValueError("Calculation precedes assessment cutoff")
        if any(
            digest(e.model_dump(mode="json")) not in {r.sha256 for r in self.references}
            for e in self.evidence
        ):
            raise ValueError("Retained earnings evidence differs from its reference")
        start, end = _bounds(self.context, self.policy, self.evidence)
        sessions = self.calendar.sessions
        if [s.session for s in sessions] != sorted({s.session for s in sessions}):
            raise ValueError("Calendar sessions must be unique and ascending")
        zone = ZoneInfo(self.context.market.timezone)
        for session in sessions:
            if not start <= session.session <= end or any(
                stamp.astimezone(zone).date() != session.session
                for stamp in (session.opens_at, session.closes_at)
            ):
                raise ValueError("Calendar session outside expected date or timezone")
        expected = _evaluate(
            self.context, self.policy, self.evidence, self.calendar, self.load_errors
        )
        if any(getattr(self, key) != value for key, value in expected.items()):
            raise ValueError("Earnings assessment differs from retained inputs")
        return self

    def semantic_bytes(self):
        return encode(self.model_dump(mode="json", exclude={"calculated_at"}))


def assess_earnings(
    context: EarningsContext,
    policy: EarningsPolicy,
    references: tuple[EarningsEvidenceReference, ...],
    calendar: SessionCalendar,
    *,
    calculated_at: datetime,
) -> EarningsAssessment:
    """Evaluate retained records without any provider or model calls."""
    context = EarningsContext.model_validate_json(context.model_dump_json())
    policy = EarningsPolicy.model_validate_json(policy.model_dump_json())
    evidence, errors = [], []
    for ref in references:
        try:
            evidence.append(_load(ref))
        except (OSError, ValueError):
            errors.append(f"EVIDENCE_UNREADABLE:{ref.sha256}")
    start, end = _bounds(context, policy, evidence)
    calendar_reference(context.market, context.instrument.venue)
    retained_calendar = EarningsCalendar(
        data_version=calendar.data_version(context.market, context.instrument.venue),
        sessions=calendar.schedule(context.market, context.instrument.venue, start, end),
    )
    return EarningsAssessment(
        context=context,
        policy=policy,
        policy_sha256=digest(policy.model_dump(mode="json")),
        references=references,
        evidence=tuple(evidence),
        load_errors=tuple(errors),
        calendar=retained_calendar,
        calculated_at=calculated_at,
        **_evaluate(context, policy, evidence, retained_calendar, errors),
    )


def replay_earnings(assessment, calendar, *, calculated_at):
    replay = assess_earnings(
        assessment.context,
        assessment.policy,
        assessment.references,
        calendar,
        calculated_at=calculated_at,
    )
    if replay.semantic_bytes() != assessment.semantic_bytes():
        raise ValueError("Earnings assessment differs from authenticated evidence replay")
    return replay
