"""Measure candidate/benchmark cold and warm history under one reviewed budget.

Inputs contain reviewed identities and metadata, never invented price snapshots.
Unknown publication timing remains a quality failure, including on successful HTTP.
"""

import json
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from stock_analyser.calendars import ExchangeCalendarAdapter, HistoryResolver
from stock_analyser.collection import (
    EvidencePacket,
    encode,
    immutable_write,
    validate_prices,
)
from stock_analyser.contracts import DataAvailability, SourceRecord
from stock_analyser.persistence.records import SnapshotReference
from stock_analyser.qualification import (
    CRITERIA,
    Evidence,
    QualificationReport,
    evaluate_qualification,
    file_hash,
    write_report,
)
from stock_analyser.qualification_budget import QualificationStop, RequestBudget
from stock_analyser.qualification_workflow import (
    SourceBinding,
    budget_evidence,
    classify_population,
    measure_benchmark,
    measure_selection,
    observe_blocked,
    potentially_supported,
    preflight,
    private_json,
    report_for,
    validate_population_sources,
    validate_qualification_windows,
)
from stock_analyser.universe import SelectionRequest, freeze_selection, resolve_selection
from stock_analyser.universe_policy import TECHNOLOGY_RULES_V1
from stock_analyser.universe_spike import REFERENCE_FIELDS, digest, project
from stock_analyser.version import source_version


class HistoryCollectionInputs(Evidence):
    version: Literal["qualification-history-inputs-1"] = "qualification-history-inputs-1"
    enrichment: QualificationReport
    selection: SelectionRequest
    benchmark: SelectionRequest
    mappings: dict[str, dict | list]
    profiles: dict[str, dict]


def prepare_history_inputs(enrichment_dir, manifest, instruments_path, output, *, as_of=None):
    """Offline bridge from retained enrichment to requests with reviewed local identities.

    Instruments carry independently reviewed Massive history symbol intervals;
    today's ticker or the Yahoo classification mapping cannot establish those dates.
    """
    from stock_analyser.calendars import CutoffPolicy
    from stock_analyser.contracts import Instrument
    from stock_analyser.markets import US_MARKET
    from stock_analyser.universe import (
        Classification,
        ReferenceListing,
        UniverseCandidate,
        UniversePopulation,
    )

    as_of = as_of or datetime.now(UTC)
    if output.exists():
        raise ValueError("Previous history inputs cannot be overwritten")
    if preflight(manifest, now=as_of):
        raise ValueError("Resolve rights and reviewed population bounds before reading details")
    report = QualificationReport.model_validate_json((enrichment_dir / "findings.json").read_text())
    gates = evaluate_qualification(report, now=as_of)["gates"]
    if report.origin != "live" or any(
        gates.get(k, {}).get("status") != "PASS"
        for k in ("contract", "freshness", "inventory", "classification", "identity")
    ):
        raise ValueError("Preparation requires complete live metadata evidence")
    detail = json.loads((enrichment_dir / "private/population.json").read_text())
    if detail.get("version") != "qualified-enrichment-1":
        raise ValueError("Unsupported enrichment detail")
    definitions = json.loads(instruments_path.read_text())
    if not isinstance(definitions, dict):
        raise ValueError("Reviewed instrument definitions must be keyed by reference ticker")
    instruments = {key: Instrument.model_validate(value) for key, value in definitions.items()}
    candidates = []
    for row in detail["reference"]["rows"]:
        listing = ReferenceListing.model_validate(row)
        profile = detail["profiles"].get(listing.ticker)
        classification = None
        if profile is not None:
            classification = Classification(
                source=SourceRecord.model_validate(detail["profile_sources"][listing.ticker]),
                original_sector=profile.get("sector"),
                original_industry=profile.get("industry"),
                status="OBSERVED",
            )
        instrument = instruments.get(listing.ticker)
        if potentially_supported(row) or listing.ticker == "SPY":
            if (
                instrument is None
                or instrument.symbol != listing.ticker
                or instrument.venue != listing.primary_exchange
            ):
                raise ValueError("Every supported listing and SPY needs a reviewed instrument")
            US_MARKET.validate_instrument(instrument)
        candidates.append(
            UniverseCandidate(
                reference=listing,
                instrument=instrument,
                identity_verified=instrument is not None,
                classification=classification,
            )
        )
    population = UniversePopulation(
        source=SourceRecord.model_validate(detail["reference_source"]),
        scope="Complete retained qualification reference population",
        enumeration_status="COMPLETE",
        enumeration_reason="TERMINAL_PAGE",
        reference_pages=len(detail["reference"]["pages"]),
        candidates=tuple(candidates),
    )
    # Native Massive currently reports publication as unverified. Preparation must
    # not manufacture an observed publication policy to obtain a successful gate.
    selection = SelectionRequest(
        market=US_MARKET,
        requested_as_of=as_of,
        population=population,
        policy=CRITERIA.selection_policy,
        cutoff_policy=CutoffPolicy(),
    )
    spy = tuple(c for c in candidates if c.reference.ticker == "SPY")
    if len(spy) != 1:
        raise ValueError("Exactly one verified SPY listing is required")
    benchmark = selection.model_copy(
        update={"population": population.model_copy(update={"candidates": spy})}
    )
    inputs = HistoryCollectionInputs(
        enrichment=report,
        selection=selection,
        benchmark=benchmark,
        mappings=detail["mappings"],
        profiles=detail["profiles"],
    )
    # Resolve and check metadata before publishing reviewable inputs; no HTTP.
    _metadata(
        inputs, manifest, as_of, HistoryResolver(ExchangeCalendarAdapter(), selection.cutoff_policy)
    )
    output.mkdir(parents=True, exist_ok=False)
    path, sha = private_json(output, "history-inputs.json", inputs.model_dump(mode="json"))
    updated = manifest.model_copy(update={"population_inputs_sha256": sha})
    immutable_write(output / "history-workload.json", encode(updated.model_dump(mode="json")))
    return path


@contextmanager
def native_provider(manifest, budget):
    from platformdirs import user_data_path

    from stock_analyser.config import load_massive_key
    from stock_analyser.massive import MassivePriceProvider, live_client

    with live_client(
        load_massive_key(),
        manifest.controls.massive,
        user_data_path("stock-analyser", appauthor=False) / "massive.lock",
        budget=budget,
        max_pages_per_operation=manifest.controls.massive_pages_per_operation,
    ) as client:
        yield MassivePriceProvider(client)


def _metadata(inputs, manifest, stamp, resolver):
    """Validate every supplied denominator/identity/source before provider construction."""
    from datetime import date

    from stock_analyser.markets import US_MARKET

    gates = evaluate_qualification(inputs.enrichment, now=stamp)["gates"]
    if any(
        gates.get(k, {}).get("status") != "PASS"
        for k in ("contract", "freshness", "inventory", "classification", "identity")
    ):
        raise ValueError("History measurement requires fresh complete qualified metadata")
    plan = manifest.population_plan
    if plan is None or plan.inventories != inputs.enrichment.inventories:
        raise ValueError("History plan differs from actual inventories")
    selection, benchmark = inputs.selection, inputs.benchmark
    for request in (selection, benchmark):
        if request.histories or request.policy != CRITERIA.selection_policy:
            raise ValueError("History inputs must use the supported policy without snapshots")
        if request.population.enumeration_status != "COMPLETE":
            raise ValueError("History population is incomplete")
        if inputs.enrichment.origin == "live" and (
            request.market != US_MARKET or request.cutoff_policy.publication_timing == "synthetic"
        ):
            raise ValueError("Live measurement requires maintained market/calendar provenance")
    if (
        benchmark.market != selection.market
        or benchmark.cutoff_policy != selection.cutoff_policy
        or benchmark.requested_as_of != selection.requested_as_of
        or len(benchmark.population.candidates) != 1
    ):
        raise ValueError("Benchmark and candidate requests must share one history policy/as-of")
    b = benchmark.population.candidates[0]
    if (
        b.instrument is None
        or not b.identity_verified
        or b.instrument.symbol != "SPY"
        or (inputs.enrichment.origin == "live" and b.instrument.venue != "ARCX")
        or b.reference.ticker != "SPY"
        or b.reference.primary_exchange != b.instrument.venue
    ):
        raise ValueError("An explicitly verified SPY benchmark is required")
    rows = [c.reference.model_dump() for c in selection.population.candidates]
    if not any(c.reference == b.reference for c in selection.population.candidates):
        raise ValueError("Benchmark must come from the same complete reference inventory")
    reference = next(i for i in plan.inventories if i.source == "massive")
    if len(rows) != reference.unique_rows or len(rows) != manifest.reference_count:
        raise ValueError("Candidate population differs from the full reference inventory")
    offset = 0
    for page in reference.pages:
        if (
            digest(project(rows[offset : offset + page.rows], REFERENCE_FIELDS, "ticker"))
            != page.sha256
        ):
            raise ValueError("Candidate inventory page hash mismatch")
        offset += page.rows
    if offset != len(rows):
        raise ValueError("Incomplete inventory pages")
    mappings = json.loads(json.dumps(inputs.mappings))
    profiles = json.loads(json.dumps(inputs.profiles))
    for mapping in mappings.values():
        if isinstance(mapping, dict):
            for key in ("effective_from", "effective_to"):
                if mapping.get(key):
                    mapping[key] = date.fromisoformat(mapping[key])
            if mapping.get("reviewed_at"):
                mapping["reviewed_at"] = datetime.fromisoformat(mapping["reviewed_at"])
    for profile in profiles.values():
        profile["retrieved_at"] = datetime.fromisoformat(profile["retrieved_at"])
    coverage = classify_population(rows, mappings, profiles, now=inputs.enrichment.coverage.as_of)
    if coverage != inputs.enrichment.coverage.model_copy(update={"profile_measurements": None}):
        raise ValueError("Actual classification differs from the enrichment evidence")
    if coverage.distinct_technology_securities > plan.history_candidate_upper_bound:
        raise ValueError("Actual history population exceeds the calculated plan")
    for c in selection.population.candidates:
        if not potentially_supported(c.reference.model_dump()):
            continue
        profile = profiles.get(c.reference.ticker)
        classification = c.classification
        if (
            profile is None
            or classification is None
            or c.instrument is None
            or not c.identity_verified
            or classification.status != "OBSERVED"
            or classification.original_sector != profile.get("sector")
            or classification.source.provider_symbol != profile.get("symbol")
            or classification.source.retrieved_at != profile["retrieved_at"]
            or c.instrument.symbol != c.reference.ticker
            or c.instrument.venue != c.reference.primary_exchange
        ):
            raise ValueError("Reviewed candidate identity/classification differs from enrichment")
        if (
            c.instrument.provider_symbol(
                classification.source.provider,
                profile["retrieved_at"].astimezone(ZoneInfo(selection.market.timezone)).date(),
            )
            != profile["symbol"]
        ):
            raise ValueError("Classification provider mapping differs from the reviewed identity")
    # Verify retained metadata bytes and account scope before any price dispatch.
    if inputs.enrichment.origin == "live":
        retained = json.loads(Path(selection.population.source.reference).read_text())
        if project(retained, REFERENCE_FIELDS, "ticker") != project(
            rows, REFERENCE_FIELDS, "ticker"
        ):
            raise ValueError("Retained reference bytes differ from the reviewed population")
        for candidate in selection.population.candidates:
            if candidate.classification:
                retained = json.loads(Path(candidate.classification.source.reference).read_text())
                if (
                    retained.get("sector") != candidate.classification.original_sector
                    or retained.get("symbol") != candidate.classification.source.provider_symbol
                ):
                    raise ValueError("Retained profile bytes differ from reviewed classification")
        bindings = {(b.provider, b.dataset, b.source_sha256): b for b in manifest.source_bindings}
        records = [(selection.population.source, "reference", "massive")]
        records += [
            (c.classification.source, "classification", "yfinance")
            for c in selection.population.candidates
            if c.classification
        ]
        for source, dataset, provider in records:
            binding = bindings.get((provider, dataset, source.sha256))
            if (
                source.provider != provider
                or file_hash(Path(source.reference)) != source.sha256
                or binding is None
            ):
                raise ValueError("Unverified retained metadata source")
            if any(
                r.account_scope_sha256 != binding.account_scope_sha256
                for r in manifest.rights
                if r.provider == provider and r.dataset == dataset
            ):
                raise ValueError("Metadata source account differs from reviewed rights")
    plans = resolve_selection(selection, resolver)
    benchmark_plans = resolve_selection(benchmark, resolver)
    return coverage, plans, benchmark_plans


def _targets(request):
    groups = {}
    for c in request.population.candidates:
        if (
            potentially_supported(c.reference.model_dump())
            and c.classification
            and c.classification.mapped_sector == "technology"
            and c.instrument
        ):
            groups.setdefault(c.reference.share_class_figi, []).append(c)
    return tuple(
        min(
            group,
            key=lambda c: (
                TECHNOLOGY_RULES_V1.listing_order.index(c.reference.primary_exchange),
                c.reference.composite_figi,
                c.reference.ticker,
            ),
        ).instrument
        for group in groups.values()
    )


def _retain(response, instrument, request, plan, directory):
    route = request.market.route("prices")
    if (
        response.provider != route.provider
        or response.feed != route.feed
        or any(
            instrument.provider_symbol(response.provider, p.session_end) != response.provider_symbol
            for p in plan.periods
        )
    ):
        raise ValueError("Price response identity/feed differs from the reviewed request")
    raw = directory / "source.json"
    sha = immutable_write(raw, response.payload)
    bars, findings, count = validate_prices(response, plan)
    from stock_analyser.contracts import EvidenceMetadata

    metadata = EvidenceMetadata(
        snapshot_id=uuid4(),
        instrument=instrument,
        market=request.market,
        currency=instrument.quote_currency,
        session_cutoff=plan.session_cutoff,
        feed=response.feed,
        timeframe=plan.requirement.timeframe,
        adjustment=plan.requirement.adjustment,
        availability=DataAvailability.PARTIAL
        if any(f.severity == "error" for f in findings)
        else DataAvailability.AVAILABLE,
        requested_bars=plan.requirement.required_bars,
        returned_bars=count,
        sources=(
            SourceRecord(
                provider=response.provider,
                provider_symbol=response.provider_symbol,
                retrieved_at=response.retrieved_at,
                reference=str(raw),
                sha256=sha,
                session_start=plan.history_start,
                session_end=plan.session_date,
            ),
        ),
        quality_flags=tuple(dict.fromkeys(f.code for f in findings)),
        reason="; ".join(dict.fromkeys(f.code for f in findings if f.severity == "error")) or None,
    )
    packet = EvidencePacket(
        metadata=metadata,
        resolution=plan,
        findings=findings,
        bars=bars,
        raw_reference=str(raw),
        response=response.model_dump(mode="json", exclude={"payload"}),
        source_version=source_version(),
        component_versions={"qualification_history": "1"},
    )
    path = directory / "manifest.json"
    return SnapshotReference(
        metadata=metadata,
        reference=str(path),
        sha256=immutable_write(path, encode(packet.model_dump(mode="json"))),
    )


def collect_histories(
    output,
    manifest,
    inputs_path,
    *,
    now=lambda: datetime.now(UTC),
    clock=time.monotonic,
    provider_factory=None,
    resolver=None,
):
    """Cold and metadata-fresh warm fetches, without cache reuse or guessed measurements."""
    output = output.resolve()
    if output.exists():
        raise ValueError("Previous findings cannot be overwritten")
    stamp = now()
    if preflight(manifest, now=stamp):
        return observe_blocked(output, manifest, mode="measure", now=now)
    if manifest.stage != "population" or manifest.population_inputs_sha256 != file_hash(
        inputs_path
    ):
        raise ValueError("History inputs differ from the reviewed population manifest")
    inputs = HistoryCollectionInputs.model_validate_json(inputs_path.read_text())
    origin = inputs.enrichment.origin
    if origin not in ("live", "synthetic"):
        raise ValueError("History inputs must identify actual or synthetic acquisition")
    if origin == "live":
        if resolver is not None or provider_factory is not None:
            raise ValueError("Live measurement requires the maintained native provider/calendar")
        resolver = HistoryResolver(ExchangeCalendarAdapter(), inputs.selection.cutoff_policy)
    elif resolver is None or provider_factory is None:
        raise ValueError("Synthetic measurements require explicit offline adapters")
    coverage, plans, benchmark_plans = _metadata(inputs, manifest, stamp, resolver)
    selection, benchmark = inputs.selection, inputs.benchmark
    # Prevent an old/future frozen request from spending the quota on the wrong window.
    initial = freeze_selection(selection, plans)
    initial_benchmark = freeze_selection(benchmark, benchmark_plans)
    validate_qualification_windows(
        initial, stamp, origin, resolver=resolver if origin == "synthetic" else None
    )
    validate_qualification_windows(
        initial_benchmark, stamp, origin, resolver=resolver if origin == "synthetic" else None
    )
    targets = _targets(selection)
    if len(targets) != coverage.distinct_technology_securities:
        raise ValueError("History target count differs from the qualified Technology population")
    spy = benchmark.population.candidates[0].instrument
    jobs = [(i, selection, p, "candidate") for i in targets for p in plans if p.venue == i.venue]
    jobs += [(spy, benchmark, p, "benchmark") for p in benchmark_plans]
    for instrument, request, plan, _ in jobs:
        request.market.validate_instrument(instrument)
        if not plan.periods or len(plan.periods) != plan.requirement.required_bars:
            raise ValueError("A complete required window cannot be resolved")
        for p in plan.periods:
            instrument.provider_symbol(request.market.route("prices").provider, p.session_start)
            instrument.provider_symbol(request.market.route("prices").provider, p.session_end)
    prior = inputs.enrichment.budget
    elapsed = (stamp - inputs.enrichment.started_at).total_seconds()
    if elapsed < 0 or any(v is None for v in prior.http_attempts.values()):
        raise ValueError("Incomplete prior acquisition accounting")
    budget = RequestBudget(
        manifest.per_source_ceilings, manifest.request_ceiling, manifest.wall_seconds, clock=clock
    )
    if (
        set(prior.http_attempts) != set(budget.counts)
        or any(v > budget.ceilings[p] for p, v in prior.http_attempts.items())
        or sum(prior.http_attempts.values()) > budget.overall
    ):
        raise ValueError("Prior acquisition exceeds reviewed limits")
    budget.counts.update(prior.http_attempts)
    budget.started -= elapsed
    budget.retries, budget.cooldown_seconds = prior.retries, prior.cooldown_seconds
    output.mkdir(parents=True, exist_ok=False)
    (output / "private").mkdir(mode=0o700)
    phases, snapshots, bindings = {}, {}, list(manifest.source_bindings)
    stop = "COMPLETE"
    factory = provider_factory or native_provider
    try:
        budget.remaining()
        with factory(manifest, budget) as provider:
            for phase in ("cold", "warm"):
                before, began = dict(budget.counts), clock()
                references, benchmark_refs = [], []
                phase_data = {
                    "attempted": 0,
                    "valid": 0,
                    "invalid": 0,
                    "failed": 0,
                    "required": len(jobs),
                    "http_attempts": {},
                    "elapsed_seconds": 0.0,
                }
                phases[phase] = phase_data
                try:
                    for number, (instrument, request, plan, kind) in enumerate(jobs):
                        budget.remaining()
                        if any(now() > r.valid_until for r in manifest.rights):
                            raise QualificationStop("RIGHTS_EXPIRED")
                        phase_data["attempted"] += 1
                        try:
                            response = provider.fetch(instrument, request.market, plan)
                            budget.remaining()
                            ref = _retain(
                                response,
                                instrument,
                                request,
                                plan,
                                output / "private" / phase / str(number),
                            )
                            budget.remaining()
                            (references if kind == "candidate" else benchmark_refs).append(ref)
                            phase_data[
                                "valid"
                                if ref.metadata.availability == DataAvailability.AVAILABLE
                                else "invalid"
                            ] += 1
                            for source in ref.metadata.sources:
                                if origin == "live":
                                    account = next(
                                        r.account_scope_sha256
                                        for r in manifest.rights
                                        if r.provider == source.provider and r.dataset == "prices"
                                    )
                                    bindings.append(
                                        SourceBinding(
                                            provider=source.provider,
                                            dataset="prices",
                                            source_sha256=source.sha256,
                                            account_scope_sha256=account,
                                        )
                                    )
                        except QualificationStop:
                            phase_data["failed"] += 1
                            raise
                        except KeyboardInterrupt:
                            phase_data["failed"] += 1
                            raise
                        except (ValueError, OSError):
                            phase_data["failed"] += 1
                finally:
                    phase_data["http_attempts"] = {p: budget.counts[p] - before[p] for p in before}
                    phase_data["elapsed_seconds"] = max(0, clock() - began)
                    phase_data["not_attempted"] = len(jobs) - phase_data["attempted"]
                    snapshots[phase] = (
                        freeze_selection(
                            selection.model_copy(update={"histories": tuple(references)}), plans
                        ),
                        freeze_selection(
                            benchmark.model_copy(update={"histories": tuple(benchmark_refs)}),
                            benchmark_plans,
                        ),
                    )
    except QualificationStop as exc:
        stop = exc.code
    except KeyboardInterrupt:
        stop = "INTERRUPTED"
    except Exception:
        stop = "HISTORY_COLLECTION_FAILED"
    finished = now()
    phase_complete = {p: p in phases and phases[p]["valid"] == len(jobs) for p in ("cold", "warm")}
    if stop == "COMPLETE" and not all(phase_complete.values()):
        stop = "HISTORY_COVERAGE_INCOMPLETE"
    chosen = snapshots.get("warm", snapshots.get("cold", (initial, initial_benchmark)))
    snapshot, benchmark_snapshot = chosen
    history = measure_selection(snapshot)
    provenance = ()
    updated = manifest.model_copy(
        update={
            "source_bindings": tuple(
                {
                    (b.provider, b.dataset, b.source_sha256, b.account_scope_sha256): b
                    for b in bindings
                }.values()
            )
        }
    )
    try:
        windows = validate_qualification_windows(
            snapshot, finished, origin, resolver=resolver if origin == "synthetic" else None
        )
        b_windows = validate_qualification_windows(
            benchmark_snapshot,
            finished,
            origin,
            resolver=resolver if origin == "synthetic" else None,
        )
        history = history.model_copy(
            update={
                "qualification_as_of": finished,
                "expected_windows_sha256": digest(
                    {
                        "candidate": [p.model_dump(mode="json") for p in windows],
                        "benchmark": [p.model_dump(mode="json") for p in b_windows],
                    }
                ),
                "benchmark_measured": measure_benchmark(
                    benchmark_snapshot.model_dump(mode="json"), snapshot
                ),
            }
        )
        provenance = validate_population_sources(
            snapshot, benchmark_snapshot, updated, origin=origin
        )
    except (ValueError, OSError):
        if stop == "COMPLETE":
            stop = "HISTORY_WINDOWS_OR_PROVENANCE_UNVERIFIED"
    b = budget_evidence(updated, budget, stop).model_copy(
        update={
            "elapsed_seconds": (finished - inputs.enrichment.started_at).total_seconds(),
            "inventory_complete_for_estimate": True,
            "cold_measured": phase_complete["cold"],
            "warm_measured": phase_complete["warm"],
        }
    )
    export = {
        "snapshot": snapshot.model_dump(mode="json"),
        "benchmark": benchmark_snapshot.model_dump(mode="json"),
        "inventories": [i.model_dump(mode="json") for i in inputs.enrichment.inventories],
        "mappings": inputs.mappings,
        "profiles": inputs.profiles,
        "budget": b.model_dump(mode="json"),
        "origin": origin,
        "started_at": inputs.enrichment.started_at.isoformat(),
        "finished_at": finished.isoformat(),
    }
    _, export_hash = private_json(output, "population-export.json", export)
    updated = updated.model_copy(update={"population_inputs_sha256": export_hash})
    immutable_write(output / "execution-workload.json", encode(updated.model_dump(mode="json")))
    immutable_write(
        output / "measurements.json",
        encode(
            {
                "version": "qualification-history-measurements-1",
                "origin": origin,
                "warm_scope": (
                    "same-window full refetch with fresh metadata; "
                    "not next-session cache performance"
                ),
                "phases": phases,
                "stop_reason": stop,
            }
        ),
    )
    report = report_for(
        updated,
        started=inputs.enrichment.started_at,
        finished=finished,
        origin=origin,
        inventories=inputs.enrichment.inventories,
        coverage=coverage,
        history=history,
        budget=b,
        provenance=provenance,
    )
    return write_report(output, report, now=now(), _reserved=True)
