"""Qualification grants acceptance on sufficient evidence and rejects independent mutations."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta

import pytest
from typer.testing import CliRunner

from stock_analyser.cli import app
from stock_analyser.qualification import (
    BudgetEvidence,
    CoverageEvidence,
    HistoryEvidence,
    InventoryEvidence,
    PageEvidence,
    evaluate_qualification,
    requirements_hash,
)
from stock_analyser.qualification_budget import QualificationStop, RequestBudget
from stock_analyser.qualification_workflow import (
    CRITERIA,
    WorkloadManifest,
    classify_population,
    measure_selection,
    observe_blocked,
    population_manifest,
    preflight,
    reconcile_identity,
    report_for,
    unknown_rights,
)
from stock_analyser.universe_spike import Enumeration, digest

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)


@pytest.fixture
def qualifying():
    rights = tuple(
        r.model_copy(
            update={
                "disposition": "permitted",
                "conditions": (),
                "retention_days": 365,
                "document_sha256": digest("synthetic-rights"),
            }
        )
        for r in unknown_rights(NOW)
    )
    manifest = WorkloadManifest(rights=rights)
    inventories = tuple(
        InventoryEvidence(
            source=source,
            query=query,
            status="COMPLETE",
            reason="COMPLETE",
            method=method,
            reported_total=10 if source == "yfinance" else None,
            raw_rows=10,
            unique_rows=10,
            duplicates=0,
            errors=0,
            count_kind="exact",
            pages=(PageEvidence(rows=10, sha256=digest(source), retrieved_at=NOW, terminal=True),),
        )
        for source, query, method in (
            ("massive", "active_us_stocks", "terminal_page"),
            ("yfinance", "us_technology_discovery", "reported_total"),
        )
    )
    coverage = CoverageEvidence(
        reference_rows=10,
        excluded_listings=0,
        potentially_supported=10,
        technology=10,
        non_technology=0,
        unknown=0,
        conflicting=0,
        as_of=NOW,
        taxonomy="yahoo-sector-map-1",
        method="all_reference_profiles",
        resolved=10,
        ambiguous=0,
        identity_conflicts=0,
        unmatched=0,
        stable_id_matches=10,
        corroborated_mapping_matches=0,
        distinct_technology_securities=10,
        duplicate_technology_listings=0,
    )
    history = HistoryEvidence(
        candidates=10,
        qualified=10,
        measured_excluded=0,
        failed=0,
        not_attempted=0,
        selected=10,
        requirements_sha256=requirements_hash(CRITERIA.selection_policy),
        selection_implementation="sector-selection-2",
        adjustment="raw",
        sessions=60,
        exclusion_reasons={},
        blocker_reasons={},
        selection_inputs_sha256=digest("local-inputs"),
        benchmark_measured=True,
        calendars_and_feeds_verified=True,
    )
    budget = BudgetEvidence(
        request_ceiling=100,
        per_source_ceilings={"massive": 50, "yfinance": 50},
        http_attempts={"massive": 12, "yfinance": 12},
        sdk_calls=10,
        retries=0,
        cooldown_seconds=0,
        elapsed_seconds=0,
        wall_seconds=180,
        concurrency=1,
        conservative_http_upper_bound=50,
        inventory_complete_for_estimate=True,
        cold_measured=True,
        warm_measured=True,
        stop_reason="COMPLETE",
    )
    return report_for(
        manifest,
        started=NOW,
        finished=NOW,
        origin="synthetic",
        inventories=inventories,
        coverage=coverage,
        history=history,
        budget=budget,
    ).model_dump(mode="json")


def test_complete_synthetic_can_pass_but_never_a_live_release(qualifying):
    result = evaluate_qualification(qualifying, now=NOW)
    assert result["full_sector_accepted"] and result["blockers"] == []
    assert all(g["status"] == "PASS" for g in result["gates"].values())
    assert not evaluate_qualification(qualifying, now=NOW, live=True)["full_sector_accepted"]
    qualifying["gate"] = result
    assert evaluate_qualification(qualifying, now=NOW) == result


@pytest.mark.parametrize(
    "path,value",
    [
        (("schema_version",), "99"),
        (("criteria",), None),
        (("criteria", "metadata_ttl_hours"), 48),
        (("criteria_sha256",), "a" * 64),
        (("policy_sha256",), "a" * 64),
        (("selection_policy_sha256",), "a" * 64),
        (("inventories", 0, "status"), "TRUNCATED"),
        (("inventories", 0, "duplicates"), 1),
        (("inventories", 0, "raw_rows"), 11),
        (("inventories", 0, "pages", 0, "terminal"), False),
        (("inventories", 1, "reported_total"), 11),
        (("coverage", "reference_rows"), 9),
        (("coverage", "technology"), 9),
        (("coverage", "unknown"), 1),
        (("coverage", "conflicting"), 1),
        (("coverage", "unmatched"), 1),
        (("coverage", "ambiguous"), 1),
        (("coverage", "identity_conflicts"), 1),
        (("coverage", "stable_id_matches"), 9),
        (("history", "requirements_sha256"), "a" * 64),
        (("history", "qualified"), 9),
        (("history", "failed"), 1),
        (("history", "not_attempted"), 1),
        (("history", "selected"), 9),
        (("history", "benchmark_measured"), False),
        (("history", "calendars_and_feeds_verified"), False),
        (("rights", 0, "disposition"), "unknown"),
        (("rights", 0, "disposition"), "prohibited"),
        (("rights", 0, "conditions"), ["UNRESOLVED"]),
        (("rights", 0, "intended_scope_sha256"), "a" * 64),
        (("rights", 0, "retention_days"), 1),
        (("rights", 0, "reviewed_at"), "2026-01-01T00:00:00Z"),
        (("rights", 0, "valid_until"), "2026-10-26T00:00:00Z"),
        (("budget", "http_attempts", "yfinance"), None),
        (("budget", "http_attempts", "massive"), 51),
        (("budget", "elapsed_seconds"), 181),
        (("budget", "conservative_http_upper_bound"), None),
        (("budget", "cold_measured"), False),
        (("budget", "warm_measured"), False),
        (("budget", "stop_reason"), "REQUEST_LIMIT"),
        (("started_at",), "2026-10-25T00:00:00Z"),
        (("coverage", "as_of"), "2026-10-28T00:00:00Z"),
        (("gate",), {"status": "PASS", "full_sector_accepted": True, "blockers": []}),
    ],
)
def test_mutation_of_every_mandatory_gate_fails(qualifying, path, value):
    parent = qualifying
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value
    assert not evaluate_qualification(qualifying, now=NOW)["full_sector_accepted"]


def test_expiration_recomputed_and_historical_summary_rejected(qualifying):
    assert not evaluate_qualification(qualifying, now=NOW + timedelta(hours=25))[
        "full_sector_accepted"
    ]
    assert not evaluate_qualification({"schema_version": "1", "gate": {"status": "PASS"}}, now=NOW)[
        "full_sector_accepted"
    ]


def test_budget_shared_across_workers_and_deadline_prevents_dispatch():
    time = [0.0]
    budget = RequestBudget({"massive": 3, "yfinance": 2}, 4, 10, clock=lambda: time[0])

    def dispatch(source):
        try:
            budget.dispatch(source)
            return 1
        except QualificationStop:
            return 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(dispatch, ["massive", "yfinance"] * 20)) == 4
    time[0] = 11
    with pytest.raises(QualificationStop, match="DEADLINE"):
        budget.dispatch("massive")
    assert sum(budget.counts.values()) == 4


def row(ticker="AAA", **updates):
    return {
        "ticker": ticker,
        "active": True,
        "market": "stocks",
        "locale": "us",
        "type": "CS",
        "primary_exchange": "XNAS",
        "currency_name": "USD",
        "share_class_figi": "share-" + ticker,
        "composite_figi": "listing-" + ticker,
        **updates,
    }


def mapping(r, **updates):
    return {
        "share_class_figi": r["share_class_figi"],
        "composite_figi": r["composite_figi"],
        "venue": r["primary_exchange"],
        "currency": "USD",
        "type": "CS",
        "effective_from": date(2020, 1, 1),
        "provider_symbol": r["ticker"],
        **updates,
    }


def test_full_reference_classification_includes_discovery_absences_and_unknown_metadata():
    rows = [row(), row("NOT_IN_DISCOVERY"), row("UNKNOWN_ID", share_class_figi=None)]
    maps = {r["ticker"]: mapping(r) for r in rows[:2]}
    profiles = {
        r["ticker"]: {"symbol": r["ticker"], "sector": "Technology", "retrieved_at": NOW}
        for r in rows[:2]
    }
    result = classify_population(rows, maps, profiles, now=NOW)
    assert result.technology == 2 and result.unknown == result.unmatched == 1
    assert result.potentially_supported == 3 and result.excluded_listings == 0
    profiles["AAA"]["retrieved_at"] = NOW - timedelta(hours=25)
    assert classify_population(rows, maps, profiles, now=NOW).unknown == 2
    profiles["AAA"]["retrieved_at"] = NOW + timedelta(seconds=1)
    assert classify_population(rows, maps, profiles, now=NOW).unknown == 2


@pytest.mark.parametrize(
    "updates,outcome",
    [
        ({"share_class_figi": "wrong"}, "conflicting"),
        ({"venue": "XNYS"}, "conflicting"),
        ({"currency": "EUR"}, "conflicting"),
        ({"effective_from": date(2027, 1, 1)}, "conflicting"),
        ({"effective_to": date(2026, 10, 27)}, "conflicting"),
        ({"share_class_figi": None, "composite_figi": None}, "unmatched"),
        ({"provider_symbol": "A-A"}, "resolved"),
    ],
)
def test_stable_ids_and_dated_mapping(updates, outcome):
    assert reconcile_identity(row(), mapping(row(), **updates), as_of=NOW)[0] == outcome


def test_corroborated_fallback_needs_evidence_and_does_not_infer_punctuation():
    r = row()
    m = mapping(r, share_class_figi=None, composite_figi=None, provider_symbol="A.A")
    assert reconcile_identity(r, m, as_of=NOW)[0] == "unmatched"
    m.update(method="corroborated_mapping", source_sha256=digest("mapping"), reviewed_at=NOW)
    assert reconcile_identity(r, m, as_of=NOW) == ("resolved", "corroborated_mapping")
    assert reconcile_identity(r, [m, m], as_of=NOW)[0] == "ambiguous"


def test_incomplete_inventories_cannot_bound_population():
    with pytest.raises(ValueError, match="Complete inventories"):
        population_manifest(
            Enumeration("massive", {}), Enumeration("yfinance", {}), WorkloadManifest()
        )


def test_preflight_and_cli_never_contact_providers_and_refuse_overwrite(tmp_path, monkeypatch):
    import yfinance

    import stock_analyser.massive as massive

    def forbidden(*a, **k):
        pytest.fail("Preflight must not contact providers")

    monkeypatch.setattr(massive, "live_client", forbidden)
    monkeypatch.setattr(yfinance, "Ticker", forbidden)
    monkeypatch.setattr(yfinance, "screen", forbidden)
    output = tmp_path / "new"
    result = CliRunner().invoke(app, ["qualify-universe", "--output", str(output)])
    assert result.exit_code == 1 and "Full-sector acceptance: FAIL" in result.output
    saved = (output / "findings.json").read_bytes()
    assert b'"origin": "preflight"' in saved
    assert b'"massive": 0' in saved
    result = CliRunner().invoke(app, ["qualify-universe", "--output", str(output)])
    assert result.exit_code == 1 and "cannot be overwritten" in result.output
    assert (output / "findings.json").read_bytes() == saved
    assert preflight(WorkloadManifest(), now=NOW) == ["RIGHTS_EVIDENCE_MISSING"]
    with pytest.raises(ValueError):
        observe_blocked(output, WorkloadManifest(), now=lambda: NOW)


def test_measurement_reuses_existing_exact_history_and_distinguishes_retrieval(tmp_path):
    from stock_analyser.universe import select_universe
    from stock_analyser.universe_demo import fixture_request

    request, resolver = fixture_request(tmp_path / "local")
    result = measure_selection(select_universe(request, resolver))
    assert result.qualified == 12
    assert result.measured_excluded == 2
    assert result.failed == 1
    assert result.exclusion_reasons == {"BELOW_MINIMUM_PRICE": 1, "BELOW_MINIMUM_LIQUIDITY": 2}
    assert result.blocker_reasons == {"INVALID_MANDATORY_HISTORY": 1}


def test_reviewed_population_export_measures_all_candidates_without_raw_report_leakage(
    qualifying, tmp_path
):
    import json

    from stock_analyser.qualification_workflow import execute_population
    from stock_analyser.universe import select_universe
    from stock_analyser.universe_demo import fixture_request
    from stock_analyser.universe_spike import REFERENCE_FIELDS, project

    request, resolver = fixture_request(tmp_path / "permitted-local")
    candidates = tuple(
        c for c in request.population.candidates if c.reference.ticker.startswith("TECH")
    )
    request = request.model_copy(
        update={"population": request.population.model_copy(update={"candidates": candidates})}
    )
    snapshot = select_universe(request, resolver)
    stamp = request.requested_as_of
    rows = [c.reference.model_dump() for c in candidates]
    inventories = []
    for i in qualifying["inventories"]:
        i.update(raw_rows=12, unique_rows=12)
        i["pages"][0].update(rows=12, retrieved_at=stamp.isoformat())
        if i["source"] == "massive":
            i["pages"][0]["sha256"] = digest(project(rows, REFERENCE_FIELDS, "ticker"))
        else:
            i["reported_total"] = 12
        inventories.append(i)
    inputs = {
        "snapshot": snapshot.model_dump(mode="json"),
        "inventories": inventories,
        "mappings": {r["ticker"]: mapping(r) for r in rows},
        "profiles": {
            c.reference.ticker: {
                "symbol": c.classification.source.provider_symbol,
                "sector": "Technology",
                "retrieved_at": stamp.isoformat(),
                "secret": "never-serialize-raw-payload",
            }
            for c in candidates
        },
        "budget": qualifying["budget"],
        "origin": "synthetic",
        "started_at": stamp.isoformat(),
        "finished_at": stamp.isoformat(),
        "benchmark": None,
    }
    path = tmp_path / "local-only.json"
    path.write_text(json.dumps(inputs, default=str))
    from stock_analyser.qualification import RightEvidence, file_hash

    rights = tuple(
        RightEvidence.model_validate(
            {
                **r,
                "reviewed_at": stamp.isoformat(),
                "valid_until": (stamp + timedelta(days=1)).isoformat(),
            }
        )
        for r in qualifying["rights"]
    )
    manifest = WorkloadManifest(
        stage="population",
        request_ceiling=100,
        per_source_ceilings={"massive": 50, "yfinance": 50},
        estimated_http_upper_bound=50,
        reference_count=12,
        inventory_sha256=digest(
            [InventoryEvidence.model_validate(i).model_dump(mode="json") for i in inventories]
        ),
        population_inputs_sha256=file_hash(path),
        rights=rights,
    )
    report = execute_population(tmp_path / "aggregate", manifest, path, now=lambda: stamp)
    assert report["history"]["qualified"] == 12
    assert report["coverage"]["technology"] == report["coverage"]["resolved"] == 12
    assert report["gate"]["blockers"] == ["HISTORY_LIQUIDITY_OR_SELECTION_INCOMPLETE"]
    assert "never-serialize" not in (tmp_path / "aggregate/findings.json").read_text()
    with pytest.raises(ValueError, match="hash"):
        execute_population(
            tmp_path / "tampered",
            manifest.model_copy(update={"population_inputs_sha256": "a" * 64}),
            path,
            now=lambda: stamp,
        )


def test_massive_shared_budget_covers_retries_and_clamps_deadline(tmp_path):
    import json

    from pydantic import SecretStr

    from stock_analyser.massive import (
        HttpResponse,
        MassiveClient,
        MassiveSettings,
        ProviderAllowance,
    )
    from stock_analyser.universe_spike import enumerate_massive

    ticks = [0.0]

    def sleep(seconds):
        ticks[0] += seconds

    budget = RequestBudget({"massive": 2, "yfinance": 0}, 2, 30, clock=lambda: ticks[0])
    timeouts = []

    def transport(target, key, timeout):
        timeouts.append(timeout)
        if len(timeouts) == 1:
            return HttpResponse(429, {"Retry-After": "1"}, b"secret upstream error")
        return HttpResponse(200, {}, json.dumps({"results": [row()]}).encode())

    with ProviderAllowance(
        tmp_path / "lease", 60, clock=lambda: ticks[0], sleep=sleep
    ) as allowance:
        client = MassiveClient(
            SecretStr("secret"),
            allowance,
            MassiveSettings(timeout_seconds=120, retries=2),
            transport,
            budget=budget,
        )
        result = enumerate_massive(client)
    assert result.status == "COMPLETE" and budget.counts["massive"] == 2
    assert budget.retries == 1 and budget.cooldown_seconds == 1
    assert timeouts == [29, 28]
    assert "secret" not in json.dumps(result.evidence())


def test_deadline_and_request_exhaustion_preserve_partial_inventory(tmp_path):
    import json

    from pydantic import SecretStr

    from stock_analyser.massive import (
        HttpResponse,
        MassiveClient,
        MassiveSettings,
        ProviderAllowance,
    )
    from stock_analyser.universe_spike import enumerate_massive

    ticks = [0.0]
    budget = RequestBudget({"massive": 1}, 1, 30, clock=lambda: ticks[0])
    calls = []

    def transport(target, key, timeout):
        calls.append(1)
        return HttpResponse(
            200,
            {},
            json.dumps(
                {
                    "results": [row()],
                    "next_url": "https://api.massive.com/v3/reference/tickers?cursor=private",
                }
            ).encode(),
        )

    with ProviderAllowance(
        tmp_path / "lease",
        60,
        clock=lambda: ticks[0],
        sleep=lambda n: ticks.__setitem__(0, ticks[0] + n),
    ) as allowance:
        client = MassiveClient(
            SecretStr("secret"), allowance, MassiveSettings(), transport, budget=budget
        )
        result = enumerate_massive(client)
    assert result.reason == "REQUEST_LIMIT" and len(result.rows) == len(calls) == 1
    assert result.evidence()["count_kind"] == "lower_bound"
    assert "private" not in json.dumps(result.evidence())
    ticks[0] = 40
    with pytest.raises(QualificationStop, match="DEADLINE"):
        budget.dispatch("massive")


def test_supported_interruption_preserves_partial_enumeration():
    from stock_analyser.universe_spike import enumerate_yahoo

    def screen(offset, size):
        if offset:
            raise KeyboardInterrupt
        return {"start": 0, "count": 1, "total": 2, "quotes": [{"symbol": "AAA"}]}

    result = enumerate_yahoo(screen, page_size=1)
    assert result.reason == "INTERRUPTED" and result.status == "TRUNCATED"
    assert len(result.rows) == 1
