"""Conservative population costs calculated from exact inventories and enforced caps."""

from stock_analyser.qualification import (
    CRITERIA,
    CollectionControls,
    InventoryEvidence,
    PopulationPlan,
    requirements_hash,
)
from stock_analyser.universe_spike import digest


def yahoo_http_upper(operations, controls):
    # Each session allows at most two auth attempts (cookie+crumb), and one
    # credential-refresh replay. Data retries and operation caps also apply.
    return (
        min(
            operations * controls.yahoo_http_per_operation,
            operations * (controls.yahoo_retries + 1) + 5,
        )
        if operations
        else 0
    )


def calculate_population_plan(inventories, controls=None, classification=None):
    controls = controls or CollectionControls()
    inventories = tuple(InventoryEvidence.model_validate(i) for i in inventories)
    sources = {i.source: i for i in inventories}
    if len(inventories) != 2 or set(sources) != {"massive", "yfinance"}:
        raise ValueError("Both complete inventories required")
    for i in inventories:
        if (
            i.status != "COMPLETE"
            or i.count_kind != "exact"
            or i.duplicates
            or i.errors
            or i.unique_rows != i.raw_rows
            or sum(p.rows for p in i.pages) != i.raw_rows
            or not i.pages
            or not i.pages[-1].terminal
            or any(p.terminal for p in i.pages[:-1])
        ):
            raise ValueError("Incomplete inventory cannot supply a population upper bound")
    if (
        sources["massive"].query != "active_us_stocks"
        or sources["yfinance"].query != "us_technology_discovery"
    ):
        raise ValueError("Unsupported inventory scope")
    if sources["yfinance"].reported_total != sources["yfinance"].unique_rows:
        raise ValueError("Discovery total is not reconciled")
    n = sources["massive"].unique_rows  # includes exclusions/unknowns: safe upper bound
    t = n
    if classification is not None:
        c = classification
        if (
            c.reference_rows != n
            or c.excluded_listings + c.potentially_supported != n
            or c.unknown
            or c.conflicting
            or c.unmatched
            or c.ambiguous
            or c.identity_conflicts
            or c.technology + c.non_technology != c.potentially_supported
            or c.resolved != c.potentially_supported
            or c.stable_id_matches + c.corroborated_mapping_matches != c.resolved
            or c.distinct_technology_securities + c.duplicate_technology_listings != c.technology
            or c.method == "not_measured"
        ):
            raise ValueError("Incomplete classification cannot shrink the history bound")
        if c.profile_measurements is not None:
            m = c.profile_measurements
            if (
                m.failed
                or m.not_attempted
                or m.succeeded != m.attempted
                or m.attempted != c.potentially_supported
            ):
                raise ValueError("Incomplete profile measurements cannot shrink the history bound")
        t = c.distinct_technology_securities
    windows = len(CRITERIA.selection_policy.requirements())
    massive_per_window = controls.massive_pages_per_operation * (controls.massive.retries + 1)
    cold = {
        "massive_inventory": len(sources["massive"].pages) * (controls.massive.retries + 1),
        "yahoo_inventory_including_auth": yahoo_http_upper(
            len(sources["yfinance"].pages), controls
        ),
        "identity_resolution_http": 0,
        "classification_including_auth": yahoo_http_upper(n, controls),
        "mandatory_candidate_history": t * windows * massive_per_window,
        "mandatory_benchmark_history": windows * massive_per_window,
    }
    warm = {
        "mandatory_candidate_history": cold["mandatory_candidate_history"],
        "mandatory_benchmark_history": cold["mandatory_benchmark_history"],
        "metadata_http": 0,
        "identity_resolution_http": 0,
    }
    cold_source = {
        "massive": cold["massive_inventory"]
        + cold["mandatory_candidate_history"]
        + cold["mandatory_benchmark_history"],
        "yfinance": cold["yahoo_inventory_including_auth"] + cold["classification_including_auth"],
    }
    warm_source = {"massive": sum(warm.values()), "yfinance": 0}
    total = {p: cold_source[p] + warm_source[p] for p in cold_source}
    massive_wait = max(
        60 / controls.massive.requests_per_minute, controls.massive.max_retry_delay_seconds
    )
    yahoo_wait = max(60 / controls.yahoo_requests_per_minute, controls.yahoo_max_cooldown_seconds)
    enrichment = {"massive": cold["massive_inventory"], "yfinance": cold_source["yfinance"]}
    return PopulationPlan(
        inventories=inventories,
        classification=classification,
        history_candidate_upper_bound=t,
        enrichment_source_http_upper=enrichment,
        enrichment_http_upper=sum(enrichment.values()),
        enrichment_network_wait_upper_seconds=enrichment["massive"]
        * (massive_wait + controls.massive.timeout_seconds)
        + enrichment["yfinance"] * (yahoo_wait + controls.yahoo_timeout_seconds),
        inventory_sha256=digest([i.model_dump(mode="json") for i in inventories]),
        requirements_sha256=requirements_hash(CRITERIA.selection_policy),
        controls=controls,
        candidate_upper_bound=n,
        history_windows_per_candidate=windows,
        cold=cold,
        warm_metadata_fresh=warm,
        cold_source_http_upper=cold_source,
        warm_source_http_upper=warm_source,
        total_source_http_upper=total,
        total_http_upper=sum(total.values()),
        network_wait_upper_seconds=total["massive"]
        * (massive_wait + controls.massive.timeout_seconds)
        + total["yfinance"] * (yahoo_wait + controls.yahoo_timeout_seconds),
        assumptions=(
            "ALL_REFERENCE_ROWS_ARE_CANDIDATES_FOR_UPPER_BOUND",
            "ALL_MANDATORY_WINDOWS_RESOLVED_INDEPENDENTLY",
            "YAHOO_AUTH_REFRESH_RETRIES_WITHIN_ENFORCED_OPERATION_CAP",
            "MASSIVE_PAGINATION_AND_RETRIES_HAVE_ENFORCED_CAPS",
            "WARM_REFRESHES_ALL_CANDIDATES_AND_BENCHMARK",
            "METADATA_EXPIRED_REFRESH_REQUIRES_ANOTHER_COLD_BUDGET",
            "CPU_AND_FILESYSTEM_TIME_MEASURED_AGAINST_OVERALL_DEADLINE",
        ),
    )


def plan_within_limits(plan, *, overall, sources, wall_seconds, enrichment=False):
    per_source = plan.enrichment_source_http_upper if enrichment else plan.total_source_http_upper
    total = plan.enrichment_http_upper if enrichment else plan.total_http_upper
    seconds = (
        plan.enrichment_network_wait_upper_seconds
        if enrichment
        else plan.network_wait_upper_seconds
    )
    return (
        total <= overall
        and all(per_source[p] <= sources.get(p, 0) for p in per_source)
        and per_source["massive"] <= plan.controls.massive.max_requests
        and seconds <= wall_seconds
    )
