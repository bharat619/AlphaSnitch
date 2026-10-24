"""Read-only collection for every open holding in a frozen portfolio revision."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from uuid import uuid4

from pydantic import AwareDatetime, Field

from stock_analyser.buy import SharedProvider
from stock_analyser.calendars import CutoffPolicy, ExchangeCalendarAdapter, HistoryResolver
from stock_analyser.collection import CollectionService
from stock_analyser.contracts import (
    AnalysisRequest,
    Contract,
    HistoryRequirement,
    Instrument,
    MarketContext,
    OperationalStatus,
)
from stock_analyser.ledger import freeze_open_holdings, portfolio_revision
from stock_analyser.markets import MarketRegistry
from stock_analyser.persistence.records import RunRecord, SnapshotReference, StageRecord
from stock_analyser.reports import ReportService
from stock_analyser.version import source_version


class SellEvidence(Contract):
    """Explicit evidence configuration; holdings always come from the local ledger."""

    market: MarketContext
    requested_as_of: AwareDatetime
    history: tuple[HistoryRequirement, ...] = Field(min_length=1)
    cutoff_policy: CutoffPolicy = CutoffPolicy()
    histories: tuple[SnapshotReference, ...] = ()


def collect_sell(
    database, runtime, evidence, provider, *, history_resolver=None, progress=lambda message: None
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
    provider = SharedProvider(provider)
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

    def collect(child, instrument):
        if child.status != OperationalStatus.FAILED:
            try:
                CollectionService(database, runtime.snapshots_dir).collect(
                    child.run_id, instrument, provider
                )
            except Exception:
                with database.write() as repo:
                    repo.set_run_status(
                        child.run_id,
                        OperationalStatus.FAILED,
                        "Holding collection could not finish",
                    )
        with database.read() as repo:
            return repo.get_run(child.run_id), repo.list_snapshots(child.run_id)

    outcomes = []
    with ThreadPoolExecutor(max_workers=runtime.stock_concurrency) as executor:
        futures = {executor.submit(collect, child, i): i for child, i in children}
        for future in as_completed(futures):
            child, refs = future.result()
            outcomes.append((child, refs, futures[future]))
            progress(
                f"Collection {len(outcomes)}/{len(instruments)}: "
                f"{futures[future].symbol} {child.status.value}"
            )
    observed_revision = portfolio_revision(database)
    changed = observed_revision != revision
    failures = sum(child.status != OperationalStatus.SUCCEEDED for child, _, _ in outcomes)
    status = OperationalStatus.PARTIAL if failures else OperationalStatus.SUCCEEDED
    reason = (
        "EMPTY_PORTFOLIO: no positive-quantity holdings in selected market"
        if not holdings
        else f"{len(holdings)} open holdings; {failures} collection failures"
    )
    reason += (
        "; portfolio changed after selection"
        if changed
        else "; portfolio revision unchanged at completion check"
    )
    scope = {
        "selection_outcome": "OPEN_HOLDINGS" if holdings else "EMPTY_PORTFOLIO",
        "holdings": holdings,
        "selected_revision": revision,
        "observed_revision": observed_revision,
        "portfolio_changed": changed,
        "revision_check": "after collection; later changes are not tracked by this frozen report",
        "stock_concurrency": runtime.stock_concurrency,
        "child_run_ids": [str(c.run_id) for c, _ in children],
    }
    run = RunRecord(
        run_id=uuid4(),
        request=request(tuple(i.instrument_id for i in instruments)),
        market=market,
        configuration={"sell_collection": scope},
        source_version=source_version(),
        component_versions={"sell_collection": "1"},
        status=status,
        reason=reason,
    )
    with database.write() as repo:
        repo.create_run(run)
        for child, refs, instrument in outcomes:
            for ref in refs:
                repo.attach_snapshot(run.run_id, ref)
            repo.put_stage(
                StageRecord(
                    run_id=run.run_id,
                    name="collection",
                    instrument_id=instrument.instrument_id,
                    status=child.status,
                    reason=child.reason,
                )
            )
    artifacts = ReportService(database, runtime.reports_dir).generate(run.run_id)
    progress(reason)
    return run.run_id, status, artifacts
