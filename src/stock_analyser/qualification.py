"""Versioned aggregate qualification evidence. No provider I/O or summary overrides.

Hashes identify observations; they cannot reconstruct the discarded vendor population.
A valid report is evidence, not cryptographic attestation of its author's assertions.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, ValidationError

from stock_analyser.universe import SelectionPolicy
from stock_analyser.universe_spike import POLICY, digest

Count = Annotated[int, Field(strict=True, ge=0)]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,99}$")]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


SCOPE = digest(
    {
        "market": "US",
        "sector": "technology",
        "use": "internal_qualification",
        "retention": "local_replay",
        "report": "aggregate_only",
    }
)


class QualificationCriteria(Evidence):
    version: Literal["us-technology-qualification-1"] = "us-technology-qualification-1"
    metadata_ttl_hours: Literal[24] = 24
    rights_ttl_days: Literal[30] = 30
    minimum_selected: Literal[10] = 10
    retention_days: Literal[365] = 365
    scope_sha256: Hash = SCOPE
    policy_sha256: Hash = digest(POLICY)
    selection_policy: SelectionPolicy = SelectionPolicy()
    required_sources: tuple[Literal["massive", "yfinance"], ...] = ("massive", "yfinance")
    required_rights: tuple[tuple[str, str, str], ...] = (
        ("massive", "reference", "collect"),
        ("massive", "reference", "retain"),
        ("massive", "reference", "derive_internal"),
        ("yfinance", "classification", "collect"),
        ("yfinance", "classification", "retain"),
        ("yfinance", "classification", "derive_internal"),
        ("massive", "prices", "collect"),
        ("massive", "prices", "retain"),
        ("massive", "prices", "derive_internal"),
    )


CRITERIA = QualificationCriteria()


def model_hash(model: BaseModel) -> str:
    return digest(model.model_dump(mode="json"))


class PageEvidence(Evidence):
    rows: Count
    sha256: Hash
    retrieved_at: AwareDatetime
    terminal: bool


class InventoryEvidence(Evidence):
    source: Literal["massive", "yfinance"]
    query: Literal["active_us_stocks", "us_technology_discovery"]
    status: Literal["COMPLETE", "TRUNCATED", "FAILED", "NOT_RUN"]
    reason: Code
    method: Literal["terminal_page", "reported_total"]
    reported_total: Count | None = None
    raw_rows: Count
    unique_rows: Count
    duplicates: Count
    errors: Count
    count_kind: Literal["exact", "lower_bound"]
    pages: tuple[PageEvidence, ...]
    transactionally_consistent: Literal[False] = False


class CoverageEvidence(Evidence):
    # Reference rows partition into measured exclusions and potentially supported listings.
    reference_rows: Count
    excluded_listings: Count
    potentially_supported: Count
    technology: Count
    non_technology: Count
    unknown: Count
    conflicting: Count
    as_of: AwareDatetime
    taxonomy: Literal["yahoo-sector-map-1"]
    method: Literal["all_reference_profiles", "complete_stable_id_export", "not_measured"]
    resolved: Count
    ambiguous: Count
    identity_conflicts: Count
    unmatched: Count
    stable_id_matches: Count
    corroborated_mapping_matches: Count
    distinct_technology_securities: Count
    duplicate_technology_listings: Count


class HistoryEvidence(Evidence):
    candidates: Count
    qualified: Count
    measured_excluded: Count
    failed: Count
    not_attempted: Count
    selected: Count
    requirements_sha256: Hash
    selection_implementation: Literal["sector-selection-2"]
    adjustment: Literal["raw"]
    sessions: Literal[60]
    exclusion_reasons: dict[Code, Count]
    blocker_reasons: dict[Code, Count]
    reason_counts_overlap: Literal[True] = True
    # These identify local rights-permitted detail; aggregates alone cannot replay selection.
    selection_inputs_sha256: Hash | None = None
    benchmark_measured: bool
    calendars_and_feeds_verified: bool


class RightEvidence(Evidence):
    provider: Literal["massive", "yfinance"]
    dataset: Literal["reference", "classification", "prices"]
    operation: Literal["collect", "retain", "derive_internal"]
    disposition: Literal["permitted", "prohibited", "unknown"]
    # Reference IDs are local reviewed manifest identifiers, never arbitrary provider URLs.
    reference: Literal["massive-market-data-2025-08-28", "yahoo-terms", "written_confirmation"]
    document_sha256: Hash | None
    reviewed_at: AwareDatetime
    valid_until: AwareDatetime
    account_scope_sha256: Hash
    intended_scope_sha256: Hash
    retention_days: Count
    required_retention_days: Count
    conditions: tuple[Code, ...]


class BudgetEvidence(Evidence):
    request_ceiling: Count
    per_source_ceilings: dict[Literal["massive", "yfinance"], Count]
    http_attempts: dict[Literal["massive", "yfinance"], Count | None]
    sdk_calls: Count
    retries: Count
    cooldown_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    elapsed_seconds: Annotated[float, Field(ge=0, allow_inf_nan=False)]
    wall_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    concurrency: Literal[1]
    conservative_http_upper_bound: Count | None
    inventory_complete_for_estimate: bool
    cold_measured: bool
    warm_measured: bool
    stop_reason: Code


class SourceVersion(Evidence):
    package_version: Annotated[str, Field(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
    commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")] | None
    dirty: bool | None


class GateOutcome(Evidence):
    status: Literal["PASS", "FAIL"]
    reasons: tuple[Code, ...]


class DerivedGate(Evidence):
    status: Literal["PASS", "FAIL"]
    full_sector_accepted: bool
    blockers: tuple[Code, ...]
    gates: dict[
        Literal[
            "contract",
            "freshness",
            "inventory",
            "classification",
            "identity",
            "history_liquidity",
            "rights",
            "workload",
            "origin",
        ],
        GateOutcome,
    ]


class QualificationReport(Evidence):
    schema_version: Literal["2"]
    run_id: UUID
    origin: Literal["live", "synthetic", "preflight"]
    started_at: AwareDatetime
    finished_at: AwareDatetime
    as_of: AwareDatetime
    source_version: SourceVersion
    implementation_sha256: Hash
    lock_sha256: Hash
    policy_sha256: Hash
    criteria: QualificationCriteria
    criteria_sha256: Hash
    selection_policy_sha256: Hash
    scope_sha256: Hash
    inventories: tuple[InventoryEvidence, ...]
    coverage: CoverageEvidence
    history: HistoryEvidence
    budget: BudgetEvidence
    rights: tuple[RightEvidence, ...]
    # Derived summaries are checked if supplied, never used as authority.
    gate: DerivedGate | None = None


def requirements_hash(policy: SelectionPolicy) -> str:
    return digest([r.model_dump(mode="json") for r in policy.requirements()])


def evaluate_qualification(report, criteria=CRITERIA, *, now: datetime, live=False) -> dict:
    """Recompute every gate with the caller's clock and supported criteria; fail closed."""
    if not isinstance(criteria, QualificationCriteria) or criteria != CRITERIA:
        return {
            "status": "FAIL",
            "full_sector_accepted": False,
            "blockers": ["UNSUPPORTED_QUALIFICATION_CRITERIA"],
            "gates": {},
        }
    gates = {}

    def gate(name, passed, reason):
        gates[name] = {
            "status": "PASS" if passed else "FAIL",
            "reasons": [] if passed else [reason],
        }

    try:
        r = QualificationReport.model_validate(
            report.model_dump(mode="json") if isinstance(report, QualificationReport) else report
        )
        if not isinstance(now, datetime) or now.tzinfo is None:
            raise ValueError("Clock must be timezone aware")
    except (ValidationError, ValueError, TypeError):
        return {
            "status": "FAIL",
            "full_sector_accepted": False,
            "blockers": ["INVALID_QUALIFICATION_EVIDENCE"],
            "gates": {},
        }
    supported = criteria == CRITERIA and r.criteria == criteria
    try:
        compatible_source = (
            r.implementation_sha256 == implementation_hash()
            and r.lock_sha256 == file_hash(Path(__file__).resolve().parents[2] / "uv.lock")
        )
    except OSError:
        compatible_source = False
    gate(
        "contract",
        supported
        and r.criteria_sha256 == model_hash(criteria)
        and compatible_source
        and r.scope_sha256 == criteria.scope_sha256
        and r.policy_sha256 == criteria.policy_sha256
        and r.selection_policy_sha256 == model_hash(criteria.selection_policy)
        and r.history.requirements_sha256 == requirements_hash(criteria.selection_policy),
        "INCOMPATIBLE_CRITERIA_OR_POLICY",
    )
    earliest = now - timedelta(hours=criteria.metadata_ttl_hours)
    times = [
        r.started_at,
        r.finished_at,
        r.as_of,
        r.coverage.as_of,
        *(p.retrieved_at for i in r.inventories for p in i.pages),
    ]
    gate(
        "freshness",
        all(earliest <= t <= now for t in times)
        and r.started_at <= r.as_of <= r.finished_at
        and all(r.started_at <= t <= r.finished_at for t in times[3:]),
        "STALE_FUTURE_OR_CONFLICTING_TIMESTAMPS",
    )
    sources = {i.source: i for i in r.inventories}
    complete = len(sources) == len(r.inventories) and set(sources) == set(criteria.required_sources)
    for i in r.inventories:
        complete &= (
            i.status == "COMPLETE"
            and i.count_kind == "exact"
            and i.errors == 0
            and i.duplicates == 0
            and i.raw_rows == i.unique_rows
            and sum(p.rows for p in i.pages) == i.raw_rows
            and bool(i.pages)
        )
        if i.method == "terminal_page":
            complete &= (
                i.source == "massive"
                and i.query == "active_us_stocks"
                and bool(i.pages)
                and i.pages[-1].terminal
                and not any(p.terminal for p in i.pages[:-1])
            )
        else:
            complete &= (
                i.source == "yfinance"
                and i.query == "us_technology_discovery"
                and i.reported_total == i.unique_rows
                and bool(i.pages)
                and i.pages[-1].terminal
                and not any(p.terminal for p in i.pages[:-1])
            )
    gate("inventory", complete, "INVENTORY_INCOMPLETE_OR_INCONSISTENT")
    c = r.coverage
    denominator = (
        c.reference_rows == c.excluded_listings + c.potentially_supported
        and c.reference_rows == (sources["massive"].unique_rows if "massive" in sources else -1)
        and c.potentially_supported > 0
    )
    gate(
        "classification",
        denominator
        and c.method != "not_measured"
        and c.potentially_supported == c.technology + c.non_technology + c.unknown + c.conflicting
        and c.unknown == c.conflicting == 0,
        "CLASSIFICATION_INCOMPLETE_OR_CONFLICTING",
    )
    gate(
        "identity",
        denominator
        and c.potentially_supported == c.resolved + c.ambiguous + c.identity_conflicts + c.unmatched
        and c.ambiguous == c.identity_conflicts == c.unmatched == 0
        and c.resolved == c.stable_id_matches + c.corroborated_mapping_matches
        and c.technology == c.distinct_technology_securities + c.duplicate_technology_listings,
        "IDENTITY_UNRESOLVED_OR_INCONSISTENT",
    )
    h = r.history
    gate(
        "history_liquidity",
        h.candidates == c.distinct_technology_securities
        and h.candidates == h.qualified + h.measured_excluded + h.failed + h.not_attempted
        and h.failed == h.not_attempted == 0
        and not h.blocker_reasons
        and h.selected == min(criteria.minimum_selected, h.qualified)
        and h.selected >= criteria.minimum_selected
        and bool(h.selection_inputs_sha256)
        and (
            sum(h.exclusion_reasons.values()) >= h.measured_excluded
            and all(0 < count <= h.measured_excluded for count in h.exclusion_reasons.values())
        )
        and h.benchmark_measured
        and h.calendars_and_feeds_verified,
        "HISTORY_LIQUIDITY_OR_SELECTION_INCOMPLETE",
    )
    rights = {(x.provider, x.dataset, x.operation): x for x in r.rights}
    valid_rights = len(rights) == len(r.rights) and set(rights) == set(criteria.required_rights)
    for x in r.rights:
        valid_rights &= (
            x.disposition == "permitted"
            and x.document_sha256 is not None
            and not x.conditions
            and now - timedelta(days=criteria.rights_ttl_days) <= x.reviewed_at <= now
            and x.reviewed_at <= r.started_at
            and now <= x.valid_until
            and x.valid_until >= x.reviewed_at
            and x.intended_scope_sha256 == r.scope_sha256
            and x.required_retention_days == criteria.retention_days
            and x.retention_days >= x.required_retention_days
        )
    gate("rights", valid_rights, "RIGHTS_UNKNOWN_DENIED_STALE_OR_WRONG_SCOPE")
    b = r.budget
    attempts = list(b.http_attempts.values())
    gate(
        "workload",
        set(b.http_attempts) == set(criteria.required_sources)
        and set(b.per_source_ceilings) == set(criteria.required_sources)
        and all(v is not None for v in attempts)
        and all(v is not None and v <= b.per_source_ceilings[s] for s, v in b.http_attempts.items())
        and sum(v or 0 for v in attempts) <= b.request_ceiling
        and b.conservative_http_upper_bound is not None
        and sum(v or 0 for v in attempts) <= b.conservative_http_upper_bound <= b.request_ceiling
        and b.elapsed_seconds <= b.wall_seconds
        and abs((r.finished_at - r.started_at).total_seconds() - b.elapsed_seconds) <= 1
        and b.inventory_complete_for_estimate
        and b.cold_measured
        and b.warm_measured
        and b.stop_reason == "COMPLETE",
        "WORKLOAD_UNVERIFIED_OR_EXHAUSTED",
    )
    gate(
        "origin",
        r.origin == "live" if live else r.origin in ("live", "synthetic"),
        "LIVE_EVIDENCE_REQUIRED",
    )
    blockers = [reason for g in gates.values() for reason in g["reasons"]]
    result = {
        "status": "FAIL" if blockers else "PASS",
        "full_sector_accepted": not blockers,
        "blockers": blockers,
        "gates": gates,
    }
    if r.gate is not None and r.gate.model_dump(mode="json") != result:
        result = {
            **result,
            "status": "FAIL",
            "full_sector_accepted": False,
            "blockers": [*blockers, "CONFLICTING_GATE_SUMMARY"],
        }
    return result


def write_report(
    output: Path, report: QualificationReport, *, now: datetime, _reserved=False
) -> dict:
    """Refuse overwrite, validate allowlist, then atomically publish aggregate JSON."""
    output.mkdir(parents=True, exist_ok=_reserved)
    data = report.model_dump(mode="json")
    data["gate"] = evaluate_qualification(report, now=now, live=report.origin != "synthetic")
    temporary = output / ".findings.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(data, indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.link(temporary, output / "findings.json")
    temporary.unlink()
    return data


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def implementation_hash():
    root = Path(__file__).resolve().parents[2]
    paths = (
        "qualification.py",
        "qualification_workflow.py",
        "qualification_budget.py",
        "universe.py",
        "universe_spike.py",
        "universe_policy.py",
        "massive.py",
        "calendars.py",
        "providers.py",
        "routing.py",
    )
    return digest({p: file_hash(root / "src/stock_analyser" / p) for p in paths})
