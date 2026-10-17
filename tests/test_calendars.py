from datetime import UTC, date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_analyser.calendars import (
    CutoffPolicy,
    ExchangeCalendarAdapter,
    HistoryResolver,
    ResolutionReason,
    ResolvedHistory,
    assess_history,
)
from stock_analyser.contracts import AnalysisRequest, HistoryRequirement
from stock_analyser.fixtures import synthetic_session_calendars
from stock_analyser.markets import US_MARKET
from stock_analyser.persistence.records import RunRecord


def resolve(instant, timeframe="daily", **policy):
    return HistoryResolver(ExchangeCalendarAdapter(), CutoffPolicy(**policy)).resolve(
        US_MARKET,
        "XNYS",
        HistoryRequirement(timeframe=timeframe, minimum_bars=1),
        datetime.fromisoformat(instant),
    )


@pytest.mark.parametrize("venue", ["XNYS", "XNAS", "ARCX"])
def test_maintained_venue_schedules(venue):
    adapter = ExchangeCalendarAdapter()
    sessions = adapter.schedule(US_MARKET, venue, date(2026, 11, 26), date(2026, 11, 27))
    assert len(sessions) == 1  # Thanksgiving is closed.
    assert sessions[0].session == date(2026, 11, 27)
    assert sessions[0].closes_at == datetime(2026, 11, 27, 18, tzinfo=UTC)
    assert adapter.data_version(US_MARKET, venue).startswith("pandas-market-calendars:")


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-11-27T17:59:59+00:00", "2026-11-25"),
        ("2026-11-27T18:00:00+00:00", "2026-11-25"),
        ("2026-11-27T18:14:59+00:00", "2026-11-25"),
        ("2026-11-27T18:15:00+00:00", "2026-11-27"),
        ("2026-11-26T21:30:00+00:00", "2026-11-25"),
        ("2026-11-28T18:15:00+00:00", "2026-11-27"),
    ],
)
def test_early_close_and_holiday_grace_boundaries(instant, expected):
    result = resolve(instant)
    assert result.session_date == date.fromisoformat(expected)
    assert result.session_cutoff <= result.requested_as_of
    assert result.periods[-1].available_at <= result.requested_as_of


@pytest.mark.parametrize(
    ("instant", "hour"),
    [
        ("2026-03-06T22:00:00+00:00", 21),
        ("2026-03-09T22:00:00+00:00", 20),
        ("2026-10-30T22:00:00+00:00", 20),
        ("2026-11-02T22:00:00+00:00", 21),
    ],
)
def test_daylight_saving_transitions(instant, hour):
    assert resolve(instant).session_cutoff.hour == hour


def test_maintained_exceptional_closure():
    result = resolve("2025-01-09T23:00:00+00:00")  # Carter national mourning.
    assert result.session_date == date(2025, 1, 8)
    assert ResolutionReason.MARKET_CLOSED in result.reasons


@pytest.mark.parametrize(
    ("instant", "expected"),
    [
        ("2026-04-02T19:59:59+00:00", "2026-03-27"),
        ("2026-04-02T20:14:59+00:00", "2026-03-27"),
        ("2026-04-02T20:15:00+00:00", "2026-04-02"),
        ("2026-04-03T12:00:00+00:00", "2026-04-02"),
        ("2026-11-27T18:14:59+00:00", "2026-11-20"),
        ("2026-11-27T18:15:00+00:00", "2026-11-27"),
    ],
)
def test_completed_holiday_shortened_weeks(instant, expected):
    assert resolve(instant, "weekly").session_date == date.fromisoformat(expected)


def test_midweek_is_never_treated_as_complete():
    assert resolve("2026-11-25T22:00:00+00:00", "weekly").session_date == date(2026, 11, 20)


def test_weekly_has_independent_grace_and_warmup_history():
    instant = "2026-10-16T20:30:00+00:00"
    policy = {"daily_grace_seconds": 0, "weekly_grace_seconds": 3600}
    assert resolve(instant, **policy).session_date == date(2026, 10, 16)
    weekly = resolve(instant, "weekly", **policy)
    assert weekly.session_date == date(2026, 10, 9)
    assert ResolutionReason.PUBLICATION_DELAYED in weekly.reasons
    resolver = HistoryResolver(ExchangeCalendarAdapter())
    plans = tuple(
        resolver.resolve(US_MARKET, "XNYS", requirement, datetime(2026, 10, 16, 22, tzinfo=UTC))
        for requirement in (
            HistoryRequirement(timeframe="daily", minimum_bars=260, warmup_bars=40),
            HistoryRequirement(timeframe="weekly", minimum_bars=200, warmup_bars=20),
        )
    )
    assert tuple(len(plan.periods) for plan in plans) == (300, 220)
    assert plans[1].history_start < plans[0].history_start - timedelta(days=700)


def test_requested_utc_is_separate_from_local_dates_and_persists():
    requested = datetime(2026, 11, 28, 1, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
    requirement = HistoryRequirement(timeframe="daily", minimum_bars=1)
    result = HistoryResolver(ExchangeCalendarAdapter()).resolve(
        US_MARKET, "XNYS", requirement, requested
    )
    assert result.requested_as_of == datetime(2026, 11, 27, 19, 30, tzinfo=UTC)
    assert result.requested_as_of.tzinfo == UTC
    assert result.session_date == date(2026, 11, 27)
    assert result.session_cutoff == datetime(2026, 11, 27, 18, tzinfo=UTC)
    assert ResolvedHistory.model_validate_json(result.model_dump_json()) == result
    run = RunRecord(
        run_id=uuid4(),
        request=AnalysisRequest(
            request_id=uuid4(),
            mode="buy",
            market_id="US",
            sector="technology",
            requested_as_of=requested,
            strategy_version="test",
            history=(requirement,),
        ),
        market=US_MARKET,
        configuration={},
        source_version={},
        component_versions={},
        history_resolutions=(result,),
    )
    assert RunRecord.model_validate_json(run.model_dump_json()) == run
    with pytest.raises(ValidationError, match="frozen run"):
        RunRecord.model_validate(
            run.model_dump()
            | {
                "history_resolutions": (
                    result.model_copy(update={"market_configuration_version": "bad"}),
                )
            }
        )


def test_closed_delayed_stale_and_short_history_are_distinct():
    closed = resolve("2026-11-29T15:00:00+00:00", publication_timing="synthetic")
    assert assess_history(closed, (closed.session_date,)) == (ResolutionReason.MARKET_CLOSED,)
    delayed = resolve("2026-11-27T18:05:00+00:00")
    assert ResolutionReason.PUBLICATION_DELAYED in delayed.reasons
    assert ResolutionReason.PUBLICATION_DELAYED not in closed.reasons
    older_only = assess_history(closed, (date(2026, 11, 25),))
    assert ResolutionReason.STALE_DATA in older_only
    assert ResolutionReason.INSUFFICIENT_HISTORY in older_only
    markets, calendars = synthetic_session_calendars()
    plan = HistoryResolver(calendars[0], CutoffPolicy(publication_timing="synthetic")).resolve(
        markets.resolve("US"),
        "XNAS",
        HistoryRequirement(timeframe="daily", minimum_bars=3),
        datetime(2026, 10, 16, 22, tzinfo=UTC),
    )
    short = assess_history(plan, (date(2026, 10, 15), date(2026, 10, 16)))
    assert ResolutionReason.INSUFFICIENT_HISTORY in short
    assert ResolutionReason.STALE_DATA not in short
    stale = assess_history(plan, (date(2026, 10, 14), date(2026, 10, 15)))
    assert ResolutionReason.STALE_DATA in stale
    assert ResolutionReason.INSUFFICIENT_HISTORY in stale
    gap = assess_history(plan, (date(2026, 10, 14), date(2026, 10, 16)))
    assert ResolutionReason.STALE_DATA in gap


def test_no_history_and_bounded_history_do_not_silently_shorten_requirements():
    no_history = resolve("1989-12-31T22:00:00+00:00")
    assert no_history.session_cutoff is None
    assert ResolutionReason.INSUFFICIENT_HISTORY in no_history.reasons
    markets, calendars = synthetic_session_calendars()
    result = HistoryResolver(
        calendars[0], CutoffPolicy(earliest_session=date(2026, 10, 5))
    ).resolve(
        markets.resolve("US"),
        "XNAS",
        HistoryRequirement(timeframe="weekly", minimum_bars=2, warmup_bars=1),
        datetime(2026, 10, 16, 22, tzinfo=UTC),
    )
    assert len(result.periods) == 2
    assert result.requirement.required_bars == 3
    assert ResolutionReason.INSUFFICIENT_HISTORY in result.reasons


def test_synthetic_second_market_resolves_daily_and_weekly_independently():
    markets, calendars = synthetic_session_calendars()
    for timeframe in ("daily", "weekly"):
        plans = tuple(
            HistoryResolver(
                calendar,
                CutoffPolicy(earliest_session=date(2026, 10, 5), publication_timing="synthetic"),
            ).resolve(
                market := markets.resolve(market_id),
                market.calendars[0].venue,
                HistoryRequirement(timeframe=timeframe, minimum_bars=1),
                datetime(2026, 10, 16, 15, tzinfo=UTC),
            )
            for market_id, calendar in zip(("US", "TEST"), calendars, strict=True)
        )
        expected = date(2026, 10, 15) if timeframe == "daily" else date(2026, 10, 9)
        assert plans[0].session_date == expected
        assert plans[1].session_date == date(2026, 10, 16)
        assert plans[0].session_cutoff != plans[1].session_cutoff
        assert plans[0].requested_as_of == plans[1].requested_as_of


def test_fail_closed_on_unsupported_naive_and_incomplete_data():
    resolver = HistoryResolver(ExchangeCalendarAdapter())
    requirement = HistoryRequirement(timeframe="weekly", minimum_bars=1)
    with pytest.raises(ValueError, match="timezone aware"):
        resolver.resolve(US_MARKET, "XNYS", requirement, datetime(2026, 10, 16))
    with pytest.raises(ValueError, match="Venue"):
        resolver.resolve(US_MARKET, "UNKNOWN", requirement, datetime.now(UTC))
    markets, _ = synthetic_session_calendars()
    with pytest.raises(ValueError, match="Unsupported maintained"):
        resolver.resolve(markets.resolve("TEST"), "XTEST", requirement, datetime.now(UTC))
    result = resolve("2026-10-14T22:00:00+00:00", "weekly")
    with pytest.raises(ValueError, match="completed history"):
        assess_history(result, (date(2026, 10, 14),))
    assert ResolutionReason.PUBLICATION_UNVERIFIED in result.reasons
    with pytest.raises(ValidationError, match="unpublished"):
        ResolvedHistory.model_validate(
            result.model_dump() | {"requested_as_of": result.session_cutoff - timedelta(seconds=1)}
        )
