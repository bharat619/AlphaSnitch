"""Two-stage qualification with reviewed manifests and local selection reuse.

The installed Yahoo SDK has no verified HTTP amplification bound. Its live
adapter deliberately cannot dispatch broad collection. Injected controlled
transports/export adapters can exercise the complete qualification path.
"""

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal
from uuid import uuid4

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
from stock_analyser.universe_policy import TECHNOLOGY_RULES_V1, reference_exclusions
from stock_analyser.universe_spike import digest
from stock_analyser.version import source_version

ROOT = Path(__file__).resolve().parents[2]


class WorkloadManifest(Evidence):
    version: Literal["qualification-workload-1"] = "qualification-workload-1"
    stage: Literal["inventory", "population"] = "inventory"
    criteria_sha256: Hash = model_hash(CRITERIA)
    scope_sha256: Hash = SCOPE
    max_pages_per_source: int = Field(default=2, strict=True, ge=1, le=100)
    request_ceiling: int = Field(default=10, strict=True, ge=1, le=10000)
    per_source_ceilings: dict[Literal["massive", "yfinance"], Count] = {
        "massive": 6,
        "yfinance": 4,
    }
    wall_seconds: float = Field(default=180, gt=0, le=3600, allow_inf_nan=False)
    concurrency: Literal[1] = 1
    reference_count: Count | None = None
    inventory_sha256: Hash | None = None
    population_inputs_sha256: Hash | None = None
    estimated_http_upper_bound: Count | None = None
    rights: tuple[RightEvidence, ...] = ()


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


def preflight(manifest, *, now):
    """No credential loading or provider imports; account rights are reviewed evidence."""
    blockers = []
    if manifest.criteria_sha256 != model_hash(CRITERIA) or manifest.scope_sha256 != SCOPE:
        blockers.append("MANIFEST_INCOMPATIBLE")
    if set(manifest.per_source_ceilings) != {"massive", "yfinance"}:
        blockers.append("SOURCE_CEILINGS_INCOMPLETE")
    rights = {(r.provider, r.dataset, r.operation): r for r in manifest.rights}
    if len(rights) != len(manifest.rights) or set(rights) != set(CRITERIA.required_rights):
        blockers.append("RIGHTS_EVIDENCE_MISSING")
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
        or manifest.estimated_http_upper_bound > manifest.request_ceiling
    ):
        blockers.append("POPULATION_WORKLOAD_UNBOUNDED")
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


def report_for(manifest, *, started, finished, origin, inventories, coverage, history, budget):
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
    )


def budget_evidence(manifest, budget, reason, *, sdk_calls=0):
    return BudgetEvidence(
        request_ceiling=manifest.request_ceiling,
        per_source_ceilings=manifest.per_source_ceilings,
        http_attempts={**budget.counts, "yfinance": None},
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
    reason = blockers[0] if blockers else "YAHOO_HTTP_AMPLIFICATION_UNBOUNDED"
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
    return manifest.model_copy(
        update={
            "stage": "population",
            "reference_count": len(reference.rows),
            "inventory_sha256": digest(
                [inventory_evidence(e).model_dump(mode="json") for e in (reference, discovery)]
            ),
            "estimated_http_upper_bound": None,
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
        or mapping["effective_from"] > as_of.date()
        or (mapping.get("effective_to") and mapping["effective_to"] <= as_of.date())
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
                if profile.get("conflicting"):
                    sector = "conflicting"
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


def run_inventory(client, output, manifest, budget, *, now=lambda: datetime.now(UTC)):
    from stock_analyser.universe_spike import enumerate_massive

    if output.exists():
        raise ValueError("Choose a new output directory; previous findings cannot be overwritten")
    started = now()
    if preflight(manifest, now=started):
        return observe_blocked(output, manifest, mode="inventory", now=now)
    if client.budget is not budget:
        raise ValueError("Inventory client must share the reviewed request budget")
    output.mkdir(parents=True, exist_ok=False)
    reference = enumerate_massive(client, max_pages=manifest.max_pages_per_source, now=now)
    supported = sum(potentially_supported(row) for row in reference.rows)
    coverage = empty_coverage(now()).model_copy(
        update={
            "reference_rows": len(reference.rows),
            "potentially_supported": supported,
            "excluded_listings": len(reference.rows) - supported,
            "unknown": supported,
            "unmatched": supported,
        }
    )
    report = report_for(
        manifest,
        started=started,
        finished=now(),
        origin="live",
        inventories=(
            inventory_evidence(reference),
            empty_inventory("yfinance", "YAHOO_HTTP_AMPLIFICATION_UNBOUNDED"),
        ),
        coverage=coverage,
        history=empty_history(),
        budget=budget_evidence(
            manifest,
            budget,
            budget.stop_reason
            or (
                "YAHOO_HTTP_AMPLIFICATION_UNBOUNDED"
                if reference.status == "COMPLETE"
                else reference.reason
            ),
        ),
    )
    return write_report(output, report, now=now(), _reserved=True)


def execute_population(output, manifest, inputs_path, *, now=lambda: datetime.now(UTC)):
    """Measure an explicitly reviewed, rights-permitted local population export.

    Live Yahoo collection is unavailable until HTTP amplification and account
    permissions are established. This path permits a complete stable-ID export
    without buying a source or retaining vendor details in aggregate evidence.
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
    coverage = classify_population(rows, mappings, profiles, now=acquired_finish)
    history = measure_selection(snapshot)
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
