"""Choose once from approved capabilities, before any provider network call."""

from datetime import date
from typing import Literal, Self

from pydantic import model_validator

from stock_analyser.calendars import ResolvedHistory
from stock_analyser.contracts import Contract, MarketContext, ProviderRoute, Text


class VerifiedCoverage(Contract):
    """Operator-verified inclusive entitlement window, not an advertised limit."""

    start: date
    end: date
    reference: Text

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.start > self.end:
            raise ValueError("Verified coverage is reversed")
        return self


class RoutingSettings(Contract):
    massive_coverage: VerifiedCoverage | None = None


class PriceRoutingDecision(Contract):
    route: ProviderRoute | None
    reason: Text
    mandatory_history: tuple[ResolvedHistory, ...]
    massive_coverage: VerifiedCoverage | None
    policy_version: Literal["history-routing-1"] = "history-routing-1"


def select_price_route(
    market: MarketContext,
    resolutions: tuple[ResolvedHistory, ...],
    settings: RoutingSettings | None = None,
) -> PriceRoutingDecision:
    """Pass stock AND benchmark plans to bind their history to the same source.

    Optional lookbacks cannot trigger switching. An approved unbounded capability
    is only permission to attempt collection; actual history is validated later.
    """
    settings = settings or RoutingSettings()
    mandatory = tuple(p for p in resolutions if p.requirement.mandatory)
    if any(
        p.market_id != market.market_id
        or p.market_configuration_version != market.configuration_version
        or p.timezone != market.timezone
        or p.calendar not in market.calendars
        for p in resolutions
    ):
        raise ValueError("Routing plans differ from the configured market")

    def decision(route, reason):
        return PriceRoutingDecision(
            route=route,
            reason=reason,
            mandatory_history=mandatory,
            massive_coverage=settings.massive_coverage,
        )

    if not mandatory or any(len(p.periods) < p.requirement.required_bars for p in mandatory):
        return decision(None, "UNASSESSABLE: mandatory calendar history could not be resolved")

    failures = []
    for role, provider, adjustments in (
        ("prices", "massive", {"raw", "split"}),
        ("long_history_prices", "yfinance", {"split"}),
    ):
        routes = [r for r in market.routes if r.role == role and r.provider == provider]
        capabilities = [c for c in market.capabilities if c.provider == provider]
        if not routes or not capabilities:
            failures.append(f"{provider}: no approved route")
            continue
        capability = capabilities[0]
        if any(
            not getattr(capability, f"{p.requirement.timeframe}_prices")
            or p.requirement.adjustment not in adjustments
            or (
                capability.max_history_days is not None
                and (p.requested_as_of.date() - p.history_start).days > capability.max_history_days
            )
            for p in mandatory
        ):
            failures.append(f"{provider}: mandatory history exceeds supported capability")
            continue
        if provider == "massive" and (
            settings.massive_coverage is None
            or any(
                p.history_start < settings.massive_coverage.start
                or p.session_date > settings.massive_coverage.end
                for p in mandatory
            )
        ):
            failures.append("massive: verified coverage does not cover mandatory history")
            continue
        reason = (
            "Massive verified coverage meets all mandatory history requirements"
            if provider == "massive"
            else "; ".join(failures)
            + "; approved yfinance history requires per-instrument validation"
        )
        return decision(routes[0], reason)
    return decision(None, "UNASSESSABLE: " + "; ".join(failures))
