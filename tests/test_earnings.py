import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from stock_analyser.calendars import ExchangeCalendarAdapter
from stock_analyser.earnings import (
    EarningsAssessment,
    EarningsContext,
    EarningsEvent,
    EarningsPolicy,
    EarningsSourceEvidence,
    EarningsSourcePolicy,
    assess_earnings,
    freeze_earnings_evidence,
    replay_earnings,
)
from stock_analyser.fixtures import FixtureSessionCalendar, fixture_id, synthetic_markets
from stock_analyser.markets import US_MARKET
from stock_analyser.research import EarningsObservation

AS_OF = datetime(2026, 11, 6, 22, tzinfo=UTC)


def event(day=date(2026, 12, 8), *, source="issuer", **changes):
    return EarningsEvent(
        **{
            "event_id": "2026-Q4",
            "announcement": EarningsObservation(
                evidence_id=fixture_id(f"{source}:{day}"),
                source_url=f"https://{source}.example.com/earnings",
                published_at=AS_OF - timedelta(hours=2),
                retrieved_at=AS_OF,
                original_date=day,
                original_timezone="America/New_York",
                exchange_timezone="America/New_York",
                normalized_session_date=day,
            ),
            "release_state": "SCHEDULED",
            "call_state": "UNKNOWN",
            **changes,
        }
    )


def context(**changes):
    _, _, adapters = synthetic_markets()
    instrument = adapters[0].listings[0].model_copy(update={"venue": "XNYS"})
    return EarningsContext(
        **{
            "market": US_MARKET,
            "instrument": instrument,
            "signal_session": date(2026, 11, 6),
            "signal_available_at": AS_OF - timedelta(minutes=30),
            "as_of": AS_OF,
            **changes,
        }
    )


def policy(**changes):
    return EarningsPolicy(
        **{
            "max_holding_sessions": 20,
            "pre_open_buffer_seconds": 900,
            "sources": tuple(
                EarningsSourcePolicy(
                    source_id=role,
                    publisher_id=f"{role}-organization",
                    role=role,
                    qualification="SYNTHETIC",
                    qualification_reference=f"synthetic://{role}-contract-v1",
                    max_age_seconds=259200,
                )
                for role in ("issuer", "independent")
            ),
            **changes,
        }
    )


def source(source_id="issuer", **changes):
    instrument = context().instrument
    default_event = event(source=source_id)
    default_event = default_event.model_copy(
        update={
            "announcement": default_event.announcement.model_copy(
                update={
                    "published_at": min(
                        AS_OF - timedelta(days=5), changes.get("coverage_as_of", AS_OF)
                    )
                }
            )
        }
    )
    return EarningsSourceEvidence(
        **{
            "source_id": source_id,
            "publisher_id": f"{source_id}-organization",
            "revision": "synthetic-revision-1",
            "instrument_id": instrument.instrument_id,
            "security_id": instrument.security_id,
            "exchange_timezone": "America/New_York",
            "scope": "synthetic",
            "coverage_state": "VERIFIED",
            "coverage_start": date(2026, 11, 6),
            "coverage_end": date(2026, 12, 31),
            "coverage_as_of": AS_OF - timedelta(minutes=30),
            "published_at": AS_OF - timedelta(minutes=15),
            "retrieved_at": AS_OF,
            "available_at": AS_OF,
            "coverage_source_url": f"https://{source_id}.example.com/calendar",
            "coverage_reference": f"synthetic://{source_id}/explicit-forward-coverage",
            "events": (default_event,),
            "recent_event_state": "NONE_CONFIRMED",
            **changes,
        }
    )


def calculate(tmp_path, *, sources=None, ctx=None, pol=None, calendar=None):
    sources = sources if sources is not None else (source(), source("independent"))
    refs = tuple(freeze_earnings_evidence(tmp_path, s) for s in sources)
    ctx = ctx or context()
    return assess_earnings(
        ctx, pol or policy(), refs, calendar or ExchangeCalendarAdapter(), calculated_at=ctx.as_of
    )


def test_full_window_equality_passes_with_retained_sources_and_replay(tmp_path):
    result = calculate(tmp_path)
    assert result.status == "PASS"
    assert result.entry_session == date(2026, 11, 9)
    assert result.holding_deadline == datetime(2026, 12, 7, 21, tzinfo=UTC)
    assert result.pre_event_deadline == result.holding_deadline
    assert result.available_at == AS_OF
    assert result.recommendation_status == "NOT_EVALUATED"
    assert EarningsAssessment.model_validate_json(result.model_dump_json()) == result
    replay = replay_earnings(
        result, ExchangeCalendarAdapter(), calculated_at=AS_OF + timedelta(days=1)
    )
    assert replay.semantic_bytes() == result.semantic_bytes()


@pytest.mark.parametrize(
    "event_day,expected,deadline",
    [(7, "FAIL", 4), (8, "PASS", 7), (6, "FAIL", 4)],
)
def test_previous_session_deadline_and_weekend_events(tmp_path, event_day, expected, deadline):
    result = calculate(
        tmp_path,
        sources=tuple(
            source(s, events=(event(date(2026, 12, event_day), source=s),))
            for s in ("issuer", "independent")
        ),
    )
    assert result.status == expected
    assert result.pre_event_deadline == datetime(2026, 12, deadline, 21, tzinfo=UTC)
    assert ("full_holding_window" in result.failed_conditions) == (expected == "FAIL")


def test_after_close_announcement_still_requires_previous_day_exit(tmp_path):
    sources = []
    for sid in ("issuer", "independent"):
        ev = event(date(2026, 12, 7), source=sid)
        announcement = ev.announcement.model_copy(
            update={"original_timestamp": datetime.fromisoformat("2026-12-07T17:00:00-05:00")}
        )
        sources.append(source(sid, events=(ev.model_copy(update={"announcement": announcement}),)))
    result = calculate(tmp_path, sources=sources)
    assert result.status == "FAIL"
    assert result.pre_event_deadline.day == 4


def test_holidays_early_close_and_entry_counted_as_one(tmp_path):
    result = calculate(tmp_path, pol=policy(max_holding_sessions=14))
    assert result.status == "PASS"
    assert result.holding_deadline == datetime(2026, 11, 27, 18, tzinfo=UTC)
    one = calculate(tmp_path, pol=policy(max_holding_sessions=1))
    assert one.holding_deadline == datetime(2026, 11, 9, 21, tzinfo=UTC)


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"coverage_state": "UNVERIFIED"}, "FORWARD_COVERAGE_UNVERIFIED"),
        ({"coverage_end": date(2026, 12, 6)}, "FORWARD_COVERAGE_INCOMPLETE"),
        ({"coverage_start": date(2026, 11, 7)}, "FORWARD_COVERAGE_INCOMPLETE"),
        ({"scope": "observed"}, "SOURCE_UNQUALIFIED"),
        ({"recent_event_state": "UNKNOWN"}, "RECENT_EVENT_UNKNOWN"),
        ({"instrument_id": uuid4()}, "IDENTITY_MISMATCH"),
        ({"security_id": uuid4()}, "IDENTITY_MISMATCH"),
        ({"publisher_id": "another"}, "IDENTITY_MISMATCH"),
        ({"available_at": AS_OF + timedelta(seconds=1)}, "EVIDENCE_NOT_YET_AVAILABLE"),
        ({"coverage_as_of": AS_OF - timedelta(days=4)}, "EVIDENCE_STALE"),
    ],
)
def test_missing_qualification_and_evidence_never_clear_gate(tmp_path, changes, reason):
    result = calculate(tmp_path, sources=(source(**changes), source("independent")))
    assert result.status == "UNAVAILABLE"
    assert f"issuer:{reason}" in result.unavailable_reasons
    assert result.available_at is None


@pytest.mark.parametrize("field", ["max_holding_sessions", "pre_open_buffer_seconds"])
def test_unset_policy_is_unavailable_and_choices_have_no_defaults(tmp_path, field):
    result = calculate(tmp_path, pol=policy(**{field: None}))
    assert result.status == "UNAVAILABLE"
    payload = policy().model_dump(mode="json")
    payload.pop(field)
    with pytest.raises(ValidationError):
        EarningsPolicy.model_validate(payload)


@pytest.mark.parametrize(
    "change,reason",
    [
        ({"max_age_seconds": None}, "FRESHNESS_POLICY_UNSET"),
        ({"qualification": "UNVERIFIED"}, "SOURCE_UNQUALIFIED"),
        ({"qualification_reference": None}, "SOURCE_UNQUALIFIED"),
    ],
)
def test_source_policy_is_explicit(tmp_path, change, reason):
    pol = policy()
    pol = pol.model_copy(
        update={"sources": (pol.sources[0].model_copy(update=change), pol.sources[1])}
    )
    result = calculate(tmp_path, pol=pol)
    assert f"issuer:{reason}" in result.unavailable_reasons
    assert result.status == "UNAVAILABLE"


def test_freshness_boundary_uses_coverage_observation_not_retrieval(tmp_path):
    sources = tuple(
        source(s, coverage_as_of=AS_OF - timedelta(days=3)) for s in ("issuer", "independent")
    )
    assert calculate(tmp_path, sources=sources).status == "PASS"
    sources = (
        sources[0].model_copy(update={"coverage_as_of": AS_OF - timedelta(days=3, microseconds=1)}),
        sources[1],
    )
    assert calculate(tmp_path, sources=sources).status == "UNAVAILABLE"


def test_empty_verified_coverage_is_distinct_from_empty_news_or_unknown(tmp_path):
    sources = tuple(source(s, events=()) for s in ("issuer", "independent"))
    result = calculate(tmp_path, sources=sources)
    assert result.status == "PASS" and result.pre_event_deadline is None
    sources = (sources[0].model_copy(update={"coverage_state": "UNVERIFIED"}), sources[1])
    assert calculate(tmp_path, sources=sources).status == "UNAVAILABLE"


def test_conflicting_dates_keep_overlap_failure_and_all_sources(tmp_path):
    result = calculate(
        tmp_path,
        sources=(
            source(),
            source("independent", events=(event(date(2026, 12, 7), source="independent"),)),
        ),
    )
    assert result.status == "UNAVAILABLE"
    assert "EARNINGS_SOURCE_CONFLICT" in result.unavailable_reasons
    assert result.failed_conditions == ("full_holding_window",)
    assert len(result.evidence) == 2


@pytest.mark.parametrize("state", ["CANCELLED", "UNRESOLVED"])
def test_cancellation_and_unresolved_revision_require_new_evidence(tmp_path, state):
    sources = tuple(
        source(s, events=(event(source=s, release_state=state),)) for s in ("issuer", "independent")
    )
    result = calculate(tmp_path, sources=sources)
    assert result.status == "UNAVAILABLE"
    assert "issuer:FORWARD_EVENT_UNRESOLVED" in result.unavailable_reasons


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unconfigured", "same_publisher"])
def test_sources_cannot_be_silently_selected(tmp_path, kind):
    sources, pol = (source(), source("independent")), policy()
    if kind == "missing":
        sources = sources[:1]
    elif kind == "duplicate":
        sources = (*sources, source(revision="another"))
    elif kind == "unconfigured":
        sources = (*sources, source("third"))
    else:
        pol = pol.model_copy(
            update={
                "sources": (
                    pol.sources[0],
                    pol.sources[1].model_copy(update={"publisher_id": pol.sources[0].publisher_id}),
                )
            }
        )
    assert calculate(tmp_path, sources=sources, pol=pol).status == "UNAVAILABLE"


@pytest.mark.parametrize("offset,expected", [(-1, "PASS"), (0, "FAIL"), (1, "FAIL")])
def test_pre_open_cutoff_is_strict(tmp_path, offset, expected):
    stamp = datetime(2026, 11, 9, 14, 15, tzinfo=UTC) + timedelta(microseconds=offset)
    # Explicit synthetic freshness contract long enough to cover this weekend.
    pol = policy()
    pol = pol.model_copy(
        update={
            "sources": tuple(s.model_copy(update={"max_age_seconds": 345600}) for s in pol.sources)
        }
    )
    result = calculate(tmp_path, ctx=context(as_of=stamp), pol=pol)
    assert result.status == expected


@pytest.mark.parametrize("stamp", [None, AS_OF + timedelta(seconds=1), AS_OF - timedelta(hours=2)])
def test_signal_must_be_complete_and_available(tmp_path, stamp):
    result = calculate(tmp_path, ctx=context(signal_available_at=stamp))
    assert result.status == "UNAVAILABLE"
    assert "SIGNAL_EVIDENCE_UNAVAILABLE" in result.unavailable_reasons


def test_old_signal_cannot_be_revived(tmp_path):
    result = calculate(tmp_path, ctx=context(as_of=datetime(2026, 11, 9, 22, tzinfo=UTC)))
    assert result.status == "UNAVAILABLE"
    assert "SIGNAL_SESSION_STALE" in result.unavailable_reasons
    assert result.comparisons["entry_timing"] is False


def recent_sources(*, released_at, call_state="NONE_CONFIRMED", call_completed_at=None):
    return tuple(
        source(
            s,
            recent_event_state="RETAINED",
            recent_event=event(
                released_at.astimezone(ZoneInfo("America/New_York")).date(),
                source=s,
                event_id="2026-Q3",
                release_state="CONFIRMED",
                released_at=released_at,
                call_state=call_state,
                call_completed_at=call_completed_at,
            ),
        )
        for s in ("issuer", "independent")
    )


@pytest.mark.parametrize(
    "released,expected",
    [
        ("2026-11-06T14:29:59Z", "PASS"),
        ("2026-11-06T14:30:00Z", "FAIL"),
        ("2026-11-06T21:00:00Z", "FAIL"),
        ("2026-11-05T22:00:00Z", "PASS"),
    ],
)
def test_post_event_requires_whole_signal_session_after_events(tmp_path, released, expected):
    result = calculate(
        tmp_path, sources=recent_sources(released_at=datetime.fromisoformat(released))
    )
    assert result.status == expected
    assert result.comparisons["post_event_session"] == (expected == "PASS")


@pytest.mark.parametrize("call_state", ["UNKNOWN", "SCHEDULED"])
def test_release_confirmation_does_not_confirm_earnings_call(tmp_path, call_state):
    result = calculate(
        tmp_path,
        sources=recent_sources(released_at=AS_OF - timedelta(days=1), call_state=call_state),
    )
    assert result.status == "UNAVAILABLE"
    assert "issuer:EVENT_COMPLETION_UNCONFIRMED" in result.unavailable_reasons


def test_later_call_completion_resets_post_event_session_boundary(tmp_path):
    result = calculate(
        tmp_path,
        sources=recent_sources(
            released_at=AS_OF - timedelta(days=1),
            call_state="CONFIRMED",
            call_completed_at=datetime(2026, 11, 6, 15, tzinfo=UTC),
        ),
    )
    assert result.status == "FAIL"
    assert result.failed_conditions == ("post_event_session",)


def test_missing_and_truncated_calendar_fail_closed(tmp_path):
    for sessions in (
        (),
        ExchangeCalendarAdapter().schedule(US_MARKET, "XNYS", date(2026, 11, 6), date(2026, 11, 9)),
    ):
        result = calculate(tmp_path, calendar=FixtureSessionCalendar(US_MARKET, sessions))
        assert result.status == "UNAVAILABLE"


def test_evidence_hash_corruption_and_missing_file_block_replay(tmp_path):
    original = calculate(tmp_path)
    path = Path(original.references[0].reference)
    path.chmod(0o644)
    path.write_text("{}")
    current = assess_earnings(
        context(), policy(), original.references, ExchangeCalendarAdapter(), calculated_at=AS_OF
    )
    assert current.status == "UNAVAILABLE"
    assert any(r.startswith("EVIDENCE_UNREADABLE") for r in current.unavailable_reasons)
    with pytest.raises(ValueError, match="authenticated evidence replay"):
        replay_earnings(original, ExchangeCalendarAdapter(), calculated_at=AS_OF)
    path.unlink()
    with pytest.raises(ValueError, match="authenticated evidence replay"):
        replay_earnings(original, ExchangeCalendarAdapter(), calculated_at=AS_OF)


def test_revision_does_not_rewrite_original_evidence(tmp_path):
    original = calculate(tmp_path)
    revised = calculate(
        tmp_path,
        sources=tuple(
            source(s, revision="revised-date", events=(event(date(2026, 12, 7), source=s),))
            for s in ("issuer", "independent")
        ),
    )
    assert original.status == "PASS" and revised.status == "FAIL"
    assert original.references != revised.references
    assert replay_earnings(original, ExchangeCalendarAdapter(), calculated_at=AS_OF) == original


def test_result_tampering_and_reordered_json(tmp_path):
    result = calculate(tmp_path)
    payload = result.model_dump(mode="json")
    payload["comparisons"] = dict(reversed(tuple(payload["comparisons"].items())))
    assert EarningsAssessment.model_validate_json(json.dumps(payload)) == result
    for field, value in (
        ("status", "FAIL"),
        ("entry_session", "2026-11-10"),
        ("holding_deadline", "2026-12-08T21:00:00Z"),
        ("unavailable_reasons", ["fabricated"]),
        ("policy_sha256", "0" * 64),
    ):
        with pytest.raises(ValidationError):
            EarningsAssessment.model_validate({**payload, field: value})


def test_calendar_changes_rejected_by_replay(tmp_path):
    result = calculate(tmp_path)
    changed = FixtureSessionCalendar(US_MARKET, result.calendar.sessions[:-1])
    with pytest.raises(ValueError, match="authenticated evidence replay"):
        replay_earnings(result, changed, calculated_at=AS_OF)


@pytest.mark.parametrize(
    "change",
    [
        {"coverage_end": date(2026, 1, 1)},
        {"published_at": AS_OF + timedelta(seconds=1)},
        {"available_at": AS_OF - timedelta(seconds=1)},
        {"recent_event_state": "RETAINED"},
        {"events": (event(), event())},
    ],
)
def test_invalid_evidence_contracts_rejected(change):
    with pytest.raises(ValidationError):
        source(**change)


def test_unknown_cross_timezone_date_does_not_become_a_deadline(tmp_path):
    sources = []
    for sid in ("issuer", "independent"):
        ev = event(source=sid)
        announcement = ev.announcement.model_copy(
            update={
                "original_timezone": "Asia/Tokyo",
                "normalized_session_date": None,
            }
        )
        sources.append(source(sid, events=(ev.model_copy(update={"announcement": announcement}),)))
    result = calculate(tmp_path, sources=sources)
    assert result.status == "UNAVAILABLE"
    assert result.pre_event_deadline is None
    assert result.comparisons["full_holding_window"] is None


def test_unknown_publication_blocks_even_with_known_event_date(tmp_path):
    ev = event()
    ev = ev.model_copy(
        update={"announcement": ev.announcement.model_copy(update={"published_at": None})}
    )
    result = calculate(tmp_path, sources=(source(events=(ev,)), source("independent")))
    assert result.status == "UNAVAILABLE"
    assert "issuer:EVENT_PUBLICATION_UNVERIFIED" in result.unavailable_reasons


@pytest.mark.parametrize("field", ["released_at", "call_completed_at", "published_at"])
def test_coverage_cannot_precede_retained_event_information(field):
    ev = event(
        date(2026, 11, 6),
        release_state="CONFIRMED",
        released_at=datetime(2026, 11, 6, 14, tzinfo=UTC),
        call_state="CONFIRMED",
        call_completed_at=datetime(2026, 11, 6, 15, tzinfo=UTC),
    )
    stamp = AS_OF - timedelta(minutes=1)
    if field == "published_at":
        ev = ev.model_copy(
            update={"announcement": ev.announcement.model_copy(update={field: stamp})}
        )
    else:
        update = {field: stamp}
        if field == "released_at":
            update["call_completed_at"] = stamp
        ev = ev.model_copy(update=update)
    with pytest.raises(ValidationError, match="postdates"):
        source(
            recent_event_state="RETAINED",
            recent_event=ev.model_copy(update={"event_id": "past-cycle"}),
        )


def test_distinct_event_order_is_not_a_source_conflict(tmp_path):
    sources = []
    for sid in ("issuer", "independent"):
        events = (event(source=sid), event(date(2027, 1, 5), source=sid, event_id="2027-Q1"))
        sources.append(source(sid, events=events if sid == "issuer" else tuple(reversed(events))))
    result = calculate(tmp_path, sources=sources)
    assert result.status == "PASS"


def test_future_evidence_does_not_supply_dates_or_failures(tmp_path):
    future = source(events=(event(date(2026, 11, 7)),), available_at=AS_OF + timedelta(seconds=1))
    result = calculate(tmp_path, sources=(future, source("independent")))
    assert result.status == "UNAVAILABLE"
    assert result.failed_conditions == ()
    assert result.pre_event_deadline == datetime(2026, 12, 7, 21, tzinfo=UTC)
    assert "EARNINGS_SOURCE_CONFLICT" not in result.unavailable_reasons


def test_same_day_event_and_unconfirmed_recent_release_block(tmp_path):
    sources = tuple(
        source(s, events=(event(date(2026, 11, 6), source=s),)) for s in ("issuer", "independent")
    )
    result = calculate(tmp_path, sources=sources)
    assert result.status == "FAIL"
    assert result.pre_event_deadline == datetime(2026, 11, 5, 21, tzinfo=UTC)
    sources = tuple(
        source(
            s,
            recent_event_state="RETAINED",
            recent_event=event(date(2026, 11, 5), source=s, event_id="past-cycle"),
        )
        for s in ("issuer", "independent")
    )
    result = calculate(tmp_path, sources=sources)
    assert result.status == "UNAVAILABLE"
    assert "issuer:EVENT_COMPLETION_UNCONFIRMED" in result.unavailable_reasons


def test_unknown_timezone_is_a_validation_error():
    with pytest.raises(ValidationError, match="timezone"):
        source(exchange_timezone="Not/AZone")


def test_walkthrough_runs_offline_and_reopens_in_a_fresh_process(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    command = [sys.executable, "-m", "stock_analyser.earnings_example", "--output", str(tmp_path)]
    first = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
    for name, state in (
        ("fits", "PASS"),
        ("overlap", "FAIL"),
        ("unknown-coverage", "UNAVAILABLE"),
        ("conflicting-date", "UNAVAILABLE"),
        ("unqualified-source", "UNAVAILABLE"),
    ):
        result = EarningsAssessment.model_validate_json((tmp_path / f"{name}.json").read_bytes())
        assert result.status == state
    original = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    second = subprocess.run(command, env=env, text=True, capture_output=True, check=True)
    assert second.stdout == first.stdout
    assert {p.name: p.read_bytes() for p in tmp_path.glob("*.json")} == original
