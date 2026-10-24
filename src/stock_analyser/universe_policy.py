"""Immutable v1 reference eligibility and taxonomy shared by qualification and selection."""

from dataclasses import dataclass


@dataclass(frozen=True)
class UniverseRules:
    version: str = "us-technology-proposed-1"
    taxonomy_version: str = "yahoo-sector-map-1"
    taxonomy_source: str = "Yahoo sector"
    source_sector: str = "Technology"
    mapped_sector: str = "technology"
    other_known_sectors: tuple[str, ...] = (
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
    )
    eligible_type: str = "CS"
    listing_order: tuple[str, ...] = ("XNAS", "XNYS", "ARCX", "XASE", "BATS")
    currency: str = "USD"


TECHNOLOGY_RULES_V1 = UniverseRules()


def reference_exclusions(row: dict, rules: UniverseRules = TECHNOLOGY_RULES_V1) -> list[str]:
    checks = {
        "INACTIVE_OR_UNKNOWN": row.get("active") is True,
        "NOT_US_STOCK": row.get("market") == "stocks" and row.get("locale") == "us",
        "NON_COMMON_OR_UNKNOWN_TYPE": row.get("type") == rules.eligible_type,
        "UNSUPPORTED_OR_UNKNOWN_VENUE": row.get("primary_exchange") in rules.listing_order,
        f"NON_{rules.currency}_OR_UNKNOWN_CURRENCY": row.get("currency_name", "").upper()
        == rules.currency
        if isinstance(row.get("currency_name"), str)
        else False,
        "UNKNOWN_SECURITY_IDENTITY": bool((row.get("share_class_figi") or "").strip()),
        "UNKNOWN_LISTING_IDENTITY": bool((row.get("composite_figi") or "").strip()),
    }
    return [reason for reason, passed in checks.items() if not passed]
