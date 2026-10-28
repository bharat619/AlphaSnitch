"""Reviewed bounded inventories/enrichment, calculated plans and local selection qualification."""

import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import Field

from stock_analyser.qualification import (
    CRITERIA,
    SCOPE,
    BudgetEvidence,
    Count,
    CoverageEvidence,
    Evidence,
    Hash,
    HistoryEvidence,
    InventoryEvidence,
    PageEvidence,
    QualificationReport,
    RightEvidence,
    file_hash,
    implementation_hash,
    model_hash,
    requirements_hash,
    write_report,
)
from stock_analyser.qualification_budget import RequestBudget
from stock_analyser.qualification_plan import (
    CollectionControls,
    PopulationPlan,
    calculate_population_plan,
    plan_within_limits,
)
from stock_analyser.universe_policy import TECHNOLOGY_RULES_V1, reference_exclusions
from stock_analyser.universe_spike import digest
from stock_analyser.version import source_version

ROOT = Path(__file__).resolve().parents[2]


class SourceBinding(Evidence):
    """Reviewed local source identity; never infer the account from an API key."""

    provider: Literal["massive", "yfinance"]
    dataset: Literal["reference", "classification", "prices"]
    source_sha256: Hash
    account_scope_sha256: Hash


class WorkloadManifest(Evidence):
    version: Literal["qualification-workload-1"] = "qualification-workload-1"
    stage: Literal["inventory", "population"] = "inventory"
    criteria_sha256: Hash = model_hash(CRITERIA)
    scope_sha256: Hash = SCOPE
    max_pages_per_source: int = Field(default=2, strict=True, ge=1, le=100)
    request_ceiling: int = Field(default=10, strict=True, ge=1, le=100000)
    per_source_ceilings: dict[Literal["massive", "yfinance"], Count] = {
        "massive": 6,
        "yfinance": 4,
    }
    wall_seconds: float = Field(default=180, gt=0, le=86400, allow_inf_nan=False)
    concurrency: Literal[1] = 1
    reference_count: Count | None = None
    inventory_sha256: Hash | None = None
    population_inputs_sha256: Hash | None = None
    estimated_http_upper_bound: Count | None = None
    rights: tuple[RightEvidence, ...] = ()
    source_bindings: tuple[SourceBinding, ...] = ()
    controls: CollectionControls = CollectionControls()
    population_plan: PopulationPlan | None = None
    inventory_detail_sha256: Hash | None = None
    identity_mappings_sha256: Hash | None = None


def unknown_rights(now):
    return tuple(
        RightEvidence(
            provider=p,
            dataset=d,
            operation=o,
            disposition="unknown",
            reference="massive-market-data-2025-08-28" if p == "massive" else "yahoo-terms",
            document_sha256=None,
            reviewed_at=now,
            valid_until=now + timedelta(days=1),
            account_scope_sha256=digest("unknown"),
            intended_scope_sha256=SCOPE,
            retention_days=0,
            required_retention_days=365,
            conditions=("ACCOUNT_ENTITLEMENT_UNVERIFIED", "RETENTION_AND_DERIVED_USE_UNVERIFIED"),
        )
        for p, d, o in CRITERIA.required_rights
    )


def preflight(manifest, *, now, stage=None):
    """No credential loading or provider imports; account rights are reviewed evidence."""
    blockers = []
    if manifest.criteria_sha256 != model_hash(CRITERIA) or manifest.scope_sha256 != SCOPE:
        blockers.append("MANIFEST_INCOMPATIBLE")
    if set(manifest.per_source_ceilings) != {"massive", "yfinance"}:
        blockers.append("SOURCE_CEILINGS_INCOMPLETE")
    rights = {(r.provider, r.dataset, r.operation): r for r in manifest.rights}
    if len(rights) != len(manifest.rights) or set(rights) != set(CRITERIA.required_rights):
        blockers.append("RIGHTS_EVIDENCE_MISSING")
    for provider in ("massive", "yfinance"):
        if len({r.account_scope_sha256 for r in manifest.rights if r.provider == provider}) > 1:
            blockers.append("RIGHTS_ACCOUNT_SCOPE_CONFLICT")
    for r in manifest.rights:
        if (
            r.disposition != "permitted"
            or not r.document_sha256
            or r.conditions
            or r.intended_scope_sha256 != SCOPE
            or not now - timedelta(days=30) <= r.reviewed_at <= now <= r.valid_until
            or r.required_retention_days != CRITERIA.retention_days
            or r.retention_days < r.required_retention_days
        ):
            blockers.append("RIGHTS_NOT_PERMITTED")
    if manifest.stage == "population" and (
        manifest.reference_count is None
        or manifest.inventory_sha256 is None
        or manifest.estimated_http_upper_bound is None
        or (stage != "enrich" and manifest.estimated_http_upper_bound > manifest.request_ceiling)
    ):
        blockers.append("POPULATION_WORKLOAD_UNBOUNDED")
    if manifest.population_plan is not None:
        try:
            plan = calculate_population_plan(
                manifest.population_plan.inventories,
                manifest.controls,
                manifest.population_plan.classification,
            )
            if (
                plan != manifest.population_plan
                or plan.inventory_sha256 != manifest.inventory_sha256
                or plan.candidate_upper_bound != manifest.reference_count
                or plan.total_http_upper != manifest.estimated_http_upper_bound
            ):
                blockers.append("WORKLOAD_PLAN_MISMATCH")
            if not plan_within_limits(
                plan,
                overall=manifest.request_ceiling,
                sources=manifest.per_source_ceilings,
                wall_seconds=manifest.wall_seconds,
                enrichment=stage == "enrich",
            ):
                blockers.append("CALCULATED_WORKLOAD_EXCEEDS_REVIEWED_LIMITS")
            if (
                plan.classification is not None
                and not now - timedelta(hours=24) <= plan.classification.as_of <= now
            ):
                blockers.append("WORKLOAD_CLASSIFICATION_STALE_OR_FUTURE")
        except (ValueError, TypeError):
            blockers.append("WORKLOAD_PLAN_INVALID")
    return sorted(set(blockers))


def inventory_evidence(enumeration):
    return InventoryEvidence(
        source=enumeration.source,
        query="active_us_stocks" if enumeration.source == "massive" else "us_technology_discovery",
        status=enumeration.status,
        reason=enumeration.reason,
        method="terminal_page" if enumeration.source == "massive" else "reported_total",
        reported_total=enumeration.reported_total,
        raw_rows=enumeration.raw_rows,
        unique_rows=len(enumeration.rows),
        duplicates=enumeration.duplicates,
        errors=enumeration.errors,
        count_kind="exact" if enumeration.status == "COMPLETE" else "lower_bound",
        pages=tuple(
            PageEvidence(
                rows=p["rows"],
                sha256=p["sha256"],
                retrieved_at=p["retrieved_at"],
                terminal=(
                    not p.get("has_next", False)
                    if enumeration.source == "massive"
                    else p["offset"] + p["rows"] == enumeration.reported_total
                ),
            )
            for p in enumeration.pages
        ),
    )


def empty_inventory(source, reason):
    return InventoryEvidence(
        source=source,
        query="active_us_stocks" if source == "massive" else "us_technology_discovery",
        status="NOT_RUN",
        reason=reason,
        method="terminal_page" if source == "massive" else "reported_total",
        raw_rows=0,
        unique_rows=0,
        duplicates=0,
        errors=0,
        count_kind="lower_bound",
        pages=(),
    )


def empty_coverage(now):
    return CoverageEvidence(
        reference_rows=0,
        excluded_listings=0,
        potentially_supported=0,
        technology=0,
        non_technology=0,
        unknown=0,
        conflicting=0,
        as_of=now,
        taxonomy="yahoo-sector-map-1",
        method="not_measured",
        resolved=0,
        ambiguous=0,
        identity_conflicts=0,
        unmatched=0,
        stable_id_matches=0,
        corroborated_mapping_matches=0,
        distinct_technology_securities=0,
        duplicate_technology_listings=0,
    )


def empty_history(candidates=0):
    return HistoryEvidence(
        candidates=candidates,
        qualified=0,
        measured_excluded=0,
        failed=0,
        not_attempted=candidates,
        selected=0,
        requirements_sha256=requirements_hash(CRITERIA.selection_policy),
        selection_implementation="sector-selection-2",
        adjustment="raw",
        sessions=60,
        exclusion_reasons={},
        blocker_reasons={"HISTORY_NOT_MEASURED": candidates},
        benchmark_measured=False,
        calendars_and_feeds_verified=False,
    )


def report_for(
    manifest, *, started, finished, origin, inventories, coverage, history, budget, provenance=()
):
    return QualificationReport(
        schema_version="2",
        run_id=uuid4(),
        origin=origin,
        started_at=started,
        finished_at=finished,
        as_of=finished,
        source_version=source_version(),
        implementation_sha256=implementation_hash(),
        lock_sha256=file_hash(ROOT / "uv.lock"),
        policy_sha256=CRITERIA.policy_sha256,
        criteria=CRITERIA,
        criteria_sha256=model_hash(CRITERIA),
        selection_policy_sha256=model_hash(CRITERIA.selection_policy),
        scope_sha256=SCOPE,
        inventories=inventories,
        coverage=coverage,
        history=history,
        budget=budget,
        rights=manifest.rights or unknown_rights(finished),
        provenance=provenance,
        workload_plan=manifest.population_plan,
    )


def budget_evidence(manifest, budget, reason, *, sdk_calls=0):
    return BudgetEvidence(
        request_ceiling=manifest.request_ceiling,
        per_source_ceilings=manifest.per_source_ceilings,
        http_attempts=dict(budget.counts),
        sdk_calls=sdk_calls,
        retries=budget.retries,
        cooldown_seconds=budget.cooldown_seconds,
        elapsed_seconds=max(0, budget.clock() - budget.started),
        wall_seconds=manifest.wall_seconds,
        concurrency=1,
        conservative_http_upper_bound=manifest.estimated_http_upper_bound,
        inventory_complete_for_estimate=False,
        cold_measured=False,
        warm_measured=False,
        stop_reason=reason,
    )


def observe_blocked(output, manifest, *, mode="plan", now=lambda: datetime.now(UTC)):
    """A real preflight observation, including explicit zero-dispatch live failures."""
    if output.exists():
        raise ValueError("Choose a new output directory; previous findings cannot be overwritten")
    started = now()
    blockers = preflight(manifest, now=started)
    reason = blockers[0] if blockers else "POPULATION_EVIDENCE_REQUIRED"
    budget = RequestBudget(
        manifest.per_source_ceilings, manifest.request_ceiling, manifest.wall_seconds
    )
    report = report_for(
        manifest,
        started=started,
        finished=now(),
        origin="preflight" if mode == "plan" else "live",
        inventories=tuple(empty_inventory(s, reason) for s in ("massive", "yfinance")),
        coverage=empty_coverage(started),
        history=empty_history(),
        budget=budget_evidence(manifest, budget, reason),
    )
    result = write_report(output, report, now=now())
    if mode == "plan":
        planned = manifest.model_copy(update={"rights": manifest.rights or report.rights})
        with (output / "workload.json").open("x", encoding="utf-8") as stream:
            stream.write(planned.model_dump_json(indent=2) + "\n")
    return result


def population_manifest(reference, discovery, manifest):
    """Do not convert lower bounds into population ceilings. No guessed amplification."""
    if reference.status != "COMPLETE" or discovery.status != "COMPLETE":
        raise ValueError("Complete inventories required before planning population work")
    return planned_manifest(tuple(inventory_evidence(e) for e in (reference, discovery)), manifest)


def planned_manifest(inventories, manifest, classification=None):
    plan = calculate_population_plan(inventories, manifest.controls, classification)
    return manifest.model_copy(
        update={
            "stage": "population",
            "reference_count": plan.candidate_upper_bound,
            "inventory_sha256": plan.inventory_sha256,
            "estimated_http_upper_bound": plan.total_http_upper,
            "population_plan": plan,
        }
    )


def potentially_supported(row):
    """Only explicit out-of-policy facts exclude rows; unknowns stay in the denominator."""
    return not (
        row.get("active") is False
        or row.get("market") not in (None, "stocks")
        or row.get("locale") not in (None, "us")
        or row.get("type") not in (None, "", "CS")
        or row.get("primary_exchange") not in (None, "", *TECHNOLOGY_RULES_V1.listing_order)
        or (
            isinstance(row.get("currency_name"), str)
            and row["currency_name"].upper() not in ("", "USD")
        )
    )


def reconcile_identity(reference, mapping, *, as_of):
    """No symbol/name inference. Stable IDs plus listing facts, or a dated verified mapping.

    Mapping evidence stays local, outside the aggregate report. Alternate punctuation
    and renames must be explicit dated mappings; identifiers are never fabricated.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("Identity reconciliation requires an aware clock")
    local_date = as_of.astimezone(ZoneInfo("America/New_York")).date()
    if not mapping:
        return "unmatched", None
    if isinstance(mapping, list):
        return "ambiguous", None
    if reference_exclusions(reference):
        return "unmatched", None
    if (
        mapping.get("venue") != reference["primary_exchange"]
        or mapping.get("currency") != "USD"
        or mapping.get("type") != "CS"
        or not mapping.get("effective_from")
        or mapping["effective_from"] > local_date
        or (mapping.get("effective_to") and mapping["effective_to"] <= local_date)
    ):
        return "conflicting", None
    share, listing = mapping.get("share_class_figi"), mapping.get("composite_figi")
    if (share and share != reference["share_class_figi"]) or (
        listing and listing != reference["composite_figi"]
    ):
        return "conflicting", None
    if (
        share
        and listing
        and isinstance(mapping.get("provider_symbol"), str)
        and mapping["provider_symbol"].strip()
    ):
        return "resolved", "stable_id"
    if (
        mapping.get("method") == "corroborated_mapping"
        and isinstance(mapping.get("source_sha256"), str)
        and len(mapping["source_sha256"]) == 64
        and all(c in "0123456789abcdef" for c in mapping["source_sha256"])
        and mapping.get("provider_symbol")
        and mapping.get("reviewed_at")
        and mapping["reviewed_at"] <= as_of
    ):
        return "resolved", "corroborated_mapping"
    return "unmatched", None


def measure_selection(snapshot):
    """Reopen every retained input through the existing history/liquidity evaluator.

    Calling code must establish rights BEFORE collecting/retaining these local files.
    Failed retrieval/history is never converted to a low-liquidity exclusion.
    """
    from stock_analyser.universe import replay_universe

    snapshot = replay_universe(snapshot)
    if snapshot.implementation_version != "sector-selection-2":
        raise ValueError("Historical selection semantics cannot qualify the pilot")
    if snapshot.request.policy != CRITERIA.selection_policy:
        raise ValueError("Selection policy differs from qualification criteria")
    decisions = snapshot.result.decisions
    excluded, blockers = Counter(), Counter()
    qualified = failed = measured_excluded = 0
    # Only duplicates and known non-Technology/out-of-scope rows leave this denominator.
    measured = {"BELOW_MINIMUM_PRICE", "BELOW_MINIMUM_LIQUIDITY"}
    candidates = 0
    for decision in decisions:
        candidate = next(
            c
            for c in snapshot.request.population.candidates
            if c.reference.ticker == decision.ticker
        )
        if (
            not potentially_supported(candidate.reference.model_dump())
            or "OUTSIDE_SECTOR" in decision.reasons
            or "DUPLICATE_SECURITY" in decision.reasons
        ):
            continue
        if (
            candidate.classification is None
            or candidate.classification.mapped_sector != "technology"
        ):
            continue
        candidates += 1
        reasons = set(decision.reasons)
        if not reasons:
            qualified += 1
        elif reasons <= measured:
            measured_excluded += 1
            excluded.update(reasons)
        else:
            failed += 1
            blockers.update(reasons - measured)
    return HistoryEvidence(
        candidates=candidates,
        qualified=qualified,
        measured_excluded=measured_excluded,
        failed=failed,
        not_attempted=0,
        selected=len(snapshot.result.selected),
        requirements_sha256=requirements_hash(snapshot.request.policy),
        selection_implementation=snapshot.implementation_version,
        adjustment="raw",
        sessions=60,
        exclusion_reasons=dict(excluded),
        blocker_reasons=dict(blockers),
        selection_inputs_sha256=snapshot.inputs_sha256,
        # Benchmark and live feed/publication feasibility require separate measurements.
        benchmark_measured=False,
        calendars_and_feeds_verified=not failed,
    )


def classify_population(rows, mappings, profiles, *, now):
    """Classify all potentially supported listings, including discovery absences.

    Inputs are local transient dictionaries keyed by reference ticker. Profiles
    must use the verified mapped symbol and carry a fresh retrieval timestamp.
    """
    supported = [r for r in rows if potentially_supported(r)]
    counts, methods = Counter(), Counter()
    sector_counts = Counter()
    sectors, listings, outcomes = {}, {}, {}
    for row in supported:
        key = row["ticker"]
        outcome, method = reconcile_identity(row, mappings.get(key), as_of=now)
        counts[outcome] += 1
        outcomes[key] = (outcome, method)
        if method:
            methods[method] += 1
        profile = profiles.get(key)
        mapping = mappings.get(key)
        sector = "unknown"
        if outcome == "resolved" and profile and isinstance(mapping, dict):
            stamp = profile.get("retrieved_at")
            if (
                isinstance(stamp, datetime)
                and stamp.tzinfo is not None
                and now - timedelta(hours=24) <= stamp <= now
                and profile.get("symbol") == mapping.get("provider_symbol")
            ):
                raw = profile.get("sector")
                sector = (
                    "technology"
                    if raw == "Technology"
                    else "non_technology"
                    if raw in TECHNOLOGY_RULES_V1.other_known_sectors
                    else "unknown"
                )
                if profile.get("identity_unverified"):
                    sector = "unknown"
                    counts["resolved"] -= 1
                    counts["unmatched"] += 1
                    methods[method] -= 1
                    outcomes[key] = ("unmatched", None)
                elif profile.get("conflicting"):
                    sector = "conflicting"
                    counts["resolved"] -= 1
                    counts["conflicting"] += 1
                    methods[method] -= 1
                    outcomes[key] = ("conflicting", None)
        sector_counts[sector] += 1
        sectors[key] = sector
        if row.get("composite_figi"):
            listings.setdefault(row["composite_figi"], []).append(row)
    # Conflicting listing identifiers and share-class sector claims stay visible.
    conflict_tickers = {
        r["ticker"]
        for group in listings.values()
        for r in group
        if len({(x.get("share_class_figi"), x.get("primary_exchange"), x["ticker"]) for x in group})
        > 1
    }
    symbols = {}
    for row in supported:
        key = row["ticker"]
        mapping = mappings.get(key)
        if outcomes[key][0] == "resolved" and isinstance(mapping, dict):
            symbols.setdefault(mapping.get("provider_symbol"), []).append(row)
    for group in symbols.values():
        if len({(r.get("share_class_figi"), r.get("composite_figi")) for r in group}) > 1:
            conflict_tickers.update(r["ticker"] for r in group)
    shares = {}
    for row in supported:
        if row.get("share_class_figi"):
            shares.setdefault(row["share_class_figi"], []).append(row)
    for ticker in conflict_tickers:
        old, method = outcomes[ticker]
        counts[old] -= 1
        counts["conflicting"] += 1
        if method:
            methods[method] -= 1
    for group in shares.values():
        if len({sectors[r["ticker"]] for r in group} - {"unknown"}) > 1:
            conflict_tickers.update(r["ticker"] for r in group)
    for ticker in conflict_tickers:
        sector_counts[sectors[ticker]] -= 1
        sector_counts["conflicting"] += 1
        sectors[ticker] = "conflicting"
    technology_shares = {
        r["share_class_figi"] for r in supported if sectors[r["ticker"]] == "technology"
    }
    return CoverageEvidence(
        reference_rows=len(rows),
        excluded_listings=len(rows) - len(supported),
        potentially_supported=len(supported),
        technology=sector_counts["technology"],
        non_technology=sector_counts["non_technology"],
        unknown=sector_counts["unknown"],
        conflicting=sector_counts["conflicting"],
        as_of=now,
        taxonomy="yahoo-sector-map-1",
        method="all_reference_profiles",
        resolved=counts["resolved"],
        ambiguous=counts["ambiguous"],
        identity_conflicts=counts["conflicting"],
        unmatched=counts["unmatched"],
        stable_id_matches=methods["stable_id"],
        corroborated_mapping_matches=methods["corroborated_mapping"],
        distinct_technology_securities=len(technology_shares),
        duplicate_technology_listings=sector_counts["technology"] - len(technology_shares),
    )


def yahoo_client(budget, controls, *, transport=None, sleep=None):
    from stock_analyser.qualification_yahoo import YahooQualificationClient

    kwargs = {"transport": transport} if transport is not None else {}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if transport is None:
        from platformdirs import user_data_path

        kwargs["lease_path"] = user_data_path("stock-analyser", appauthor=False) / "yahoo.lock"
    return YahooQualificationClient(
        budget,
        http_cap=controls.yahoo_http_per_operation,
        retries=controls.yahoo_retries,
        requests_per_minute=controls.yahoo_requests_per_minute,
        timeout_seconds=controls.yahoo_timeout_seconds,
        max_cooldown_seconds=controls.yahoo_max_cooldown_seconds,
        **kwargs,
    )


def private_json(output, name, value):
    from stock_analyser.collection import encode, immutable_write

    private = output / "private"
    private.mkdir(mode=0o700, exist_ok=True)
    path = private / name
    return path, immutable_write(path, encode(value))


def run_inventory(client, output, manifest, budget, *, now=lambda: datetime.now(UTC), yahoo=None):
    from dataclasses import asdict

    from stock_analyser.universe_spike import enumerate_massive, enumerate_yahoo

    if output.exists():
        raise ValueError("Choose a new output directory; previous findings cannot be overwritten")
    started = now()
    if preflight(manifest, now=started):
        return observe_blocked(output, manifest, mode="inventory", now=now)
    if manifest.stage != "inventory":
        raise ValueError("Inventory mode requires an inventory manifest")
    if client.budget is not budget or client.settings != manifest.controls.massive:
        raise ValueError("Inventory transport controls differ from reviewed manifest")
    output.mkdir(parents=True, exist_ok=False)
    reference = enumerate_massive(client, max_pages=manifest.max_pages_per_source, now=now)
    owned = yahoo is None
    try:
        yahoo = yahoo or yahoo_client(budget, manifest.controls)
        if (
            yahoo.budget is not budget
            or yahoo.http_cap != manifest.controls.yahoo_http_per_operation
        ):
            raise ValueError("Yahoo must share reviewed transport controls")
        discovery = enumerate_yahoo(yahoo.screen, max_pages=manifest.max_pages_per_source, now=now)
    except Exception:
        from stock_analyser.universe_spike import Enumeration

        discovery = Enumeration("yfinance", {}, reason="YAHOO_SETUP_FAILURE", errors=1)
    finally:
        if owned and yahoo is not None:
            yahoo.close()
    finished = now()
    evidence = (inventory_evidence(reference), inventory_evidence(discovery))
    detail = {
        "version": "qualification-inventory-detail-1",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "controls": manifest.controls.model_dump(mode="json"),
        "reference": asdict(reference),
        "discovery": asdict(discovery),
        "http_attempts": dict(budget.counts),
        "retries": budget.retries,
        "cooldown_seconds": budget.cooldown_seconds,
    }
    ref_path, ref_hash = private_json(output, "reference.json", reference.rows)
    detail["reference_source"] = {
        "provider": "massive",
        "provider_symbol": "US:active_stocks",
        "retrieved_at": finished.isoformat(),
        "reference": str(ref_path.resolve()),
        "sha256": ref_hash,
    }
    _, detail_hash = private_json(output, "inventory.json", detail)
    if reference.status == discovery.status == "COMPLETE":
        planned = population_manifest(reference, discovery, manifest).model_copy(
            update={
                "inventory_detail_sha256": detail_hash,
                "source_bindings": (
                    *manifest.source_bindings,
                    SourceBinding(
                        provider="massive",
                        dataset="reference",
                        source_sha256=ref_hash,
                        account_scope_sha256=next(
                            r.account_scope_sha256
                            for r in manifest.rights
                            if r.provider == "massive"
                        ),
                    ),
                ),
            }
        )
        with (output / "population-workload.json").open("x", encoding="utf-8") as stream:
            stream.write(planned.model_dump_json(indent=2) + "\n")
    supported = sum(potentially_supported(row) for row in reference.rows)
    coverage = empty_coverage(finished).model_copy(
        update={
            "reference_rows": len(reference.rows),
            "potentially_supported": supported,
            "excluded_listings": len(reference.rows) - supported,
            "unknown": supported,
            "unmatched": supported,
        }
    )
    reason = (
        "INVENTORY_COMPLETE"
        if reference.status == discovery.status == "COMPLETE"
        else budget.stop_reason or "INVENTORY_INCOMPLETE"
    )
    report = report_for(
        manifest,
        started=started,
        finished=finished,
        origin="live",
        inventories=evidence,
        coverage=coverage,
        history=empty_history(),
        budget=budget_evidence(manifest, budget, reason),
    )
    return write_report(output, report, now=now(), _reserved=True)


def run_population_enrichment(
    output,
    manifest,
    inventory_path,
    mappings_path,
    *,
    now=lambda: datetime.now(UTC),
    transport=None,
    sleep=None,
    clock=time.monotonic,
):
    """Visit every potentially supported listing, including discovery absences.

    Missing/ambiguous mappings are retained as unknown, never inferred from symbol
    equality. Output details are private, rights-permitted local evidence. Price
    selection/replay consumes this metadata in the separate execute stage.
    """
    import json

    from stock_analyser.universe_spike import (
        DISCOVERY_FIELDS,
        REFERENCE_FIELDS,
        Enumeration,
        project,
    )

    if output.exists():
        raise ValueError("Choose a new output directory; previous findings cannot be overwritten")
    if preflight(manifest, now=now(), stage="enrich"):
        return observe_blocked(output, manifest, mode="enrich", now=now)
    if (
        manifest.stage != "population"
        or manifest.population_plan is None
        or manifest.inventory_detail_sha256 != file_hash(inventory_path)
        or manifest.identity_mappings_sha256 != file_hash(mappings_path)
    ):
        raise ValueError(
            "Native enrichment requires calculated plan and exact reviewed input hashes"
        )
    detail = json.loads(inventory_path.read_text())
    if detail.get("version") != "qualification-inventory-detail-1":
        raise ValueError("Unsupported inventory detail version")
    if detail["controls"] != manifest.controls.model_dump(mode="json"):
        raise ValueError("Inventory controls differ from the population plan")
    reference, discovery = Enumeration(**detail["reference"]), Enumeration(**detail["discovery"])
    reference.rows = project(reference.rows, REFERENCE_FIELDS, "ticker")
    discovery.rows = project(discovery.rows, DISCOVERY_FIELDS, "symbol")
    inventories = tuple(inventory_evidence(e) for e in (reference, discovery))
    if (
        calculate_population_plan(
            inventories, manifest.controls, manifest.population_plan.classification
        )
        != manifest.population_plan
    ):
        raise ValueError("Actual inventories differ from calculated plan")
    from stock_analyser.contracts import SourceRecord

    source = SourceRecord.model_validate(detail["reference_source"])
    if (
        source.provider != "massive"
        or source.sha256 != file_hash(Path(source.reference))
        or project(json.loads(Path(source.reference).read_text()), REFERENCE_FIELDS, "ticker")
        != reference.rows
    ):
        raise ValueError("Retained reference source does not match inventory")
    for enumeration, key in ((reference, "ticker"), (discovery, "symbol")):
        if len({r[key] for r in enumeration.rows}) != len(enumeration.rows):
            raise ValueError("Duplicate private inventory identifiers")
        offset = 0
        for page in enumeration.pages:
            if digest(enumeration.rows[offset : offset + page["rows"]]) != page["sha256"]:
                raise ValueError("Private inventory page hash mismatch")
            offset += page["rows"]
        if offset != len(enumeration.rows):
            raise ValueError("Private inventory rows are incomplete")
    acquired_start = datetime.fromisoformat(detail["started_at"])
    acquired_finish = datetime.fromisoformat(detail["finished_at"])
    if not now() - timedelta(hours=24) <= acquired_start <= acquired_finish <= now() or any(
        not acquired_start <= p.retrieved_at <= acquired_finish
        for i in inventories
        for p in i.pages
    ):
        raise ValueError("Private inventories are stale, future or inconsistent")
    mappings = json.loads(mappings_path.read_text())
    if not isinstance(mappings, dict):
        raise ValueError("Invalid reviewed mappings")
    for item in mappings.values():
        if isinstance(item, dict):
            for key in ("effective_from", "effective_to"):
                if item.get(key):
                    from datetime import date

                    item[key] = date.fromisoformat(item[key])
            if item.get("reviewed_at"):
                item["reviewed_at"] = datetime.fromisoformat(item["reviewed_at"])
    started = datetime.fromisoformat(detail["started_at"])
    elapsed = (now() - started).total_seconds()
    if elapsed < 0:
        raise ValueError("Future inventory start")
    budget = RequestBudget(
        manifest.per_source_ceilings, manifest.request_ceiling, manifest.wall_seconds, clock=clock
    )
    consumed = detail["http_attempts"]
    if (
        set(consumed) != {"massive", "yfinance"}
        or any(
            type(v) is not int or v < 0 or v > manifest.per_source_ceilings[p]
            for p, v in consumed.items()
        )
        or sum(consumed.values()) > manifest.request_ceiling
    ):
        raise ValueError("Invalid inventory request accounting")
    import math

    if (
        type(detail["retries"]) is not int
        or not 0 <= detail["retries"] <= sum(consumed.values())
        or not isinstance(detail["cooldown_seconds"], (int, float))
        or not math.isfinite(detail["cooldown_seconds"])
        or detail["cooldown_seconds"] < 0
    ):
        raise ValueError("Invalid inventory retry/cooldown accounting")
    budget.counts.update(consumed)
    budget.started -= elapsed
    budget.retries = detail["retries"]
    budget.cooldown_seconds = detail["cooldown_seconds"]
    output.mkdir(parents=True, exist_ok=False)
    yahoo = yahoo_client(budget, manifest.controls, transport=transport, sleep=sleep)
    profiles, sources, bindings = {}, {}, []
    attempted = 0
    stop = "ENRICHMENT_COMPLETE"
    try:
        for row in reference.rows:
            if not potentially_supported(row):
                continue
            budget.remaining()
            key = row["ticker"]
            mapping = mappings.get(key)
            symbol = mapping.get("provider_symbol") if isinstance(mapping, dict) else None
            # An unverified exact symbol is only a diagnostic profile lookup;
            # classify_population still requires corroborated identity evidence.
            symbol = symbol or key
            try:
                attempted += 1
                profile = yahoo.profile(symbol)
                venues = {
                    "NMS": "XNAS",
                    "NGM": "XNAS",
                    "NCM": "XNAS",
                    "NYQ": "XNYS",
                    "ASE": "XASE",
                    "PCX": "ARCX",
                    "BTS": "BATS",
                }
                profile["identity_unverified"] = any(
                    profile[k] is None for k in ("currency", "quoteType", "exchange")
                )
                profile["conflicting"] = not profile["identity_unverified"] and (
                    profile["currency"] != "USD"
                    or profile["quoteType"] != "EQUITY"
                    or venues.get(profile["exchange"]) != row.get("primary_exchange")
                )
                profile["retrieved_at"] = now()
                source_path, sha = private_json(
                    output,
                    f"profile-{len(sources):06d}.json",
                    {**profile, "retrieved_at": profile["retrieved_at"].isoformat()},
                )
                profiles[key] = profile
                account = next(
                    r.account_scope_sha256 for r in manifest.rights if r.provider == "yfinance"
                )
                sources[key] = {
                    "provider": "yfinance",
                    "provider_symbol": profile["symbol"],
                    "retrieved_at": profile["retrieved_at"].isoformat(),
                    "reference": str(source_path.resolve()),
                    "sha256": sha,
                }
                bindings.append(
                    SourceBinding(
                        provider="yfinance",
                        dataset="classification",
                        source_sha256=sha,
                        account_scope_sha256=account,
                    )
                )
            except OSError:
                stop = "LOCAL_EVIDENCE_WRITE_FAILED"
                break
            except Exception as exc:
                from stock_analyser.qualification_budget import QualificationStop

                if isinstance(exc, QualificationStop) and exc.code in ("DEADLINE", "REQUEST_LIMIT"):
                    stop = exc.code
                    break
                # No upstream text or per-symbol failures enter aggregate evidence.
                continue
    except KeyboardInterrupt:
        stop = "INTERRUPTED"
    except Exception:
        stop = budget.stop_reason or "ENRICHMENT_STOPPED"
    finally:
        yahoo.close()
    finished = now()
    if stop == "ENRICHMENT_COMPLETE" and len(profiles) < sum(
        potentially_supported(r) for r in reference.rows
    ):
        stop = "ENRICHMENT_PARTIAL"
    from stock_analyser.qualification import ProfileMeasurements

    coverage = classify_population(reference.rows, mappings, profiles, now=finished)
    coverage = coverage.model_copy(
        update={
            "profile_measurements": ProfileMeasurements(
                attempted=attempted,
                succeeded=len(profiles),
                failed=attempted - len(profiles),
                not_attempted=coverage.potentially_supported - attempted,
            )
        }
    )
    private_json(
        output,
        "population.json",
        {
            "version": "qualified-enrichment-1",
            "reference": detail["reference"],
            "discovery": detail["discovery"],
            "profiles": {
                k: {**p, "retrieved_at": p["retrieved_at"].isoformat()} for k, p in profiles.items()
            },
            "mappings": json.loads(mappings_path.read_text()),
            "profile_sources": sources,
            "reference_source": detail["reference_source"],
        },
    )
    updated = manifest
    if not any(
        (
            coverage.unknown,
            coverage.conflicting,
            coverage.unmatched,
            coverage.ambiguous,
            coverage.identity_conflicts,
        )
    ):
        updated = planned_manifest(inventories, manifest, classification=coverage)
    with (output / "enrichment-workload.json").open("x", encoding="utf-8") as stream:
        stream.write(
            updated.model_copy(
                update={"source_bindings": (*manifest.source_bindings, *bindings)}
            ).model_dump_json(indent=2)
            + "\n"
        )
    b = budget_evidence(manifest, budget, stop).model_copy(
        update={
            "elapsed_seconds": (finished - started).total_seconds(),
            "inventory_complete_for_estimate": True,
        }
    )
    report = report_for(
        manifest,
        started=started,
        finished=finished,
        origin="live",
        inventories=inventories,
        coverage=coverage,
        history=empty_history(coverage.distinct_technology_securities),
        budget=b,
    )
    return write_report(output, report, now=now(), _reserved=True)


def execute_population(
    output, manifest, inputs_path, *, now=lambda: datetime.now(UTC), resolver=None
):
    """Measure an explicitly reviewed, rights-permitted local population export.

    The bounded native metadata stages may supply these inputs. Price selection
    uses the existing retained-history contracts. Current account rights must pass
    before any detailed data is read or any provider collection is dispatched.
    """
    import json

    from stock_analyser.universe import UniverseSnapshot
    from stock_analyser.universe_spike import REFERENCE_FIELDS, project

    if output.exists():
        raise ValueError("Choose a new output directory; previous findings cannot be overwritten")
    started = now()
    if preflight(manifest, now=started):
        return observe_blocked(output, manifest, mode="execute", now=now)
    if (
        manifest.stage != "population"
        or not manifest.population_inputs_sha256
        or file_hash(inputs_path) != manifest.population_inputs_sha256
    ):
        raise ValueError("Population input hash differs from the reviewed manifest")
    # This file is LOCAL ONLY: raw metadata and histories must never enter findings.
    inputs = json.loads(inputs_path.read_text())
    if set(inputs) != {
        "snapshot",
        "inventories",
        "mappings",
        "profiles",
        "budget",
        "origin",
        "started_at",
        "finished_at",
        "benchmark",
    }:
        raise ValueError("Unsupported population export fields")
    snapshot = UniverseSnapshot.model_validate(inputs["snapshot"])
    inventories = tuple(InventoryEvidence.model_validate(i) for i in inputs["inventories"])
    if (
        len(inventories) != 2
        or {i.source for i in inventories} != {"massive", "yfinance"}
        or any(i.status != "COMPLETE" for i in inventories)
        or digest([i.model_dump(mode="json") for i in inventories]) != manifest.inventory_sha256
    ):
        raise ValueError("Complete inventories differ from reviewed population plan")
    calculated = calculate_population_plan(
        inventories,
        manifest.controls,
        manifest.population_plan.classification if manifest.population_plan is not None else None,
    )
    if (
        manifest.population_plan != calculated
        or manifest.estimated_http_upper_bound != calculated.total_http_upper
    ):
        raise ValueError("Execution requires the calculated reviewed population plan")
    rows = [c.reference.model_dump() for c in snapshot.request.population.candidates]
    reference = next(i for i in inventories if i.source == "massive")
    if len(rows) != manifest.reference_count or len(rows) != reference.unique_rows:
        raise ValueError("Population does not cover complete reference inventory")
    # Page hashes must cover exactly the same reference rows, in provider order.
    offset = 0
    for page in reference.pages:
        if (
            digest(project(rows[offset : offset + page.rows], REFERENCE_FIELDS, "ticker"))
            != page.sha256
        ):
            raise ValueError("Reference population/page hash mismatch")
        offset += page.rows
    if offset != len(rows):
        raise ValueError("Reference page denominator mismatch")
    acquired_start = datetime.fromisoformat(inputs["started_at"])
    acquired_finish = datetime.fromisoformat(inputs["finished_at"])
    if inputs["origin"] not in ("live", "synthetic"):
        raise ValueError("Population export must identify its evidence origin")
    if (
        inputs["origin"] == "live"
        and snapshot.request.cutoff_policy.publication_timing == "synthetic"
    ):
        raise ValueError("Synthetic calendar evidence cannot qualify a live population")
    mappings, profiles = inputs["mappings"], inputs["profiles"]
    for mapping in mappings.values():
        if isinstance(mapping, dict):
            for key in ("effective_from", "effective_to"):
                if mapping.get(key):
                    from datetime import date

                    mapping[key] = date.fromisoformat(mapping[key])
            if mapping.get("reviewed_at"):
                mapping["reviewed_at"] = datetime.fromisoformat(mapping["reviewed_at"])
    for profile in profiles.values():
        profile["retrieved_at"] = datetime.fromisoformat(profile["retrieved_at"])
    for candidate in snapshot.request.population.candidates:
        if not potentially_supported(candidate.reference.model_dump()):
            continue
        profile = profiles.get(candidate.reference.ticker)
        classification = candidate.classification
        if (
            profile
            and classification
            and (
                classification.original_sector != profile.get("sector")
                or classification.source.retrieved_at != profile["retrieved_at"]
                or classification.source.provider_symbol != profile.get("symbol")
            )
        ):
            raise ValueError("Selection classification conflicts with population profile")
    # Resolve at the qualification instant, independently of the frozen request.
    # Injected calendars are only for explicitly synthetic exports.
    expected_windows = validate_qualification_windows(
        snapshot, acquired_finish, inputs["origin"], resolver=resolver
    )
    benchmark = (
        UniverseSnapshot.model_validate(inputs["benchmark"])
        if inputs["benchmark"] is not None
        else None
    )
    benchmark_windows = ()
    if benchmark is not None:
        benchmark_windows = validate_qualification_windows(
            benchmark, acquired_finish, inputs["origin"], resolver=resolver
        )
    provenance = validate_population_sources(snapshot, benchmark, manifest, origin=inputs["origin"])
    coverage = classify_population(rows, mappings, profiles, now=acquired_finish)
    history = measure_selection(snapshot).model_copy(
        update={
            "qualification_as_of": acquired_finish,
            "expected_windows_sha256": digest(
                {
                    "candidate": [p.model_dump(mode="json") for p in expected_windows],
                    "benchmark": [p.model_dump(mode="json") for p in benchmark_windows],
                }
            ),
        }
    )
    if inputs["benchmark"] is not None:
        history = history.model_copy(
            update={
                "benchmark_measured": measure_benchmark(inputs["benchmark"], snapshot),
            }
        )
    budget = BudgetEvidence.model_validate(inputs["budget"])
    if (
        budget.request_ceiling != manifest.request_ceiling
        or budget.per_source_ceilings != manifest.per_source_ceilings
        or budget.wall_seconds != manifest.wall_seconds
        or budget.conservative_http_upper_bound != manifest.estimated_http_upper_bound
    ):
        raise ValueError("Measured workload differs from reviewed limits")
    # Imported collection timestamps and elapsed budget describe the acquisition,
    # not the near-zero time taken to evaluate local files.
    report = report_for(
        manifest,
        started=acquired_start,
        finished=acquired_finish,
        origin=inputs["origin"],
        inventories=inventories,
        coverage=coverage,
        history=history,
        budget=budget,
        provenance=provenance,
    )
    return write_report(output, report, now=now())


def measure_benchmark(data, snapshot):
    """Verify SPY windows independently, without imposing candidate sector/liquidity rules."""
    from stock_analyser.universe import UniverseSnapshot, _history

    benchmark = UniverseSnapshot.model_validate(data)
    request = benchmark.request
    if (
        request.market != snapshot.request.market
        or request.requested_as_of != snapshot.request.requested_as_of
        or request.policy != snapshot.request.policy
        or request.cutoff_policy != snapshot.request.cutoff_policy
    ):
        return False
    candidates = [
        c
        for c in request.population.candidates
        if c.instrument is not None and c.instrument.symbol == "SPY"
    ]
    if len(candidates) != 1 or not candidates[0].identity_verified:
        return False
    instrument = candidates[0].instrument
    for requirement in request.policy.requirements():
        references = [
            h
            for h in request.histories
            if h.metadata.instrument == instrument
            and h.metadata.timeframe == requirement.timeframe
            and h.metadata.adjustment == requirement.adjustment
        ]
        plans = [
            p
            for p in benchmark.resolutions
            if p.venue == instrument.venue and p.requirement == requirement
        ]
        if len(references) != 1 or len(plans) != 1:
            return False
        try:
            _history(references[0], instrument, request, plans[0])
        except (OSError, ValueError):
            return False
    return True


def qualification_resolutions(snapshot, as_of, origin, resolver=None):
    from stock_analyser.calendars import ExchangeCalendarAdapter, HistoryResolver
    from stock_analyser.universe import resolve_selection

    if origin == "live":
        if resolver is not None:
            raise ValueError("Live qualification requires the maintained calendar adapter")
        resolver = HistoryResolver(ExchangeCalendarAdapter(), snapshot.request.cutoff_policy)
    elif resolver is None:
        raise ValueError("Synthetic qualification requires an explicit test calendar")
    if resolver.policy != snapshot.request.cutoff_policy:
        raise ValueError("Qualification calendar policy differs from selection")
    return resolve_selection(
        snapshot.request.model_copy(update={"requested_as_of": as_of}), resolver
    )


def validate_qualification_windows(snapshot, as_of, origin, *, resolver=None):
    """Compare full independently resolved daily/weekly windows, not just timestamps.

    Same-window reuse over weekends is valid. Replaying an older generation after
    a new required session completes is not evidence of current coverage.
    """
    expected = qualification_resolutions(snapshot, as_of, origin, resolver)

    def keys(p):
        return p.venue, p.requirement.timeframe, p.requirement.adjustment

    frozen = {keys(p): p for p in snapshot.resolutions}
    if len(frozen) != len(expected):
        raise ValueError("Qualification history windows are incomplete")
    for current in expected:
        old = frozen.get(keys(current))
        if (
            old is None
            or old.periods != current.periods
            or old.calendar != current.calendar
            or old.calendar_data_version != current.calendar_data_version
            or old.policy != current.policy
            or old.requirement != current.requirement
            or len(current.periods) != current.requirement.required_bars
        ):
            raise ValueError("History is not current at the qualification as-of time")
    if snapshot.request.requested_as_of > as_of:
        raise ValueError("History request is later than qualification as-of")
    return expected


def validate_population_sources(snapshot, benchmark, manifest, *, origin):
    """Bind actual retained source records to reviewed dataset/account permissions.

    Synthetic fixture evaluation remains available offline, but its origin cannot
    qualify a live release. Publication flags never turn a fixture into live data.
    """
    from stock_analyser.cache import load_snapshot
    from stock_analyser.qualification import SourceUse

    if origin == "synthetic":
        return ()
    bindings = {(b.provider, b.dataset, b.source_sha256): b for b in manifest.source_bindings}
    if len(bindings) != len(manifest.source_bindings):
        raise ValueError("Duplicate source/account bindings")
    rights = {(r.provider, r.dataset, r.operation): r for r in manifest.rights}
    uses = {}

    def require(source, dataset, provider):
        if source.provider != provider:
            raise ValueError("Actual source provider is not permitted for this dataset")
        try:
            actual_hash = file_hash(Path(source.reference))
        except OSError:
            raise ValueError("Actual source detail is not available for verification") from None
        if actual_hash != source.sha256:
            raise ValueError("Actual source bytes differ from reviewed source hash")
        binding = bindings.get((provider, dataset, source.sha256))
        if binding is None:
            raise ValueError("Actual source has no reviewed account/dataset binding")
        for operation in ("collect", "retain", "derive_internal"):
            right = rights.get((provider, dataset, operation))
            if (
                right is None
                or right.disposition != "permitted"
                or right.conditions
                or right.account_scope_sha256 != binding.account_scope_sha256
            ):
                raise ValueError("Actual source account does not match dataset permissions")
        uses.setdefault((provider, dataset, binding.account_scope_sha256), set()).add(source.sha256)

    require(snapshot.request.population.source, "reference", "massive")
    for candidate in snapshot.request.population.candidates:
        if candidate.classification is not None:
            require(candidate.classification.source, "classification", "yfinance")
    for item in (snapshot, benchmark):
        if item is None:
            continue
        if item.request.cutoff_policy.publication_timing != "observed" or any(
            route.feed.kind == "synthetic" for route in item.request.market.routes
        ):
            raise ValueError("Synthetic or unverified provenance cannot qualify live evidence")
        for reference in item.request.histories:
            packet = load_snapshot(reference)
            if packet.metadata.feed.kind not in ("consolidated", "venue"):
                raise ValueError("Synthetic or discovery prices cannot qualify live evidence")
            if packet.response.get("provider") != "massive":
                raise ValueError("Actual price response provider is outside permitted scope")
            if not packet.metadata.sources:
                raise ValueError("Price evidence has no actual source provenance")
            for source in packet.metadata.sources:
                require(source, "prices", "massive")
    return tuple(
        SourceUse(
            provider=p,
            dataset=d,
            account_scope_sha256=a,
            source_count=len(hashes),
            source_set_sha256=digest(sorted(hashes)),
        )
        for (p, d, a), hashes in sorted(uses.items())
    )
