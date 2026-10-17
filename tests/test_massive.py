"""Controlled provider failures exercise production boundaries without live quota."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from pydantic import SecretStr

from stock_analyser.calendars import CutoffPolicy, ExchangeCalendarAdapter, HistoryResolver
from stock_analyser.collection import validate_prices
from stock_analyser.contracts import HistoryRequirement
from stock_analyser.live import smoke_instrument
from stock_analyser.markets import US_MARKET
from stock_analyser.massive import (
    HttpResponse,
    MassiveActionsProvider,
    MassiveClient,
    MassiveError,
    MassiveEvidenceProvider,
    MassivePriceProvider,
    MassiveSettings,
    ProviderAllowance,
)


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def reply(payload=None, status=200, headers=None):
    return HttpResponse(status, headers or {}, json.dumps(payload or {"status": "OK"}).encode())


@pytest.fixture
def client_factory(tmp_path):
    leases = []

    def make(responses, **settings):
        clock = Clock()
        lease = ProviderAllowance(
            tmp_path / str(len(leases)), 5, clock=clock.time, sleep=clock.sleep
        )
        lease.__enter__()
        leases.append(lease)
        calls = []
        responses = iter(responses)

        def transport(target, key, timeout):
            calls.append((target, clock.now, timeout))
            assert key.get_secret_value() == "secret-do-not-retain"
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        client = MassiveClient(
            SecretStr("secret-do-not-retain"), lease, MassiveSettings(**settings), transport
        )
        return client, calls, clock

    yield make
    for lease in leases:
        lease.__exit__()


def resolution(timeframe="daily", n=3):
    return HistoryResolver(
        ExchangeCalendarAdapter(),
        CutoffPolicy(
            earliest_session=date(2026, 9, 1),
            publication_timing="unverified",
        ),
    ).resolve(
        US_MARKET,
        "XNAS",
        HistoryRequirement(timeframe=timeframe, minimum_bars=n),
        datetime(2026, 10, 17, tzinfo=UTC),
    )


def row(session):
    stamp = int(
        datetime.combine(
            session, datetime.min.time(), tzinfo=ZoneInfo("America/New_York")
        ).timestamp()
        * 1000
    )
    return {"t": stamp, "o": 10, "h": 12, "l": 9, "c": 11, "v": 1.25}


def prices(rows, **extra):
    return reply({"status": "OK", "ticker": "AAPL", "adjusted": True, "results": rows, **extra})


def test_pages_and_retries_share_one_allowance(client_factory):
    responses = [
        reply(status=429, headers={"Retry-After": "30"}),
        reply(status=503),
        reply({"next_url": "https://api.massive.com/test?cursor=x"}),
        reply(),
    ]
    client, calls, clock = client_factory(responses)
    assert len(list(client.pages("/test", {}))) == 2
    assert [c[1] for c in calls] == [1012, 1042, 1054, 1066]
    assert clock.sleeps == [12, 30, 12, 12]
    assert all(c[2] == 20 for c in calls)


def test_http_date_retry_after(client_factory):
    client, calls, _ = client_factory(
        [
            reply(status=429, headers={"retry-after": "Thu, 01 Jan 1970 00:17:30 GMT"}),
            reply(),
        ]
    )
    client.request("/test")
    assert [c[1] for c in calls] == [1012, 1050]


@pytest.mark.parametrize("status", [401, 403, 404, 301, 302])
def test_nonretryable_errors_do_not_echo_bodies(client_factory, status):
    client, calls, _ = client_factory([reply({"error": "secret-do-not-retain"}, status=status)])
    with pytest.raises(MassiveError) as exc:
        client.request("/test")
    assert "secret" not in str(exc.value)
    assert len(calls) == 1


@pytest.mark.parametrize("value", ["NaN", "Infinity", "secret-do-not-retain", "9999"])
def test_bad_retry_delays_fail_safely(client_factory, value):
    client, calls, _ = client_factory([reply(status=429, headers={"Retry-After": value})])
    with pytest.raises(MassiveError):
        client.request("/test")
    assert len(calls) == 1


def test_network_and_server_failures_are_bounded(client_factory):
    client, calls, _ = client_factory([OSError("secret-do-not-retain")] * 3)
    with pytest.raises(MassiveError, match="transport failed"):
        client.request("/test")
    assert len(calls) == 3
    client, calls, _ = client_factory([reply(status=503)] * 3)
    with pytest.raises(MassiveError, match="exhausted retries"):
        client.request("/test")
    assert len(calls) == 3


@pytest.mark.parametrize(
    "payload",
    [b"bad", b"[]", b'{"status":"ERROR"}', b'{"status":"OK","error":"secret-do-not-retain"}'],
)
def test_invalid_success_payload(client_factory, payload):
    client, _, _ = client_factory([HttpResponse(200, {}, payload)])
    with pytest.raises(MassiveError, match="invalid or error payload"):
        client.request("/test")


@pytest.mark.parametrize(
    "destination",
    [
        "http://api.massive.com/test",
        "https://evil.example/test",
        "//api.massive.com/test",
        "https://api.massive.com.evil/test",
        "https://user@api.massive.com/test",
        "https://api.massive.com:443/test",
        "https://api.massive.com/other?cursor=x",
        "https://api.massive.com/test?unexpected=1",
        "https://api.massive.com/test#fragment",
    ],
)
def test_unsafe_pagination_stops_before_sending_auth(client_factory, destination):
    client, calls, _ = client_factory([reply({"next_url": destination})])
    with pytest.raises(MassiveError, match="unsafe pagination"):
        list(client.pages("/test", {}))
    assert len(calls) == 1


def test_pagination_cycle_and_request_budget(client_factory):
    client, calls, _ = client_factory(
        [
            reply({"next_url": "https://api.massive.com/test?cursor=x"}),
            reply({"next_url": "https://api.massive.com/test?cursor=x"}),
        ]
    )
    with pytest.raises(MassiveError, match="cycle"):
        list(client.pages("/test", {}))
    assert len(calls) == 2
    client, calls, _ = client_factory([reply(status=429)], max_requests=1)
    with pytest.raises(MassiveError, match="budget"):
        client.request("/test")
    assert len(calls) == 1


def test_pacing_is_shared_between_threads(client_factory):
    client, calls, _ = client_factory([reply()] * 8)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(client.request, ["/test"] * 8))
    assert [c[1] for c in calls] == list(range(1012, 1108, 12))


def test_competing_process_and_restart_cooldown(tmp_path):
    path = tmp_path / "massive.lock"
    clock = Clock()
    with ProviderAllowance(path, 5, clock=clock.time, sleep=clock.sleep) as lease:
        lease.acquire()
        lease.defer(90)
        script = (
            "from pathlib import Path; from stock_analyser.massive import ProviderAllowance; "
            f"ProviderAllowance(Path({str(path)!r}), 5).__enter__()"
        )
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
        assert result.returncode != 0
        assert "Another local Massive collection" in result.stderr
    with ProviderAllowance(path, 5, clock=clock.time, sleep=clock.sleep) as lease:
        lease.acquire()
    assert clock.now == 1102


def test_price_pages_preserve_decimals_and_do_not_store_echoed_credentials(client_factory):
    plan = resolution()
    rows = [row(p.session_end) for p in plan.periods]
    path = "/v2/aggs/ticker/AAPL/range/1/day/2026-10-15/2026-10-16"
    page1 = prices(
        rows[:1],
        next_url=f"https://api.massive.com{path}?cursor=x&apiKey=echo-secret",
        message_ignored="secret-do-not-retain",
    )
    page2 = prices(rows[1:])
    client, calls, _ = client_factory([page1, page2])
    response = MassivePriceProvider(client).fetch(smoke_instrument("AAPL", "XNAS"), US_MARKET, plan)
    bars, findings, count = validate_prices(response, plan)
    assert count == 3 and bars[0].volume == Decimal("1.25")
    assert {f.code for f in findings} == {"PUBLICATION_UNVERIFIED"}
    for target, _, _ in calls:
        assert "apiKey" not in target
        assert parse_qs(urlsplit(target).query)["adjusted"] == ["true"]
    assert b"secret" not in response.payload
    assert b"next_url" not in response.payload
    assert len(json.loads(response.payload)["daily_source"]) == 3


@pytest.mark.parametrize(
    "mutation",
    [
        lambda rows: rows.append(rows[0]),
        lambda rows: rows.reverse(),
        lambda rows: rows[0].update(v=None),
        lambda rows: rows[0].update(t="bad"),
        lambda rows: rows[0].update(h=1),
        lambda rows: rows[0].update(t=rows[0]["t"] + 1),
    ],
)
def test_price_contract_failures(client_factory, mutation):
    plan = resolution()
    rows = [row(p.session_end) for p in plan.periods]
    mutation(rows)
    client, _, _ = client_factory([prices(rows)])
    with pytest.raises(MassiveError):
        MassivePriceProvider(client).fetch(smoke_instrument("AAPL", "XNAS"), US_MARKET, plan)


@pytest.mark.parametrize("metadata", [{"adjusted": False}, {"ticker": "SPY"}])
def test_wrong_adjustment_or_ticker_rejected(client_factory, metadata):
    client, _, _ = client_factory([prices([], **metadata)])
    with pytest.raises(MassiveError, match="metadata mismatch"):
        MassivePriceProvider(client).fetch(
            smoke_instrument("AAPL", "XNAS"), US_MARKET, resolution()
        )


def test_weekly_missing_daily_constituent_cannot_be_hidden(client_factory):
    plan = resolution("weekly", 2)
    days = ExchangeCalendarAdapter().schedule(
        US_MARKET, "XNAS", plan.history_start, plan.session_date
    )
    rows = [row(s.session) for s in days]
    client, _, _ = client_factory([prices(rows)])
    response = MassivePriceProvider(client).fetch(smoke_instrument("AAPL", "XNAS"), US_MARKET, plan)
    bars, _, count = validate_prices(response, plan)
    assert count == 2 and bars[-1].volume == Decimal("6.25")
    client, _, _ = client_factory([prices(rows[:-1])])
    response = MassivePriceProvider(client).fetch(smoke_instrument("AAPL", "XNAS"), US_MARKET, plan)
    _, findings, count = validate_prices(response, plan)
    assert count == 1
    assert {"MISSING_SESSION", "INSUFFICIENT_HISTORY"} <= {f.code for f in findings}


def test_corporate_actions_are_allowlisted_and_in_snapshot(client_factory):
    plan = resolution()
    client, calls, _ = client_factory(
        [
            prices([row(p.session_end) for p in plan.periods]),
            reply(
                {
                    "results": [
                        {
                            "ticker": "AAPL",
                            "execution_date": "2026-10-15",
                            "split_from": 1,
                            "split_to": 2,
                            "extra": "secret-do-not-retain",
                        }
                    ]
                }
            ),
            reply(
                {
                    "results": [
                        {
                            "ticker": "AAPL",
                            "ex_dividend_date": "2026-10-16",
                            "cash_amount": 0.25,
                            "currency": "USD",
                        }
                    ]
                }
            ),
        ]
    )
    response = MassiveEvidenceProvider(client).fetch(
        smoke_instrument("AAPL", "XNAS"), US_MARKET, plan
    )
    payload = json.loads(response.payload)
    assert len(payload["corporate_actions"]["actions"]) == 2
    assert payload["corporate_actions"]["actions"][1]["cash_amount"] == "0.25"
    assert len(payload["capability_observations"]) == 3
    assert b"secret" not in response.payload
    assert len(calls) == 3


def test_corporate_actions_reject_foreign_symbol(client_factory):
    client, _, _ = client_factory(
        [
            reply(
                {
                    "results": [
                        {
                            "ticker": "SPY",
                            "execution_date": "2026-10-15",
                            "split_from": 1,
                            "split_to": 2,
                        }
                    ]
                }
            )
        ]
    )
    with pytest.raises(MassiveError, match="contract validation"):
        MassiveActionsProvider(client).fetch(
            smoke_instrument("AAPL", "XNAS"), US_MARKET, date(2026, 10, 14), date(2026, 10, 16)
        )


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize("retry_after", ["300", "Thu, 01 Jan 1970 00:21:52 GMT"])
def test_excessive_retry_delay_stops_shared_allowance_and_survives_restart(
    client_factory, status, retry_after
):
    from stock_analyser.massive import ProviderCooldownError

    client, calls, clock = client_factory(
        [
            reply(status=status, headers={"Retry-After": retry_after}),
            reply(),
        ]
    )
    with pytest.raises(ProviderCooldownError):
        client.request("/stock")
    other = MassiveClient(
        SecretStr("secret-do-not-retain"), client.allowance, client.settings, client.transport
    )
    with pytest.raises(ProviderCooldownError):
        other.request("/benchmark")
    assert len(calls) == 1 and clock.sleeps == [12]
    client.allowance.__exit__()
    # Windows byte locks also deny reads through a second handle. Verify the
    # persisted deadline after releasing the lease, before testing a restart.
    assert float(client.allowance.path.read_text()) == 1312
    with ProviderAllowance(client.allowance.path, 5, clock=clock.time, sleep=clock.sleep) as lease:
        restarted = MassiveClient(
            SecretStr("secret-do-not-retain"), lease, client.settings, client.transport
        )
        with pytest.raises(ProviderCooldownError):
            restarted.request("/benchmark")
    assert len(calls) == 1 and clock.sleeps == [12]
    clock.now = 1312
    with ProviderAllowance(client.allowance.path, 5, clock=clock.time, sleep=clock.sleep) as lease:
        restarted = MassiveClient(
            SecretStr("secret-do-not-retain"), lease, client.settings, client.transport
        )
        restarted.request("/benchmark")
    assert len(calls) == 2 and calls[-1][1] >= 1312


def test_windows_lock_uses_first_byte_and_nonblocking_mode(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from stock_analyser import massive

    calls = []
    monkeypatch.setitem(
        sys.modules,
        "msvcrt",
        SimpleNamespace(
            LK_NBLCK=2,
            locking=lambda fd, mode, size: calls.append(
                (fd, mode, size, os.lseek(fd, 0, os.SEEK_CUR))
            ),
        ),
    )
    monkeypatch.setattr(massive.sys, "platform", "win32")
    path = tmp_path / "provider.lock"
    path.write_text("existing pacing state")
    with path.open("a+", encoding="ascii") as stream:
        stream.seek(0, 2)
        massive._lock_file(stream)
        assert calls == [(stream.fileno(), 2, 1, 0)]
