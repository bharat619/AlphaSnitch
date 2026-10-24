"""Bounded, opt-in coverage qualification; never a production universe provider.

Only aggregate findings and hashes of allowlisted metadata are retained. Enumeration
of a vendor query is deliberately distinct from full-sector acceptance.
"""

import hashlib
import json
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from stock_analyser.massive import MassiveClient
from stock_analyser.version import source_version

POLICY = {
    "version": "us-technology-proposed-1",
    "status": "evaluation_hypothesis",
    "market": "US",
    "sector": "technology",
    "taxonomy": {"source": "Yahoo sector", "Technology": "technology"},
    "taxonomy_version": "yahoo-sector-map-1",
    "other_known_sectors": [
        "Basic Materials",
        "Communication Services",
        "Consumer Cyclical",
        "Consumer Defensive",
        "Energy",
        "Financial Services",
        "Healthcare",
        "Industrials",
        "Real Estate",
        "Utilities",
    ],
    "eligible_type": "CS",
    "venues": ["XNAS", "XNYS", "ARCX", "XASE", "BATS"],
    "currency": "USD",
    "security_key": "share_class_figi; missing or conflicting identity blocks eligibility",
    "listing_order": ["XNAS", "XNYS", "ARCX", "XASE", "BATS"],
    "listing_tie_breaker": "composite_figi, then provider ticker, ascending",
    "minimum_price_usd": "5",
    "minimum_average_traded_value_usd": "10000000",
    "traded_value_sessions": 60,
    "traded_value": "mean(unadjusted regular-session close * unadjusted share volume)",
    "minimum_completed_daily_bars": 260,
    "mandatory_history": "max(260, daily minimum+warmup); each mandatory weekly window separately",
    "rank": "average traded value descending, immutable instrument UUID ascending",
    "selection_count": 10,
    "metadata_ttl_hours": 24,
    "full_sector_accepted": False,
}

REFERENCE_FIELDS = (
    "ticker",
    "active",
    "market",
    "locale",
    "type",
    "primary_exchange",
    "currency_name",
    "share_class_figi",
    "composite_figi",
    "last_updated_utc",
)
DISCOVERY_FIELDS = ("symbol", "exchange", "quoteType", "sector", "industry", "currency")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def project(rows: list, fields: tuple[str, ...], key: str) -> list[dict]:
    result = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(key), str) or not row[key].strip():
            raise ValueError("Invalid identifier")
        item = {name: row.get(name) for name in fields}
        if any(
            value is not None and not isinstance(value, bool if name == "active" else str)
            for name, value in item.items()
        ):
            raise ValueError("Invalid metadata")
        result.append(item)
    return result


@dataclass
class Enumeration:
    source: str
    query: dict
    rows: list[dict] = field(default_factory=list)
    pages: list[dict] = field(default_factory=list)
    status: str = "FAILED"
    reason: str = "NO_RESPONSE"
    reported_total: int | None = None
    attempts: int = 0

    def evidence(self) -> dict:
        return {
            "source": self.source,
            "query": self.query,
            "status": self.status,
            "reason": self.reason,
            "reported_total": self.reported_total,
            "unique_rows": len(self.rows),
            "pages": self.pages,
            "sdk_calls_attempted": self.attempts if self.source == "yfinance" else None,
            "metadata_sha256": digest(self.rows),
            "scope": "provider query at retrieval time; not full-sector coverage",
        }


def enumerate_massive(client: MassiveClient, *, max_pages: int = 40) -> Enumeration:
    if not 1 <= max_pages <= 100:
        raise ValueError("Invalid page bound")
    query = {
        "market": "stocks",
        "locale": "us",
        "active": "true",
        "limit": 1000,
        "sort": "ticker",
        "order": "asc",
    }
    result = Enumeration("massive", query)
    seen = set()
    try:
        for page in client.pages("/v3/reference/tickers", query):
            rows = page.get("results")
            if not isinstance(rows, list) or len(rows) > 1000:
                raise ValueError("Invalid page")
            rows = project(rows, REFERENCE_FIELDS, "ticker")
            result.pages.append(
                {
                    "rows": len(rows),
                    "sha256": digest(rows),
                    "retrieved_at": datetime.now(UTC).isoformat(),
                    "has_next": page.get("next_url") is not None,
                }
            )
            for row in rows:
                if row["ticker"] in seen:
                    result.reason = "DUPLICATE_IDENTIFIER"
                    return result
                seen.add(row["ticker"])
                result.rows.append(row)
            if page.get("next_url") is None:
                result.status, result.reason = "COMPLETE", "TERMINAL_PAGE"
                return result
            if not rows:
                result.reason = "EMPTY_NONTERMINAL_PAGE"
                return result
            if len(result.pages) >= max_pages:
                result.status, result.reason = "TRUNCATED", "PAGE_LIMIT"
                return result
    except Exception:
        # No exception strings: upstream errors may contain credentials or URLs.
        result.reason = "RETRIEVAL_OR_SCHEMA_FAILURE"
    return result


def enumerate_yahoo(screen, *, max_pages: int = 40, page_size: int = 250) -> Enumeration:
    if not 1 <= max_pages <= 100 or not 1 <= page_size <= 250:
        raise ValueError("Invalid page bound")
    result = Enumeration(
        "yfinance",
        {
            "region": "us",
            "sector": "Technology",
            "size": page_size,
            "sortField": "ticker",
            "sortAsc": True,
        },
    )
    seen = set()
    offset = 0
    try:
        for _ in range(max_pages):
            result.attempts += 1
            page = screen(offset, page_size)
            total = page.get("total")
            rows = page.get("quotes")
            if (
                type(total) is not int
                or total < 0
                or page.get("start") != offset
                or not isinstance(rows, list)
                or len(rows) > page_size
                or page.get("count") != len(rows)
            ):
                raise ValueError("Invalid page")
            if result.reported_total is not None and total != result.reported_total:
                result.reason = "TOTAL_CHANGED_DURING_PAGINATION"
                return result
            result.reported_total = total
            rows = project(rows, DISCOVERY_FIELDS, "symbol")
            result.pages.append(
                {
                    "offset": offset,
                    "rows": len(rows),
                    "total": total,
                    "sha256": digest(rows),
                    "retrieved_at": datetime.now(UTC).isoformat(),
                }
            )
            for row in rows:
                if row["symbol"] in seen:
                    result.reason = "DUPLICATE_IDENTIFIER"
                    return result
                seen.add(row["symbol"])
                result.rows.append(row)
            offset += len(rows)
            if offset == total:
                result.status, result.reason = "COMPLETE", "TOTAL_RECONCILED"
                return result
            if offset > total or not rows:
                result.reason = "TOTAL_MISMATCH_OR_EMPTY_PAGE"
                return result
        result.status, result.reason = "TRUNCATED", "PAGE_LIMIT"
    except Exception:
        result.reason = "RETRIEVAL_OR_SCHEMA_FAILURE"
    return result


def reference_exclusions(row: dict) -> list[str]:
    checks = {
        "INACTIVE_OR_UNKNOWN": row.get("active") is True,
        "NOT_US_STOCK": row.get("market") == "stocks" and row.get("locale") == "us",
        "NON_COMMON_OR_UNKNOWN_TYPE": row.get("type") == POLICY["eligible_type"],
        "UNSUPPORTED_OR_UNKNOWN_VENUE": row.get("primary_exchange") in POLICY["venues"],
        f"NON_{POLICY['currency']}_OR_UNKNOWN_CURRENCY": row.get("currency_name", "").upper()
        == POLICY["currency"]
        if isinstance(row.get("currency_name"), str)
        else False,
        "UNKNOWN_SECURITY_IDENTITY": bool((row.get("share_class_figi") or "").strip()),
        "UNKNOWN_LISTING_IDENTITY": bool((row.get("composite_figi") or "").strip()),
    }
    return [reason for reason, passed in checks.items() if not passed]


def reconcile(reference: Enumeration, discovery: Enumeration) -> dict:
    exclusions = Counter()
    eligible = []
    identities = {}
    for row in reference.rows:
        identities.setdefault(row.get("composite_figi"), set()).add(
            (row.get("share_class_figi"), row.get("primary_exchange"), row.get("ticker"))
        )
    for row in reference.rows:
        reasons = reference_exclusions(row)
        if row.get("composite_figi") and len(identities[row["composite_figi"]]) > 1:
            reasons.append("CONFLICTING_LISTING_IDENTITY")
        exclusions.update(reasons)
        if not reasons:
            eligible.append(row)
    groups = {}
    for row in eligible:
        groups.setdefault(row["share_class_figi"], []).append(row)
    listings = [
        min(
            group,
            key=lambda row: (
                POLICY["listing_order"].index(row["primary_exchange"]),
                row["composite_figi"],
                row["ticker"],
            ),
        )
        for group in groups.values()
    ]
    reference_symbols = {row["ticker"] for row in reference.rows}
    eligible_symbol_to_security = {row["ticker"]: row["share_class_figi"] for row in eligible}
    discovered = {row["symbol"] for row in discovery.rows}
    discovered_securities = {
        eligible_symbol_to_security[symbol]
        for symbol in discovered & eligible_symbol_to_security.keys()
    }
    return {
        "reference_exclusions": dict(sorted(exclusions.items())),
        "exclusion_counts_overlap": True,
        "eligible_listings_before_dedup": len(eligible),
        "distinct_candidate_securities": len(listings),
        "duplicate_listings_removed": len(eligible) - len(listings),
        "discovery_exact_symbol_matches": len(discovered & reference_symbols),
        "discovery_unmatched_symbols": len(discovered - reference_symbols),
        "discovery_eligible_security_matches": len(discovered_securities),
        "eligible_securities_outside_discovery": len(groups.keys() - discovered_securities),
        "classification_completeness": "UNVERIFIED; absence from discovery is not non-Technology",
        "symbol_mapping": "Exact only; punctuation, rename and venue differences need verification",
    }


def estimate_workload(
    *, reference_pages: int, discovery_pages: int, candidate_securities: int, sector_candidates: int
) -> dict:
    """Logical calls, explicitly not measured SDK HTTP requests or entitlement claims."""
    if min(reference_pages, discovery_pages, candidate_securities, sector_candidates) < 0:
        raise ValueError("Workload inputs must be nonnegative")
    metadata = reference_pages + discovery_pages + candidate_securities
    prices = sector_candidates + 1  # one daily union per candidate + SPY benchmark
    selected = POLICY["selection_count"]
    research = 2 * (selected + 1) + selected  # splits/dividends + benchmark, selected news
    return {
        "kind": "estimate_only",
        "unit": "logical provider calls; SDK HTTP amplification unknown",
        "inputs": {
            "reference_pages": reference_pages,
            "discovery_pages": discovery_pages,
            "candidate_securities": candidate_securities,
            "sector_candidates": sector_candidates,
            "selected": selected,
            "benchmark": 1,
            "profile_calls_per_candidate": 1,
            "price_pages_per_candidate": 1,
            "action_pages_per_selected_and_benchmark": 2,
            "news_pages_per_selected": 1,
        },
        "cold_start": metadata + prices + research,
        "warm_next_session_metadata_fresh": prices + research,
        "warm_after_metadata_ttl": metadata + prices + research,
        "warm_exact_snapshot_replay": 0,
        "cold_upper_scenario_two_retries_and_three_pages": 9 * (metadata + prices + research),
        "assumptions": [
            "All candidate securities need classification, not only discovery matches",
            f"Every sector candidate refreshes for ranking, not only the selected {selected}",
            f"{POLICY['minimum_completed_daily_bars']} daily bars plus independently resolved "
            "mandatory weekly warmup",
            "One daily union-history call; valid only if chosen source covers the window",
            f"Actions/news estimate assumes {selected} selections; zero forward-earnings coverage",
            "No failed calls, retries, paging, or SDK authentication in base estimates",
            f"Metadata TTL {POLICY['metadata_ttl_hours']}h; "
            "warm price refresh includes five overlapping sessions",
        ],
    }


def run_spike(client, screen, profile, output: Path, *, max_pages=40, profile_limit=10) -> dict:
    if not 1 <= max_pages <= 100 or not 0 <= profile_limit <= 25:
        raise ValueError("Invalid probe bounds")
    # Refuse overwrite so earlier measurements remain separate immutable observations.
    output.mkdir(parents=True, exist_ok=False)
    started = datetime.now(UTC).isoformat()
    implementation_sha256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    begin = time.monotonic()
    reference = enumerate_massive(client, max_pages=max_pages)
    discovery = enumerate_yahoo(screen, max_pages=max_pages)
    profiles = {
        "attempted": 0,
        "classified_technology": 0,
        "other_sector": 0,
        "unknown_or_failed": 0,
        "scope": "alphabetical bounded sample, not coverage proof",
        "observations": [],
    }
    for row in sorted(discovery.rows, key=lambda row: row["symbol"])[:profile_limit]:
        profiles["attempted"] += 1
        try:
            item = project([profile(row["symbol"])], DISCOVERY_FIELDS, "symbol")[0]
            if item["symbol"] != row["symbol"]:
                raise ValueError("Profile identity mismatch")
            sector = item["sector"]
            key = (
                "classified_technology"
                if sector == "Technology"
                else "other_sector"
                if sector in POLICY["other_known_sectors"]
                else "unknown_or_failed"
            )
            profiles[key] += 1
            profiles["observations"].append(
                {
                    "sha256": digest(item),
                    "classification": key,
                    "retrieved_at": datetime.now(UTC).isoformat(),
                }
            )
        except Exception:
            profiles["unknown_or_failed"] += 1
    joined = reconcile(reference, discovery)
    report = {
        "schema_version": "1",
        "policy": POLICY,
        "policy_sha256": digest(POLICY),
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "bounds": {
            "max_pages_per_source": max_pages,
            "profile_limit": profile_limit,
            "massive": client.settings.model_dump(mode="json"),
        },
        "versions": {
            "yfinance": version("yfinance"),
            "source": source_version(),
            "implementation_sha256": implementation_sha256,
        },
        "measured": {
            "reference": reference.evidence(),
            "discovery": discovery.evidence(),
            "profiles": profiles,
            "reconciliation": joined,
            "elapsed_seconds": round(time.monotonic() - begin, 3),
            "massive_http_responses": client.observations,
            "yahoo_http_requests": None,
            "price_history_requests": 0,
        },
        "estimated_workload": estimate_workload(
            reference_pages=len(reference.pages),
            discovery_pages=len(discovery.pages),
            candidate_securities=joined["distinct_candidate_securities"],
            sector_candidates=len(discovery.rows),
        ),
        "estimate_input_status": "observed counts; lower bounds when enumeration failed/truncated",
        "gate": {
            "status": "FAIL",
            "full_sector_accepted": False,
            "blockers": [
                "CLASSIFICATION_COMPLETENESS_UNVERIFIED",
                "CROSS_SOURCE_IDENTITY_MAPPING_UNVERIFIED",
                "HISTORY_AND_LIQUIDITY_NOT_MEASURED",
                "RETENTION_RIGHTS_UNVERIFIED",
            ]
            + (["REFERENCE_ENUMERATION_INCOMPLETE"] if reference.status != "COMPLETE" else [])
            + (["DISCOVERY_ENUMERATION_INCOMPLETE"] if discovery.status != "COMPLETE" else []),
        },
        "retention": "Aggregate counts and allowlisted metadata hashes; no raw rows or cursors",
    }
    (output / "findings.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report
