"""Native history measurement is bounded and never turns fixture readiness into live PASS."""

import json
from contextlib import contextmanager
from datetime import datetime, timedelta

import pytest
from test_qualification_collection import controls, grants, inventories, mapping
from typer.testing import CliRunner

from stock_analyser.cli import app
from stock_analyser.collection import RawPriceResponse, encode
from stock_analyser.qualification import file_hash
from stock_analyser.qualification_history import HistoryCollectionInputs, collect_histories
from stock_analyser.qualification_workflow import (
    WorkloadManifest,
    budget_evidence,
    classify_population,
    empty_history,
    planned_manifest,
    report_for,
)
from stock_analyser.universe_demo import fixture_request
from stock_analyser.universe_spike import REFERENCE_FIELDS, digest, project


@pytest.fixture
def history_case(tmp_path):
    from stock_analyser.qualification_budget import RequestBudget

    request, resolver = fixture_request(tmp_path / "fixture", extra_symbols=("SPY",))
    stamp = request.requested_as_of
    ticks = [0.0]

    def now():
        return stamp + timedelta(seconds=ticks[0])

    candidates = [c for c in request.population.candidates if c.reference.ticker.startswith("TECH")]
    spy = next(c for c in request.population.candidates if c.reference.ticker == "SPY")
    spy = spy.model_copy(
        update={
            "reference": spy.reference.model_copy(update={"type": "ETF"}),
            "classification": None,
        }
    )
    candidates.append(spy)
    selection = request.model_copy(
        update={
            "histories": (),
            "population": request.population.model_copy(update={"candidates": tuple(candidates)}),
        }
    )
    benchmark = selection.model_copy(
        update={"population": selection.population.model_copy(update={"candidates": (spy,)})}
    )
    rows = [c.reference.model_dump() for c in candidates]
    maps = {r["ticker"]: mapping(r) for r in rows[:-1]}
    profiles = {
        c.reference.ticker: {
            "symbol": c.reference.ticker,
            "sector": "Technology",
            "retrieved_at": stamp.isoformat(),
        }
        for c in candidates[:-1]
    }
    coverage = classify_population(
        rows,
        {
            k: {**v, "effective_from": datetime.fromisoformat(v["effective_from"]).date()}
            for k, v in maps.items()
        },
        {k: {**v, "retrieved_at": stamp} for k, v in profiles.items()},
        now=stamp,
    )
    inv = tuple(
        i.model_copy(
            update={
                "pages": tuple(
                    p.model_copy(
                        update={
                            "retrieved_at": stamp,
                            "sha256": digest(project(rows, REFERENCE_FIELDS, "ticker"))
                            if i.source == "massive"
                            else p.sha256,
                        }
                    )
                    for p in i.pages
                )
            }
        )
        for i in inventories(len(rows))
    )
    manifest = planned_manifest(
        inv,
        WorkloadManifest(
            rights=grants(stamp),
            controls=controls(),
            request_ceiling=300,
            per_source_ceilings={"massive": 100, "yfinance": 100},
            wall_seconds=1000,
        ),
        classification=coverage,
    )
    prior = RequestBudget(manifest.per_source_ceilings, 300, 1000, clock=lambda: ticks[0])
    prior.counts.update({"massive": 1, "yfinance": 14})
    enrichment = report_for(
        manifest,
        started=stamp,
        finished=stamp,
        origin="synthetic",
        inventories=inv,
        coverage=coverage,
        history=empty_history(12),
        budget=budget_evidence(manifest, prior, "ENRICHMENT_COMPLETE"),
    )
    inputs = HistoryCollectionInputs(
        enrichment=enrichment,
        selection=selection,
        benchmark=benchmark,
        mappings=maps,
        profiles=profiles,
    )
    path = tmp_path / "history-inputs.json"
    path.write_text(inputs.model_dump_json())
    manifest = manifest.model_copy(update={"population_inputs_sha256": file_hash(path)})
    calls = []

    @contextmanager
    def factory(manifest, budget):
        class Provider:
            def fetch(self, instrument, market, plan):
                budget.dispatch("massive")
                calls.append((instrument.symbol, plan.requirement.timeframe))
                ticks[0] += 0.01
                return RawPriceResponse(
                    provider="fixture-us",
                    provider_symbol=instrument.symbol,
                    retrieved_at=now(),
                    feed=market.route("prices").feed,
                    adjustment=plan.requirement.adjustment,
                    payload=encode(
                        {
                            "bars": [
                                {
                                    "session": p.session_end.isoformat(),
                                    "timestamp": p.closes_at.isoformat(),
                                    "open": "10",
                                    "high": "10",
                                    "low": "10",
                                    "close": "10",
                                    "volume": "2000000",
                                }
                                for p in plan.periods
                            ]
                        }
                    ),
                )

        yield Provider()

    return manifest, path, inputs, resolver, ticks, now, factory, calls


def test_cold_and_warm_measurements_produce_replayable_export(history_case, tmp_path):
    from stock_analyser.qualification import evaluate_qualification
    from stock_analyser.qualification_workflow import WorkloadManifest, execute_population

    manifest, path, inputs, resolver, ticks, now, factory, calls = history_case
    output = tmp_path / "measured"
    report = collect_histories(
        output,
        manifest,
        path,
        now=now,
        clock=lambda: ticks[0],
        resolver=resolver,
        provider_factory=factory,
    )
    assert report["gate"]["full_sector_accepted"], report["gate"]
    assert len(calls) == 26 and calls.count(("SPY", "daily")) == 2
    assert report["budget"]["http_attempts"] == {"massive": 27, "yfinance": 14}
    assert report["budget"]["cold_measured"] and report["budget"]["warm_measured"]
    assert not evaluate_qualification(report, now=now(), live=True)["full_sector_accepted"]
    measurements = json.loads((output / "measurements.json").read_text())
    assert measurements["phases"]["cold"]["valid"] == 13
    assert measurements["phases"]["warm"]["http_attempts"]["massive"] == 13
    reviewed = WorkloadManifest.model_validate_json(
        (output / "execution-workload.json").read_text()
    )
    replay = execute_population(
        tmp_path / "replayed",
        reviewed,
        output / "private/population-export.json",
        now=now,
        resolver=resolver,
    )
    assert replay["gate"]["full_sector_accepted"], replay["gate"]
    aggregate = (output / "findings.json").read_text() + (output / "measurements.json").read_text()
    assert "TECH00" not in aggregate and "private/" not in aggregate
    with pytest.raises(ValueError, match="overwritten"):
        collect_histories(output, manifest, path, now=now)


@pytest.mark.parametrize("kind", ["short", "publication", "transport"])
def test_incomplete_history_keeps_acceptance_blocked(history_case, tmp_path, kind):
    manifest, path, inputs, resolver, ticks, now, factory, calls = history_case

    @contextmanager
    def broken(manifest, budget):
        with factory(manifest, budget) as provider:

            class Provider:
                def fetch(self, instrument, market, plan):
                    response = provider.fetch(instrument, market, plan)
                    if instrument.symbol != "TECH00":
                        return response
                    if kind == "transport":
                        raise ValueError("secret upstream failure must not escape")
                    if kind == "publication":
                        return response.model_copy(
                            update={"quality_flags": ("PUBLICATION_UNVERIFIED",)}
                        )
                    data = json.loads(response.payload)
                    data["bars"].pop()
                    return response.model_copy(update={"payload": encode(data)})

            yield Provider()

    output = tmp_path / "incomplete"
    report = collect_histories(
        output,
        manifest,
        path,
        now=now,
        clock=lambda: ticks[0],
        resolver=resolver,
        provider_factory=broken,
    )
    assert not report["gate"]["full_sector_accepted"]
    assert not report["budget"]["cold_measured"] and not report["budget"]["warm_measured"]
    assert report["history"]["failed"] == 1
    measurements = json.loads((output / "measurements.json").read_text())
    assert measurements["phases"]["cold"]["invalid" if kind != "transport" else "failed"] == 1
    assert "secret upstream" not in (output / "findings.json").read_text()


def test_deadline_stops_collection_and_preserves_partial_counts(history_case, tmp_path):
    manifest, path, inputs, resolver, ticks, now, factory, calls = history_case
    ticks[0] = 999.99
    report = collect_histories(
        tmp_path / "deadline",
        manifest,
        path,
        now=now,
        clock=lambda: ticks[0],
        resolver=resolver,
        provider_factory=factory,
    )
    assert not report["gate"]["full_sector_accepted"]
    assert report["budget"]["stop_reason"] == "DEADLINE"
    assert len(calls) <= 2
    assert not report["budget"]["warm_measured"]


def test_unknown_rights_and_cli_never_read_history_inputs_or_load_credentials(
    tmp_path, monkeypatch
):
    import stock_analyser.config as config

    monkeypatch.setattr(config, "load_massive_key", lambda: pytest.fail("No credential access"))
    manifest = WorkloadManifest()
    path = tmp_path / "manifest.json"
    path.write_text(manifest.model_dump_json())
    result = CliRunner().invoke(
        app,
        [
            "qualify-universe",
            "--mode",
            "measure",
            "--manifest",
            str(path),
            "--manifest-sha256",
            file_hash(path),
            "--population-inputs",
            str(tmp_path / "missing"),
            "--output",
            str(tmp_path / "blocked"),
        ],
    )
    assert result.exit_code == 1
    report = json.loads((tmp_path / "blocked/findings.json").read_text())
    assert report["budget"]["http_attempts"] == {"massive": 0, "yfinance": 0}


def test_input_tampering_and_live_fixture_cannot_dispatch(history_case, tmp_path):
    manifest, path, inputs, resolver, ticks, now, factory, calls = history_case
    with pytest.raises(ValueError, match="reviewed"):
        collect_histories(
            tmp_path / "bad-hash",
            manifest.model_copy(update={"population_inputs_sha256": "a" * 64}),
            path,
            now=now,
        )
    inputs = inputs.model_copy(
        update={"enrichment": inputs.enrichment.model_copy(update={"origin": "live"})}
    )
    path.write_text(inputs.model_dump_json())
    manifest = manifest.model_copy(update={"population_inputs_sha256": file_hash(path)})
    with pytest.raises(ValueError, match="maintained"):
        collect_histories(tmp_path / "live", manifest, path, now=now)
    assert not calls


def test_prepare_live_metadata_offline_without_inventing_publication_or_symbols(
    history_case, tmp_path
):
    from stock_analyser.contracts import SourceRecord, SymbolMapping
    from stock_analyser.qualification_history import prepare_history_inputs
    from stock_analyser.qualification_workflow import SourceBinding

    manifest, path, inputs, resolver, ticks, now, factory, calls = history_case
    directory = tmp_path / "enrichment"
    (directory / "private").mkdir(parents=True)
    stamp = now()
    rows = [c.reference.model_dump() for c in inputs.selection.population.candidates]
    rows[-1]["primary_exchange"] = "ARCX"
    raw = directory / "private/reference.json"
    raw.write_text(json.dumps(rows))
    source = inputs.selection.population.source.model_copy(
        update={
            "provider": "massive",
            "reference": str(raw),
            "sha256": file_hash(raw),
        }
    )
    account = manifest.rights[0].account_scope_sha256
    bindings = [
        SourceBinding(
            provider="massive",
            dataset="reference",
            source_sha256=source.sha256,
            account_scope_sha256=account,
        )
    ]
    sources, definitions = {}, {}
    for c in inputs.selection.population.candidates:
        instrument = c.instrument.model_copy(
            update={
                "venue": "ARCX" if c.reference.ticker == "SPY" else c.instrument.venue,
                "provider_symbols": tuple(
                    SymbolMapping(
                        provider=p,
                        symbol=c.reference.ticker,
                        effective_from=datetime(2020, 1, 1).date(),
                    )
                    for p in ("massive", "yfinance")
                ),
            }
        )
        definitions[c.reference.ticker] = instrument.model_dump(mode="json")
        if c.classification:
            s = SourceRecord.model_validate(c.classification.source.model_dump()).model_copy(
                update={"provider": "yfinance"}
            )
            sources[c.reference.ticker] = s.model_dump(mode="json")
            bindings.append(
                SourceBinding(
                    provider="yfinance",
                    dataset="classification",
                    source_sha256=s.sha256,
                    account_scope_sha256=account,
                )
            )
    inv = tuple(
        i.model_copy(
            update={
                "pages": tuple(
                    p.model_copy(
                        update={
                            "sha256": digest(project(rows, REFERENCE_FIELDS, "ticker"))
                            if i.source == "massive"
                            else p.sha256
                        }
                    )
                    for p in i.pages
                )
            }
        )
        for i in inputs.enrichment.inventories
    )
    report = inputs.enrichment.model_copy(update={"origin": "live", "inventories": inv})
    (directory / "findings.json").write_text(report.model_dump_json())
    detail = {
        "version": "qualified-enrichment-1",
        "reference": {"rows": rows, "pages": [{}]},
        "profiles": inputs.profiles,
        "mappings": inputs.mappings,
        "profile_sources": sources,
        "reference_source": source.model_dump(mode="json"),
    }
    (directory / "private/population.json").write_text(json.dumps(detail))
    definitions_path = tmp_path / "instruments.json"
    definitions_path.write_text(json.dumps(definitions))
    manifest = planned_manifest(inv, manifest, classification=report.coverage).model_copy(
        update={"source_bindings": tuple(bindings)}
    )
    prepared = prepare_history_inputs(
        directory, manifest, definitions_path, tmp_path / "prepared", as_of=stamp
    )
    restored = HistoryCollectionInputs.model_validate_json(prepared.read_text())
    assert not restored.selection.histories and not restored.benchmark.histories
    assert restored.selection.cutoff_policy.publication_timing == "unverified"
    assert restored.benchmark.population.candidates[0].instrument.venue == "ARCX"
    saved = WorkloadManifest.model_validate_json(
        (prepared.parent.parent / "history-workload.json").read_text()
    )
    assert saved.population_inputs_sha256 == file_hash(prepared)
    assert not calls
    definitions.pop("TECH00")
    definitions_path.write_text(json.dumps(definitions))
    with pytest.raises(ValueError, match="reviewed instrument"):
        prepare_history_inputs(
            directory, manifest, definitions_path, tmp_path / "missing-instrument", as_of=stamp
        )


def test_interruption_and_rights_expiry_keep_partial_evidence(history_case, tmp_path):
    manifest, path, inputs, resolver, ticks, now, factory, calls = history_case

    @contextmanager
    def interrupted(manifest, budget):
        with factory(manifest, budget) as provider:

            class Provider:
                def fetch(self, instrument, market, plan):
                    response = provider.fetch(instrument, market, plan)
                    if len(calls) == 2:
                        raise KeyboardInterrupt
                    return response

            yield Provider()

    output = tmp_path / "interrupted"
    report = collect_histories(
        output,
        manifest,
        path,
        now=now,
        clock=lambda: ticks[0],
        resolver=resolver,
        provider_factory=interrupted,
    )
    assert report["budget"]["stop_reason"] == "INTERRUPTED" and len(calls) == 2
    phases = json.loads((output / "measurements.json").read_text())["phases"]
    assert phases["cold"]["valid"] == phases["cold"]["failed"] == 1
    assert phases["cold"]["not_attempted"] == 11 and "warm" not in phases
    calls.clear()
    expires = now() + timedelta(seconds=0.015)
    manifest = manifest.model_copy(
        update={
            "rights": tuple(r.model_copy(update={"valid_until": expires}) for r in manifest.rights)
        }
    )
    report = collect_histories(
        tmp_path / "expired",
        manifest,
        path,
        now=now,
        clock=lambda: ticks[0],
        resolver=resolver,
        provider_factory=factory,
    )
    assert report["budget"]["stop_reason"] == "RIGHTS_EXPIRED" and len(calls) == 2
    assert not report["gate"]["full_sector_accepted"]
