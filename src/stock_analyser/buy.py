"""Bounded sector collection from explicitly supplied prototype universe evidence."""

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from threading import Lock
from uuid import uuid4

from stock_analyser.cache import load_snapshot
from stock_analyser.collection import CollectionService, RawPriceResponse
from stock_analyser.contracts import (
    AnalysisRequest,
    Instrument,
    OperationalStatus,
    Security,
    SymbolMapping,
)
from stock_analyser.persistence.records import RunRecord, StageRecord
from stock_analyser.reports import ReportService
from stock_analyser.universe import save_universe
from stock_analyser.version import source_version


class FrozenPriceProvider:
    """Replay verified raw inputs without making a provider request."""

    def __init__(self, histories):
        self.histories = histories

    def fetch(self, instrument, market, resolution):
        for reference in self.histories:
            meta = reference.metadata
            if (
                meta.instrument == instrument
                and meta.timeframe == resolution.requirement.timeframe
                and meta.adjustment == resolution.requirement.adjustment
            ):
                packet = load_snapshot(reference)
                return RawPriceResponse(
                    **packet.response, payload=Path(packet.raw_reference).read_bytes()
                )
        raise ValueError("No frozen price evidence supplied")


class SharedProvider:
    """One provider access lane shared by benchmark and stock workers.

    Frozen/fixture inputs have no network allowance. A live provider must retain
    its own shared request allowance for pages and retries inside fetch as well.
    """

    def __init__(self, provider):
        self.provider = provider
        self.lock = Lock()

    def fetch(self, *args):
        with self.lock:
            return self.provider.fetch(*args)


def collect_buy(database, runtime, snapshot, provider, *, progress=lambda message: None):
    """Collect each selected listing, retain every outcome, and publish one report."""
    save_universe(database, runtime.snapshots_dir, snapshot)
    selected = snapshot.result.selected
    request = snapshot.request
    market = request.market
    plans = snapshot.resolutions
    history = request.policy.requirements()
    # AnalysisRequest permits one price basis per timeframe; refuse ambiguous inputs.
    if len({h.timeframe for h in history}) != len(history):
        raise ValueError("Buy collection requires one price basis per timeframe")
    provider = SharedProvider(provider)
    progress(
        f"Market: {market.market_id}; cutoff: {request.requested_as_of.isoformat()}; "
        f"currency: {market.currency}; policy: {request.policy.version}"
    )
    progress(
        "Scope: PROTOTYPE; incomplete sector coverage; not a completed full-sector top-ten scan."
    )
    progress(f"Selected instruments ({len(selected)}): " + ", ".join(i.symbol for i in selected))

    def make_run(instrument):
        run = RunRecord(
            run_id=uuid4(),
            request=AnalysisRequest(
                request_id=uuid4(),
                mode="buy",
                market_id=market.market_id,
                instrument_ids=(instrument.instrument_id,),
                requested_as_of=request.requested_as_of,
                strategy_version="collection-only-1",
                history=history,
            ),
            market=market,
            configuration={"universe_snapshot_id": str(snapshot.snapshot_id)},
            source_version=source_version(),
            component_versions={"buy_collection": "1"},
            history_resolutions=tuple(p for p in plans if p.venue == instrument.venue),
        )
        with database.write() as repo:
            try:
                repo.get_security(instrument.security_id)
            except LookupError:
                repo.put_security(
                    Security(security_id=instrument.security_id, name=instrument.symbol)
                )
            repo.put_instrument(instrument)
            repo.create_run(run)
        return run

    reusable_context = {}

    def collect(run, instrument):
        if instrument.instrument_id in reusable_context:
            return reusable_context[instrument.instrument_id]
        try:
            CollectionService(database, runtime.snapshots_dir).collect(
                run.run_id, instrument, provider
            )
        except Exception:
            with database.read() as repo:
                if repo.get_run(run.run_id).status != OperationalStatus.FAILED:
                    raise
        with database.read() as repo:
            return repo.get_run(run.run_id), repo.list_snapshots(run.run_id)

    # Benchmark/context is independent of stock workers and collected once per mapping.
    shared = []
    if selected:
        for mapping in market.benchmarks:
            instrument = next(
                (
                    i
                    for i in (*selected, *(r.metadata.instrument for r in request.histories))
                    if i.symbol == mapping.symbol and i.venue == mapping.venue
                ),
                None,
            )
            if instrument is None:
                if market.route("prices").feed.kind != "synthetic":
                    shared.append(
                        {
                            "key": mapping.key,
                            "run_id": None,
                            "status": "FAILED",
                            "reason": "Benchmark identity/evidence not supplied in frozen inputs",
                            "snapshots": [],
                        }
                    )
                    progress(f"Shared context {mapping.key}: FAILED (frozen input absent)")
                    continue
                instrument = Instrument(
                    instrument_id=uuid4(),
                    security_id=uuid4(),
                    market_id=market.market_id,
                    venue=mapping.venue,
                    symbol=mapping.symbol,
                    quote_currency=mapping.currency,
                    provider_symbols=(
                        SymbolMapping(
                            provider=market.route("prices").provider,
                            symbol=mapping.symbol,
                            effective_from=date(1900, 1, 1),
                        ),
                    ),
                )
            if instrument.instrument_id in reusable_context:
                run, refs = reusable_context[instrument.instrument_id]
            else:
                run, refs = collect(make_run(instrument), instrument)
            reusable_context[instrument.instrument_id] = (run, refs)
            shared.append(
                {
                    "key": mapping.key,
                    "run_id": str(run.run_id),
                    "instrument_id": str(instrument.instrument_id),
                    "status": run.status.value,
                    "snapshots": [r.model_dump(mode="json") for r in refs],
                }
            )
            progress(f"Shared context {mapping.key}: {run.status.value}")
    children = [
        (
            reusable_context[i.instrument_id][0]
            if i.instrument_id in reusable_context
            else make_run(i),
            i,
        )
        for i in selected
    ]
    run = RunRecord(
        run_id=uuid4(),
        request=AnalysisRequest(
            request_id=uuid4(),
            mode="buy",
            market_id=market.market_id,
            instrument_ids=tuple(i.instrument_id for i in selected),
            sector=None if selected else "technology",
            requested_as_of=request.requested_as_of,
            strategy_version="collection-only-1",
            history=history,
        ),
        market=market,
        history_resolutions=plans,
        configuration={
            "buy_collection": {
                "sector": "technology",
                "policy_version": request.policy.version,
                "universe_snapshot_id": str(snapshot.snapshot_id),
                "scope": "PROTOTYPE",
                "full_sector_accepted": False,
                "enumeration_complete": snapshot.result.enumeration_complete,
                "unresolved_classifications": list(snapshot.result.unresolved_classifications),
                "selection_outcome": "NO_ELIGIBLE_STOCKS"
                if not selected
                else "FEWER_THAN_TEN"
                if len(selected) < 10
                else "TEN_SELECTED",
                "shortfall": snapshot.result.shortfall,
                "stock_concurrency": runtime.stock_concurrency,
                "shared_context": shared,
                "child_run_ids": [str(r.run_id) for r, _ in children],
            }
        },
        source_version=source_version(),
        component_versions={"buy_collection": "1"},
        status=OperationalStatus.RUNNING,
    )
    with database.write() as repo:
        repo.create_run(run)
    failures = 0
    with ThreadPoolExecutor(max_workers=runtime.stock_concurrency) as executor:
        pending = {executor.submit(collect, child, i): i for child, i in children}
        for completed, future in enumerate(as_completed(pending), 1):
            child, refs = future.result()
            instrument = pending[future]
            failures += child.status != OperationalStatus.SUCCEEDED
            with database.write() as repo:
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
            progress(
                f"Collection {completed}/{len(selected)}: {instrument.symbol} {child.status.value}"
            )
    context_failures = sum(c["status"] != "SUCCEEDED" for c in shared)
    status = (
        OperationalStatus.PARTIAL if failures or context_failures else OperationalStatus.SUCCEEDED
    )
    reason = (
        "NO_ELIGIBLE_STOCKS; "
        if not selected
        else f"FEWER_THAN_TEN ({len(selected)}); "
        if len(selected) < 10
        else ""
    )
    reason += f"{failures} stock collection failures; " if failures else ""
    reason += f"{context_failures} shared context failures; " if context_failures else ""
    reason += (
        "Prototype collection only; incomplete sector coverage; "
        "full-sector top-ten scan not completed"
    )
    with database.write() as repo:
        repo.set_run_status(run.run_id, status, reason)
    artifacts = ReportService(database, runtime.reports_dir).generate(run.run_id)
    return run.run_id, status, artifacts
