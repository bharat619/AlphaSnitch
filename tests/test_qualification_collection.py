"""No live calls: bound the native Yahoo transport and complete reference enrichment."""

import json
from datetime import UTC, date, datetime, timedelta
from urllib.parse import unquote

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from stock_analyser.cli import app
from stock_analyser.massive import (
    HttpResponse,
    MassiveClient,
    MassivePageLimitError,
    MassiveSettings,
    ProviderAllowance,
)
from stock_analyser.qualification import (
    CollectionControls,
    InventoryEvidence,
    PageEvidence,
    file_hash,
)
from stock_analyser.qualification_budget import QualificationStop, RequestBudget
from stock_analyser.qualification_plan import calculate_population_plan
from stock_analyser.qualification_workflow import (
    WorkloadManifest,
    planned_manifest,
    preflight,
    run_inventory,
    run_population_enrichment,
    unknown_rights,
    yahoo_client,
)
from stock_analyser.qualification_yahoo import YahooQualificationClient, YahooResponse
from stock_analyser.universe_spike import digest


class Clock:
    def __init__(self):
        self.seconds = 0.0
        self.base = datetime.now(UTC) - timedelta(hours=1)

    def clock(self):
        return self.seconds

    def sleep(self, seconds):
        self.seconds += seconds

    def now(self):
        return self.base + timedelta(seconds=self.seconds)


def response(value, status=200, headers=()):
    return YahooResponse(
        status, headers, value.encode() if isinstance(value, str) else json.dumps(value).encode()
    )


def screen(rows, *, offset=0, total=None):
    return {
        "finance": {
            "error": None,
            "result": [
                {
                    "start": offset,
                    "count": len(rows),
                    "total": len(rows) if total is None else total,
                    "quotes": [{"symbol": s} for s in rows],
                }
            ],
        }
    }


def profile(symbol, sector="Technology"):
    return {
        "quoteSummary": {
            "error": None,
            "result": [
                {
                    "quoteType": {"symbol": symbol, "exchange": "NMS", "quoteType": "EQUITY"},
                    "assetProfile": {
                        "sector": sector,
                        "industry": "Software",
                        "ignored": "never-serialize-secret",
                    },
                    "price": {"currency": "USD"},
                }
            ],
        }
    }


def make_client(replies, *, cap=8, overall=100, deadline=180, retries=1):
    clock = Clock()
    replies = iter(replies)
    calls = []
    budget = RequestBudget(
        {"massive": 100, "yfinance": overall}, overall, deadline, clock=clock.clock
    )

    def transport(host, method, path, params, body, timeout):
        calls.append((host, method, path, dict(params), body, timeout))
        result = next(replies)
        if isinstance(result, Exception):
            raise result
        return result

    client = YahooQualificationClient(
        budget, http_cap=cap, retries=retries, transport=transport, sleep=clock.sleep
    )
    return client, budget, clock, calls


def test_native_yahoo_counts_cookie_crumb_data_and_preserves_returned_identity():
    client, budget, clock, calls = make_client(
        [
            response("", 404),
            response("sensitive-crumb"),
            response(screen(["AAA"])),
            response(profile("B-B")),
        ]
    )
    assert client.screen(0, 250)["quotes"] == [{"symbol": "AAA"}]
    result = client.profile("B-B")
    assert result["symbol"] == "B-B" and result["sector"] == "Technology"
    assert calls[-1][0] == "query2.finance.yahoo.com"
    assert calls[2][0] == "query1.finance.yahoo.com"
    assert calls[-1][3]["modules"] == "assetProfile,quoteType,price"
    assert calls[2][4]["query"]["operator"] == "AND"
    assert budget.counts["yfinance"] == 4 and client.logical_calls == 2
    assert clock.seconds == 3
    assert "sensitive-crumb" not in json.dumps(client.observations)
    client.close()
    assert client.crumb is None
    with pytest.raises(QualificationStop):
        client.profile("AAA")
    assert len(calls) == 4


@pytest.mark.parametrize(
    "cap,overall,code", [(3, 100, "YAHOO_OPERATION_LIMIT"), (8, 3, "REQUEST_LIMIT")]
)
def test_auth_refresh_cannot_escape_operation_or_shared_request_ceiling(cap, overall, code):
    client, budget, _, calls = make_client(
        [
            response(""),
            response("crumb"),
            response({}, 401),
            response(""),
            response("new-crumb"),
            response(screen(["AAA"])),
        ],
        cap=cap,
        overall=overall,
    )
    with pytest.raises(QualificationStop, match=code):
        client.screen(0, 250)
    assert len(calls) == budget.counts["yfinance"] == 3


def test_http_retry_cooldown_and_shared_deadline():
    client, budget, clock, calls = make_client(
        [
            response(""),
            response("crumb"),
            response({}, 429, (("Retry-After", "4"),)),
            response(screen(["AAA"])),
        ]
    )
    assert client.screen(0, 250)["total"] == 1
    assert budget.retries == 1 and budget.cooldown_seconds == 4
    assert clock.seconds == 6 and len(calls) == 4
    client, budget, clock, calls = make_client(
        [response(""), response("crumb"), response({}, 429, (("Retry-After", "30"),))], deadline=10
    )
    with pytest.raises(QualificationStop, match="COOLDOWN_LIMIT"):
        client.screen(0, 250)
    assert len(calls) == 3 and clock.seconds == 2


@pytest.mark.parametrize(
    "reply",
    [
        response("", 302, (("Location", "https://consent.yahoo.com/"),)),
        response("secret", 500, (("Retry-After", "nan"),)),
    ],
)
def test_no_redirect_consent_fallback_or_invalid_cooldown(reply):
    client, _, _, calls = make_client([reply])
    with pytest.raises(QualificationStop):
        client.screen(0, 250)
    assert len(calls) == 1


def test_mismatched_vendor_symbol_is_never_rewritten_to_requested_symbol():
    client, _, _, _ = make_client([response(""), response("crumb"), response(profile("WRONG"))])
    with pytest.raises(QualificationStop, match="PROFILE_IDENTITY"):
        client.profile("AAA")


def inventories(n=4, *, status="COMPLETE"):
    now = datetime.now(UTC)
    return tuple(
        InventoryEvidence(
            source=p,
            query=q,
            status=status,
            reason="COMPLETE",
            method=m,
            reported_total=1 if p == "yfinance" else None,
            raw_rows=n if p == "massive" else 1,
            unique_rows=n if p == "massive" else 1,
            duplicates=0,
            errors=0,
            count_kind="exact" if status == "COMPLETE" else "lower_bound",
            pages=(
                PageEvidence(
                    rows=n if p == "massive" else 1,
                    sha256=digest(p),
                    retrieved_at=now,
                    terminal=True,
                ),
            ),
        )
        for p, q, m in (
            ("massive", "active_us_stocks", "terminal_page"),
            ("yfinance", "us_technology_discovery", "reported_total"),
        )
    )


def controls():
    return CollectionControls(
        massive=MassiveSettings(
            retries=0, timeout_seconds=1, max_retry_delay_seconds=1, requests_per_minute=60
        ),
        massive_pages_per_operation=1,
        yahoo_http_per_operation=3,
        yahoo_timeout_seconds=1,
        yahoo_max_cooldown_seconds=1,
    )


def test_population_plan_calculates_all_candidates_benchmark_and_cold_warm_costs():
    plan = calculate_population_plan(inventories(), controls())
    assert plan.candidate_upper_bound == 4
    assert plan.cold_source_http_upper == {"massive": 6, "yfinance": 15}
    assert plan.warm_source_http_upper == {"massive": 5, "yfinance": 0}
    assert plan.total_http_upper == 26
    assert plan.network_wait_upper_seconds == 52
    assert plan.cold["identity_resolution_http"] == 0
    assert plan.sdk_calls == 0 and plan.history_windows_per_candidate == 1
    assert plan == calculate_population_plan(plan.inventories, plan.controls)
    with pytest.raises(ValueError, match="Incomplete"):
        calculate_population_plan(inventories(status="TRUNCATED"), controls())


def grants(now):
    return tuple(
        r.model_copy(
            update={
                "disposition": "permitted",
                "conditions": (),
                "document_sha256": digest("offline-fake-permission"),
                "retention_days": 365,
                "account_scope_sha256": digest("offline-test-account"),
            }
        )
        for r in unknown_rights(now)
    )


def test_altered_calculated_plan_or_insufficient_reviewed_capacity_blocks_preflight():
    manifest = WorkloadManifest(
        controls=controls(),
        request_ceiling=40,
        per_source_ceilings={"massive": 20, "yfinance": 20},
        rights=grants(datetime.now(UTC)),
    )
    manifest = planned_manifest(inventories(), manifest)
    assert manifest.estimated_http_upper_bound == 26
    assert preflight(manifest, now=datetime.now(UTC)) == []
    tampered = manifest.model_copy(
        update={
            "population_plan": manifest.population_plan.model_copy(update={"total_http_upper": 1})
        }
    )
    assert "WORKLOAD_PLAN_MISMATCH" in preflight(tampered, now=datetime.now(UTC))
    assert "CALCULATED_WORKLOAD_EXCEEDS_REVIEWED_LIMITS" in preflight(
        manifest.model_copy(update={"per_source_ceilings": {"massive": 20, "yfinance": 2}}),
        now=datetime.now(UTC),
    )


def ref(symbol, **updates):
    return {
        "ticker": symbol,
        "active": True,
        "market": "stocks",
        "locale": "us",
        "type": "CS",
        "primary_exchange": "XNAS",
        "currency_name": "USD",
        "share_class_figi": "share-" + symbol,
        "composite_figi": "listing-" + symbol,
        **updates,
    }


def mapping(row, symbol=None):
    return {
        "provider_symbol": symbol or row["ticker"],
        "venue": "XNAS",
        "currency": "USD",
        "type": "CS",
        "share_class_figi": row["share_class_figi"],
        "composite_figi": row["composite_figi"],
        "effective_from": "2020-01-01",
    }


@pytest.fixture
def collected(tmp_path):
    clock = Clock()
    rows = [ref("AAA"), ref("BBB"), ref("CCC"), ref("ETF", type="ETF")]
    manifest = WorkloadManifest(
        controls=controls(),
        rights=grants(clock.now()),
        request_ceiling=40,
        per_source_ceilings={"massive": 20, "yfinance": 20},
    )
    budget = RequestBudget(manifest.per_source_ceilings, 40, 180, clock=clock.clock)

    def massive_transport(target, key, timeout):
        budget.remaining()
        return HttpResponse(200, {}, json.dumps({"results": rows}).encode())

    def yahoo_transport(host, method, path, params, body, timeout):
        if host == "fc.yahoo.com":
            return response("", 404)
        if path.endswith("getcrumb"):
            return response("never-report-crumb")
        if path.endswith("screener"):
            return response(screen(["AAA"]))
        symbol = unquote(path.rsplit("/", 1)[-1])
        return response(profile(symbol, "Financial Services" if symbol == "CCC" else "Technology"))

    yahoo = yahoo_client(budget, manifest.controls, transport=yahoo_transport, sleep=clock.sleep)
    with ProviderAllowance(
        tmp_path / "massive.lock", 60, clock=clock.clock, sleep=clock.sleep
    ) as allowance:
        client = MassiveClient(
            SecretStr("no-live-key"),
            allowance,
            manifest.controls.massive,
            massive_transport,
            budget=budget,
            max_pages_per_operation=1,
        )
        output = tmp_path / "inventory"
        report = run_inventory(client, output, manifest, budget, now=clock.now, yahoo=yahoo)
    yahoo.close()
    assert all(i["status"] == "COMPLETE" for i in report["inventories"])
    planned = WorkloadManifest.model_validate_json(
        (output / "population-workload.json").read_text()
    )
    return clock, rows, planned, output, yahoo_transport


def test_native_enrichment_visits_full_reference_including_discovery_absence(collected, tmp_path):
    clock, rows, manifest, inventory, transport = collected
    maps = tmp_path / "reviewed-mappings.json"
    maps.write_text(
        json.dumps(
            {r["ticker"]: mapping(r, "B-B" if r["ticker"] == "BBB" else None) for r in rows[:3]}
        )
    )
    manifest = manifest.model_copy(update={"identity_mappings_sha256": file_hash(maps)})
    output = tmp_path / "population"
    report = run_population_enrichment(
        output,
        manifest,
        inventory / "private/inventory.json",
        maps,
        now=clock.now,
        transport=transport,
        sleep=clock.sleep,
        clock=clock.clock,
    )
    assert report["coverage"]["reference_rows"] == 4
    assert report["coverage"]["potentially_supported"] == 3
    assert report["coverage"]["technology"] == 2
    assert report["coverage"]["non_technology"] == 1
    assert report["coverage"]["resolved"] == 3
    assert report["history"]["not_attempted"] == 2
    assert report["budget"]["http_attempts"]["yfinance"] == 8
    assert report["budget"]["sdk_calls"] == 0
    assert report["workload_plan"]["total_http_upper"] == 26
    text = (output / "findings.json").read_text()
    assert (
        "never-report-crumb" not in text
        and "B-B" not in text
        and "never-serialize-secret" not in text
    )
    private = json.loads((output / "private/population.json").read_text())
    assert set(private["profiles"]) == {"AAA", "BBB", "CCC"}
    assert private["profiles"]["BBB"]["symbol"] == "B-B"
    with pytest.raises(ValueError, match="overwritten"):
        run_population_enrichment(
            output,
            manifest,
            inventory / "private/inventory.json",
            maps,
            now=clock.now,
            transport=lambda *a: pytest.fail("No calls on overwrite"),
        )


def test_unmatched_identity_stays_in_denominator_after_successful_profile_collection(
    collected, tmp_path
):
    clock, rows, manifest, inventory, transport = collected
    maps = tmp_path / "partial-mappings.json"
    maps.write_text(json.dumps({"AAA": mapping(rows[0]), "CCC": mapping(rows[2])}))
    manifest = manifest.model_copy(update={"identity_mappings_sha256": file_hash(maps)})
    report = run_population_enrichment(
        tmp_path / "unmatched",
        manifest,
        inventory / "private/inventory.json",
        maps,
        now=clock.now,
        transport=transport,
        sleep=clock.sleep,
        clock=clock.clock,
    )
    assert report["coverage"]["potentially_supported"] == 3
    assert report["coverage"]["unmatched"] == report["coverage"]["unknown"] == 1
    assert not report["gate"]["full_sector_accepted"]


def test_cli_population_plan_is_offline_and_uses_exact_complete_inventories(
    collected, tmp_path, monkeypatch
):
    _, _, _, inventory, _ = collected
    import stock_analyser.massive as massive

    monkeypatch.setattr(
        massive, "live_client", lambda *a, **k: pytest.fail("Offline planner called provider")
    )
    output = tmp_path / "offline-plan"
    result = CliRunner().invoke(
        app,
        [
            "qualify-universe",
            "--mode",
            "plan",
            "--inventory-evidence",
            str(inventory / "findings.json"),
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 1 and "Full-sector acceptance: FAIL" in result.output
    plan = WorkloadManifest.model_validate_json((output / "workload.json").read_text())
    assert plan.population_plan.candidate_upper_bound == 4
    assert plan.estimated_http_upper_bound == plan.population_plan.total_http_upper


def test_unknown_rights_block_all_native_io_before_reading_private_inputs(tmp_path, monkeypatch):
    import stock_analyser.qualification_workflow as workflow

    monkeypatch.setattr(
        workflow, "yahoo_client", lambda *a, **k: pytest.fail("Unknown rights must block Yahoo")
    )
    report = run_population_enrichment(
        tmp_path / "blocked",
        WorkloadManifest(),
        tmp_path / "missing-inventory",
        tmp_path / "missing-mappings",
    )
    assert not report["gate"]["full_sector_accepted"]
    assert report["budget"]["http_attempts"] == {"massive": 0, "yfinance": 0}


def test_massive_page_cap_binds_the_planners_history_costs(tmp_path):
    clock = Clock()
    calls = []
    with ProviderAllowance(
        tmp_path / "lease", 60, clock=clock.clock, sleep=clock.sleep
    ) as allowance:

        def transport(target, key, timeout):
            calls.append(target)
            return HttpResponse(
                200,
                {},
                json.dumps(
                    {
                        "results": [],
                        "next_url": "https://api.massive.com/v3/reference/ticker?cursor=do-not-store",
                    }
                ).encode(),
            )

        client = MassiveClient(
            SecretStr("secret"), allowance, controls().massive, transport, max_pages_per_operation=1
        )
        with pytest.raises(MassivePageLimitError):
            list(client.pages("/v3/reference/ticker", {}))
        assert len(calls) == 1


def test_enrichment_deadline_preserves_partial_report_without_more_requests(collected, tmp_path):
    clock, rows, manifest, inventory, transport = collected
    maps = tmp_path / "mappings.json"
    maps.write_text(json.dumps({r["ticker"]: mapping(r) for r in rows[:3]}))
    manifest = manifest.model_copy(update={"identity_mappings_sha256": file_hash(maps)})
    clock.seconds = 179
    calls = []

    def counted(*args):
        calls.append(args)
        return transport(*args)

    report = run_population_enrichment(
        tmp_path / "deadline",
        manifest,
        inventory / "private/inventory.json",
        maps,
        now=clock.now,
        clock=clock.clock,
        transport=counted,
        sleep=clock.sleep,
    )
    assert report["budget"]["stop_reason"] == "DEADLINE"
    assert report["coverage"]["unknown"] == 3
    assert report["coverage"]["potentially_supported"] == 3
    assert len(calls) == 1 and clock.seconds == 179
    assert report["budget"]["http_attempts"]["yfinance"] == 4


def test_corrupted_retained_reference_is_rejected_before_any_population_io(collected, tmp_path):
    clock, rows, manifest, inventory, _ = collected
    maps = tmp_path / "mappings.json"
    maps.write_text(json.dumps({r["ticker"]: mapping(r) for r in rows[:3]}))
    manifest = manifest.model_copy(update={"identity_mappings_sha256": file_hash(maps)})
    source = inventory / "private/reference.json"
    source.chmod(0o600)
    source.write_text("tampered")
    with pytest.raises(ValueError, match="reference source"):
        run_population_enrichment(
            tmp_path / "corrupt",
            manifest,
            inventory / "private/inventory.json",
            maps,
            now=clock.now,
            clock=clock.clock,
            transport=lambda *a: pytest.fail("No requests with corrupt source"),
        )


def test_conflicting_actual_profile_venue_blocks_classification_and_identity(collected, tmp_path):
    clock, rows, manifest, inventory, transport = collected
    maps = tmp_path / "mappings.json"
    maps.write_text(json.dumps({r["ticker"]: mapping(r) for r in rows[:3]}))
    manifest = manifest.model_copy(update={"identity_mappings_sha256": file_hash(maps)})

    def mismatch(host, method, path, params, body, timeout):
        result = transport(host, method, path, params, body, timeout)
        if path.endswith("AAA"):
            data = json.loads(result.body)
            data["quoteSummary"]["result"][0]["quoteType"]["exchange"] = "NYQ"
            return response(data)
        return result

    report = run_population_enrichment(
        tmp_path / "conflict",
        manifest,
        inventory / "private/inventory.json",
        maps,
        now=clock.now,
        clock=clock.clock,
        transport=mismatch,
        sleep=clock.sleep,
    )
    assert report["coverage"]["conflicting"] == 1
    assert report["coverage"]["identity_conflicts"] == 1
    assert report["coverage"]["resolved"] == 2


def test_native_http_transport_keeps_cookies_only_in_memory_and_never_follows_redirect(monkeypatch):
    import stock_analyser.qualification_yahoo as yahoo

    calls = []
    replies = [
        (
            404,
            (("Set-Cookie", "test-cookie=private-value; Domain=.yahoo.com; Path=/; Secure"),),
            b"",
        ),
        (302, (("Location", "https://consent.yahoo.com/"),), b"not followed"),
    ]

    class Connection:
        def __init__(self, host, remaining):
            self.host = host

        def request(self, method, target, body, headers):
            calls.append((self.host, headers))

        def getresponse(self):
            status, headers, body = replies.pop(0)

            class Reply:
                def getheaders(self):
                    return headers

                def read1(self, n):
                    nonlocal body
                    result, body = body, b""
                    return result

            result = Reply()
            result.status = status
            return result

        def close(self):
            pass

    monkeypatch.setattr(yahoo, "_DeadlineHTTPSConnection", Connection)
    transport = yahoo.YahooHTTPTransport()
    assert transport("fc.yahoo.com", "GET", "/", {}, None, 5).status == 404
    assert (
        transport("query1.finance.yahoo.com", "GET", "/v1/test/getcrumb", {}, None, 5).status == 302
    )
    assert len(calls) == 2
    assert any(k.lower() == "cookie" and "private-value" in v for k, v in calls[1][1].items())
    transport.close()
    assert not list(transport.cookies)
    with pytest.raises(QualificationStop):
        transport("query1.finance.yahoo.com", "GET", "/v1/test/getcrumb", {}, None, 5)
    assert len(calls) == 2


def test_authentication_refresh_is_counted_and_operation_cap_still_holds():
    client, budget, _, calls = make_client(
        [
            response(""),
            response("old-crumb"),
            response({}, 401),
            response(""),
            response("new-crumb"),
            response(profile("AAA")),
        ]
    )
    assert client.profile("AAA")["symbol"] == "AAA"
    assert len(calls) == budget.counts["yfinance"] == 6
    assert budget.retries == 1
    assert calls[-1][3]["crumb"] == "new-crumb"


def test_reentrant_yahoo_operation_is_rejected_without_a_dispatch():
    client, budget, _, calls = make_client([])
    client.operation_lock.acquire()
    try:
        with pytest.raises(QualificationStop, match="CONCURRENCY"):
            client.screen(0, 250)
    finally:
        client.operation_lock.release()
    assert not calls and budget.counts["yfinance"] == 0


def test_unknown_profile_metadata_remains_unknown_instead_of_measured_conflict(collected, tmp_path):
    clock, rows, manifest, inventory, transport = collected
    maps = tmp_path / "mappings.json"
    maps.write_text(json.dumps({r["ticker"]: mapping(r) for r in rows[:3]}))
    manifest = manifest.model_copy(update={"identity_mappings_sha256": file_hash(maps)})

    def incomplete(host, method, path, params, body, timeout):
        result = transport(host, method, path, params, body, timeout)
        if path.endswith("AAA"):
            data = json.loads(result.body)
            data["quoteSummary"]["result"][0]["price"].pop("currency")
            return response(data)
        return result

    report = run_population_enrichment(
        tmp_path / "unknown",
        manifest,
        inventory / "private/inventory.json",
        maps,
        now=clock.now,
        clock=clock.clock,
        transport=incomplete,
        sleep=clock.sleep,
    )
    assert report["coverage"]["unknown"] == report["coverage"]["unmatched"] == 1
    assert report["coverage"]["conflicting"] == 0


def test_mandatory_weekly_window_adds_an_independent_history_and_benchmark_cost(monkeypatch):
    import stock_analyser.qualification_plan as planner
    from stock_analyser.contracts import HistoryRequirement
    from stock_analyser.universe import SelectionPolicy

    policy = SelectionPolicy(
        mandatory_history=(HistoryRequirement(timeframe="weekly", minimum_bars=200),)
    )
    monkeypatch.setattr(
        planner, "CRITERIA", planner.CRITERIA.model_copy(update={"selection_policy": policy})
    )
    plan = planner.calculate_population_plan(inventories(), controls())
    assert plan.history_windows_per_candidate == 2
    assert plan.cold["mandatory_candidate_history"] == 8
    assert plan.cold["mandatory_benchmark_history"] == 2
    assert plan.warm_metadata_fresh["mandatory_candidate_history"] == 8


def test_population_preflight_rejects_conflicting_provider_account_scopes_before_io():
    rights = grants(datetime.now(UTC))
    rights = (
        rights[0].model_copy(update={"account_scope_sha256": digest("different-account")}),
        *rights[1:],
    )
    assert "RIGHTS_ACCOUNT_SCOPE_CONFLICT" in preflight(
        WorkloadManifest(rights=rights), now=datetime.now(UTC)
    )


def test_profile_endpoint_deadline_stops_before_dispatching_a_third_http_request():
    clock = Clock()
    budget = RequestBudget({"yfinance": 10}, 10, 2, clock=clock.clock)
    calls = []

    def transport(host, method, path, params, body, timeout):
        calls.append(timeout)
        clock.sleep(min(timeout, 0.6))
        return response("crumb" if path.endswith("getcrumb") else "")

    client = YahooQualificationClient(budget, transport=transport, sleep=clock.sleep)
    with pytest.raises(QualificationStop, match="DEADLINE"):
        client.profile("AAA")
    assert len(calls) == 2
    assert clock.seconds <= 2


def test_native_inventory_can_stop_at_page_cap_and_does_not_emit_population_plan(tmp_path):
    clock = Clock()
    manifest = WorkloadManifest(
        max_pages_per_source=1,
        controls=controls(),
        rights=grants(clock.now()),
        request_ceiling=20,
        per_source_ceilings={"massive": 10, "yfinance": 10},
    )
    budget = RequestBudget(manifest.per_source_ceilings, 20, 180, clock=clock.clock)

    def transport(host, method, path, params, body, timeout):
        if host == "fc.yahoo.com":
            return response("")
        if path.endswith("getcrumb"):
            return response("crumb")
        return response(screen(["AAA"], total=2))

    yahoo = yahoo_client(budget, manifest.controls, transport=transport, sleep=clock.sleep)
    with ProviderAllowance(
        tmp_path / "lease", 60, clock=clock.clock, sleep=clock.sleep
    ) as allowance:
        client = MassiveClient(
            SecretStr("fake"),
            allowance,
            manifest.controls.massive,
            lambda *a: HttpResponse(200, {}, json.dumps({"results": [ref("AAA")]}).encode()),
            budget=budget,
        )
        report = run_inventory(
            client, tmp_path / "partial", manifest, budget, now=clock.now, yahoo=yahoo
        )
    assert report["inventories"][1]["status"] == "TRUNCATED"
    assert report["inventories"][1]["count_kind"] == "lower_bound"
    assert not (tmp_path / "partial/population-workload.json").exists()
    assert not report["gate"]["full_sector_accepted"]
    yahoo.close()


def test_mapping_effective_dates_use_the_us_market_day():
    from stock_analyser.qualification_workflow import reconcile_identity

    row = ref("AAA")
    item = mapping(row)
    item["effective_from"] = date(2026, 10, 27)
    stamp = datetime(2026, 10, 27, 2, tzinfo=UTC)  # October 26 in New York
    assert reconcile_identity(row, item, as_of=stamp)[0] == "conflicting"
    item["effective_from"] = date(2020, 1, 1)
    item["effective_to"] = date(2026, 10, 27)
    assert reconcile_identity(row, item, as_of=stamp)[0] == "resolved"


def test_two_share_classes_cannot_silently_share_one_provider_symbol():
    from stock_analyser.qualification_workflow import classify_population

    stamp = datetime.now(UTC)
    rows = [ref("AAA"), ref("BBB")]
    maps = {r["ticker"]: {**mapping(r, "AAA"), "effective_from": date(2020, 1, 1)} for r in rows}
    profiles = {
        r["ticker"]: {"symbol": "AAA", "sector": "Technology", "retrieved_at": stamp} for r in rows
    }
    result = classify_population(rows, maps, profiles, now=stamp)
    assert result.identity_conflicts == result.conflicting == 2
    assert result.resolved == result.technology == 0


def test_complete_classification_refines_history_bound_without_changing_metadata_scope(
    collected, tmp_path
):
    clock, rows, manifest, inventory, transport = collected
    maps = tmp_path / "mappings.json"
    maps.write_text(json.dumps({r["ticker"]: mapping(r) for r in rows[:3]}))
    manifest = manifest.model_copy(
        update={
            "identity_mappings_sha256": file_hash(maps),
            "request_ceiling": 20,
            "per_source_ceilings": {"massive": 5, "yfinance": 15},
        }
    )
    assert preflight(manifest, now=clock.now(), stage="enrich") == []
    assert preflight(manifest, now=clock.now())
    output = tmp_path / "refined"
    run_population_enrichment(
        output,
        manifest,
        inventory / "private/inventory.json",
        maps,
        now=clock.now,
        clock=clock.clock,
        transport=transport,
        sleep=clock.sleep,
    )
    refined = WorkloadManifest.model_validate_json(
        (output / "enrichment-workload.json").read_text()
    )
    assert refined.population_plan.candidate_upper_bound == 4
    assert refined.population_plan.history_candidate_upper_bound == 2
    assert refined.population_plan.total_http_upper == 22
    assert refined.request_ceiling == 20  # Never raise a reviewed ceiling automatically.
    assert "CALCULATED_WORKLOAD_EXCEEDS_REVIEWED_LIMITS" in preflight(refined, now=clock.now())


def test_incomplete_classification_cannot_reduce_history_requests():
    from stock_analyser.qualification_workflow import empty_coverage

    partial = empty_coverage(datetime.now(UTC)).model_copy(
        update={"reference_rows": 4, "potentially_supported": 4, "unknown": 4, "unmatched": 4}
    )
    with pytest.raises(ValueError, match="Incomplete classification"):
        calculate_population_plan(inventories(), controls(), partial)


def test_authentication_attempts_have_a_session_wide_bound():
    client, budget, _, calls = make_client(
        [response(""), response("bad", 500), response(""), response("bad", 500)]
    )
    for _ in range(2):
        with pytest.raises(QualificationStop):
            client.profile("AAA")
    with pytest.raises(QualificationStop, match="AUTHENTICATION_LIMIT"):
        client.profile("AAA")
    assert len(calls) == budget.counts["yfinance"] == 4
