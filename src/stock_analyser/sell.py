"""Read-only collection for every open holding in a frozen portfolio revision."""

from uuid import uuid4

from pydantic import AwareDatetime, Field

from stock_analyser.calendars import CutoffPolicy, ExchangeCalendarAdapter, HistoryResolver
from stock_analyser.contracts import (
    AnalysisRequest,
    Contract,
    HistoryRequirement,
    Instrument,
    MarketContext,
    OperationalStatus,
)
from stock_analyser.ledger import freeze_open_holdings
from stock_analyser.markets import MarketRegistry
from stock_analyser.persistence.records import RunRecord, SnapshotReference
from stock_analyser.version import source_version


class SellEvidence(Contract):
    """Explicit evidence configuration; holdings always come from the local ledger."""

    market: MarketContext
    requested_as_of: AwareDatetime
    history: tuple[HistoryRequirement, ...] = Field(min_length=1)
    cutoff_policy: CutoffPolicy = CutoffPolicy()
    histories: tuple[SnapshotReference, ...] = ()


def collect_sell(
    database,
    runtime,
    evidence,
    provider,
    *,
    history_resolver=None,
    progress=lambda message: None,
    provider_spec=None,
):
    MarketRegistry().resolve(evidence.market.market_id)
    if evidence.market.market_id != runtime.market:
        raise ValueError("Sell evidence market differs from active market")
    holdings, revision = freeze_open_holdings(database, runtime.market)
    instruments = [Instrument.model_validate(h["instrument"]) for h in holdings]
    market = evidence.market
    resolver = history_resolver or HistoryResolver(
        ExchangeCalendarAdapter(), evidence.cutoff_policy
    )
    progress(
        f"Sell selection: {len(holdings)} open holdings; "
        f"portfolio revision: {revision}; market: {runtime.market}"
    )
    progress("Collection only; no SELL/HOLD verdicts. Holdings and trades are unchanged by scans.")

    def request(ids):
        return AnalysisRequest(
            request_id=uuid4(),
            mode="sell",
            market_id=market.market_id,
            instrument_ids=ids,
            portfolio_revision=revision,
            requested_as_of=evidence.requested_as_of,
            strategy_version="collection-only-1",
            history=evidence.history,
        )

    children = []
    for instrument in instruments:
        # Resolution failures are also retained per holding, rather than aborting selection.
        try:
            plans = tuple(
                resolver.resolve(market, instrument.venue, h, evidence.requested_as_of)
                for h in evidence.history
            )
            reason = None
        except Exception:
            plans, reason = (), "History resolution unavailable for holding"
        child = RunRecord(
            run_id=uuid4(),
            request=request((instrument.instrument_id,)),
            market=market,
            configuration={},
            source_version=source_version(),
            component_versions={"sell_collection": "1"},
            history_resolutions=plans,
            status=OperationalStatus.FAILED if reason else OperationalStatus.NOT_RUN,
            reason=reason,
        )
        with database.write() as repo:
            repo.create_run(child)
        children.append((child, instrument))

    from stock_analyser.resume import CollectionJob, ResumePlan, execute_plan, provider_recipe

    if provider_spec and provider_spec.get("scenario") == "provider-failure" and instruments:
        provider_spec = {**provider_spec, "failure_symbols": [instruments[0].symbol]}
        if hasattr(provider, "failed_id"):
            provider.failed_id = instruments[0].instrument_id
    plan = ResumePlan(
        jobs=tuple(CollectionJob(run_id=c.run_id, instrument=i) for c, i in children),
        provider=provider_spec or provider_recipe(provider),
        stock_concurrency=runtime.stock_concurrency,
    )
    scope = {
        "selection_outcome": "OPEN_HOLDINGS" if holdings else "EMPTY_PORTFOLIO",
        "holdings": holdings,
        "selected_revision": revision,
        "observed_revision": revision,
        "portfolio_changed": False,
        "revision_check": "after collection; later changes are not tracked by this frozen report",
        "stock_concurrency": runtime.stock_concurrency,
        "child_run_ids": [str(c.run_id) for c, _ in children],
    }
    run = RunRecord(
        run_id=uuid4(),
        request=request(tuple(i.instrument_id for i in instruments)),
        market=market,
        configuration={"sell_collection": scope, "resume_plan": plan.model_dump(mode="json")},
        source_version=source_version(),
        component_versions={"sell_collection": "1"},
        status=OperationalStatus.NOT_RUN,
    )
    with database.write() as repo:
        repo.create_run(run)
    return execute_plan(
        database,
        runtime,
        run.run_id,
        provider if plan.provider["kind"] == "external" else None,
        progress=progress,
    )
