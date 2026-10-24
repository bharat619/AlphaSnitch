"""Coverage must fail closed even when the vendor's paginated query completes."""

import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from stock_analyser.cli import app
from stock_analyser.massive import HttpResponse, MassiveClient, MassiveSettings, ProviderAllowance
from stock_analyser.universe_spike import (
    POLICY,
    Enumeration,
    enumerate_massive,
    enumerate_yahoo,
    estimate_workload,
    reconcile,
    reference_exclusions,
    run_spike,
)


def reference(ticker="AAA", **overrides):
    return {
        "ticker": ticker,
        "active": True,
        "market": "stocks",
        "locale": "us",
        "type": "CS",
        "primary_exchange": "XNAS",
        "currency_name": "usd",
        "share_class_figi": "share-" + ticker,
        "composite_figi": "listing-" + ticker,
        **overrides,
    }


def yahoo(symbols, *, start=0, total=None):
    return {
        "start": start,
        "total": len(symbols) if total is None else total,
        "count": len(symbols),
        "quotes": [{"symbol": s} for s in symbols],
    }


@pytest.fixture
def client_factory(tmp_path):
    @contextmanager
    def make(pages, **settings):
        calls = []
        replies = iter(pages)

        def transport(target, key, timeout):
            calls.append(target)
            payload = next(replies)
            if isinstance(payload, Exception):
                raise payload
            return HttpResponse(200, {}, json.dumps(payload).encode())

        with ProviderAllowance(tmp_path / "lock", 5, sleep=lambda _: None) as allowance:
            yield (
                MassiveClient(
                    SecretStr("never-retain-secret"),
                    allowance,
                    MassiveSettings(retries=0, **settings),
                    transport,
                ),
                calls,
            )

    return make


def test_massive_follows_all_pages_and_strips_cursor_credentials(client_factory):
    pages = [
        {
            "results": [reference()],
            "next_url": "https://api.massive.com/v3/reference/tickers?cursor=abc&apiKey=secret",
        },
        {"results": [reference("BBB")]},
    ]
    with client_factory(pages) as (client, calls):
        result = enumerate_massive(client)
    assert result.status == "COMPLETE"
    assert len(result.rows) == 2
    assert "secret" not in calls[1]
    assert "locale=us" in calls[1] and "active=true" in calls[1]
    assert "cursor" not in json.dumps(result.evidence())


@pytest.mark.parametrize(
    "next_url",
    [
        "https://evil.test/steal?apiKey=secret",
        "https://api.massive.com/v3/reference/tickers?cursor=abc",
    ],
)
def test_massive_unsafe_or_repeating_cursor_cannot_pass(client_factory, next_url):
    with client_factory(
        [
            {"results": [reference()], "next_url": next_url},
            {"results": [reference("BBB")], "next_url": next_url},
        ]
    ) as (client, _):
        result = enumerate_massive(client)
    assert result.status == "FAILED"
    assert "secret" not in json.dumps(result.evidence())


def test_massive_page_limit_is_sample_and_request_budget_is_failure(client_factory):
    page = {
        "results": [reference()],
        "next_url": "https://api.massive.com/v3/reference/tickers?cursor=abc",
    }
    with client_factory([page]) as (client, calls):
        assert enumerate_massive(client, max_pages=1).status == "TRUNCATED"
        assert len(calls) == 1
    with client_factory([page], max_requests=1) as (client, calls):
        assert enumerate_massive(client).status == "FAILED"
        assert len(calls) == 1


@pytest.mark.parametrize(
    "pages,reason",
    [
        ([{"results": [reference(), reference()]}], "DUPLICATE_IDENTIFIER"),
        ([{"results": []}], "TERMINAL_PAGE"),
        ([{"results": "invalid"}], "RETRIEVAL_OR_SCHEMA_FAILURE"),
        ([OSError("secret credential")], "RETRIEVAL_OR_SCHEMA_FAILURE"),
    ],
)
def test_massive_empty_duplicate_and_transport(client_factory, pages, reason):
    with client_factory(pages) as (client, _):
        result = enumerate_massive(client)
    assert result.reason == reason
    assert (result.status == "COMPLETE") == (reason == "TERMINAL_PAGE")


def test_yahoo_reconciles_total_with_unique_results():
    offsets = []

    def screen(offset, size):
        offsets.append(offset)
        return yahoo(["AAA", "BBB"] if offset == 0 else ["CCC"], start=offset, total=3)

    result = enumerate_yahoo(screen, page_size=2)
    assert result.status == "COMPLETE"
    assert result.reported_total == len(result.rows) == 3
    assert offsets == [0, 2]
    assert enumerate_yahoo(screen, page_size=2, max_pages=1).status == "TRUNCATED"


@pytest.mark.parametrize(
    "second,reason",
    [
        (yahoo(["BBB"], start=1, total=3), "TOTAL_CHANGED_DURING_PAGINATION"),
        (yahoo(["AAA"], start=1, total=2), "DUPLICATE_IDENTIFIER"),
        (yahoo([], start=1, total=2), "TOTAL_MISMATCH_OR_EMPTY_PAGE"),
        (yahoo(["BBB"], start=0, total=2), "RETRIEVAL_OR_SCHEMA_FAILURE"),
        ({"start": 1, "count": 1, "quotes": [{"symbol": "BBB"}]}, "RETRIEVAL_OR_SCHEMA_FAILURE"),
    ],
)
def test_yahoo_changing_population_and_malformed_pagination_fail(second, reason):
    pages = iter([yahoo(["AAA"], total=2), second])
    result = enumerate_yahoo(lambda *_: next(pages), page_size=1)
    assert result.status == "FAILED"
    assert result.reason == reason


@pytest.mark.parametrize(
    "fields,reason",
    [
        ({"type": "ADRC"}, "NON_COMMON_OR_UNKNOWN_TYPE"),
        ({"type": "ETF"}, "NON_COMMON_OR_UNKNOWN_TYPE"),
        ({"type": "PFD"}, "NON_COMMON_OR_UNKNOWN_TYPE"),
        ({"type": None}, "NON_COMMON_OR_UNKNOWN_TYPE"),
        ({"primary_exchange": "OTC"}, "UNSUPPORTED_OR_UNKNOWN_VENUE"),
        ({"active": False}, "INACTIVE_OR_UNKNOWN"),
        ({"currency_name": None}, "NON_USD_OR_UNKNOWN_CURRENCY"),
        ({"share_class_figi": None}, "UNKNOWN_SECURITY_IDENTITY"),
    ],
)
def test_eligibility_is_explicit(fields, reason):
    assert reason in reference_exclusions(reference(**fields))


def test_dedup_preserves_share_classes_and_blocks_identity_conflicts():
    rows = [
        reference(),
        reference("ALT", share_class_figi="share-AAA", primary_exchange="XNYS"),
        reference("CLASSB"),
        reference("BAD", composite_figi="listing-CLASSB"),
    ]
    discovery = Enumeration("yfinance", {}, rows=[{"symbol": "AAA"}, {"symbol": "ALT"}])
    result = reconcile(Enumeration("massive", {}, rows=rows), discovery)
    assert result["distinct_candidate_securities"] == 1
    assert result["duplicate_listings_removed"] == 1
    assert result["discovery_eligible_security_matches"] == 1
    assert result["reference_exclusions"]["CONFLICTING_LISTING_IDENTITY"] == 2
    rows = [reference(), reference("CLASSB")]
    assert (
        reconcile(Enumeration("massive", {}, rows=rows), discovery)["distinct_candidate_securities"]
        == 2
    )


def test_saved_evidence_never_promotes_a_sample_or_leaks_payloads(client_factory, tmp_path):
    output = tmp_path / "findings"
    with client_factory([{"results": [reference(secret="secret-payload")]}]) as (client, _):
        report = run_spike(
            client,
            lambda *_: yahoo(["AAA"]),
            lambda s: {"symbol": s, "sector": "Technology", "secret": "payload"},
            output,
        )
    saved = (output / "findings.json").read_text()
    assert "secret" not in saved and "payload" not in saved
    assert report == json.loads(saved)
    assert report["gate"]["status"] == "FAIL"
    assert not report["gate"]["full_sector_accepted"]
    assert report["measured"]["profiles"]["classified_technology"] == 1
    assert report["measured"]["yahoo_http_requests"] is None
    assert report["policy"] == POLICY
    with pytest.raises(FileExistsError):
        run_spike(None, None, None, output)


def test_workload_prices_every_candidate_and_keeps_replay_separate():
    estimate = estimate_workload(
        reference_pages=12, discovery_pages=4, candidate_securities=4000, sector_candidates=700
    )
    assert estimate["cold_start"] == 4749
    assert estimate["warm_next_session_metadata_fresh"] == 733
    assert estimate["warm_after_metadata_ttl"] == 4749
    assert estimate["warm_exact_snapshot_replay"] == 0
    assert estimate["cold_upper_scenario_two_retries_and_three_pages"] == 9 * 4749


@pytest.mark.parametrize("sector", ["Unfamiliar", "", None, True])
def test_unknown_classifications_never_become_known_non_technology(
    client_factory, tmp_path, sector
):
    with client_factory([{"results": [reference()]}]) as (client, _):
        report = run_spike(
            client,
            lambda *_: yahoo(["AAA"]),
            lambda s: {"symbol": s, "sector": sector},
            tmp_path / "run",
        )
    assert report["measured"]["profiles"]["unknown_or_failed"] == 1
    assert report["measured"]["profiles"]["other_sector"] == 0
    assert not report["gate"]["full_sector_accepted"]


def test_cli_failed_gate_returns_nonzero_and_preserves_report(
    monkeypatch, tmp_path, client_factory
):
    import yfinance as yf

    import stock_analyser.config as config
    import stock_analyser.massive as massive

    @contextmanager
    def client(*args):
        with client_factory([{"results": [reference()]}]) as (client, _):
            yield client

    monkeypatch.setattr(config, "load_massive_key", lambda: SecretStr("secret"))
    monkeypatch.setattr(massive, "live_client", client)
    monkeypatch.setattr(yf, "screen", lambda *a, **kw: yahoo(["AAA"]))
    monkeypatch.setattr(
        yf,
        "Ticker",
        lambda symbol: SimpleNamespace(get_info=lambda: {"symbol": symbol, "sector": "Technology"}),
    )
    result = CliRunner().invoke(app, ["qualify-universe", "--output", str(tmp_path / "new")])
    assert result.exit_code == 1
    assert "Full-sector acceptance: FAIL" in result.output
    path = tmp_path / "new" / "findings.json"
    saved = path.read_bytes()
    assert json.loads(saved)["gate"]["full_sector_accepted"] is False
    result = CliRunner().invoke(app, ["qualify-universe", "--output", str(tmp_path / "new")])
    assert result.exit_code == 1
    assert "cannot be overwritten" in result.output
    assert path.read_bytes() == saved
