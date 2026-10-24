"""Bounded sector collection from explicitly supplied prototype universe evidence."""

import json
from datetime import date
from pathlib import Path
from threading import Lock
from uuid import uuid4

from stock_analyser.cache import load_snapshot
from stock_analyser.calendars import ExchangeCalendarAdapter, HistoryResolver
from stock_analyser.collection import RawPriceResponse, encode, validate_prices
from stock_analyser.contracts import (
    AnalysisRequest,
    Instrument,
    OperationalStatus,
    Security,
    SourceRecord,
    SymbolMapping,
)
from stock_analyser.persistence.records import RunRecord
from stock_analyser.universe import save_universe
from stock_analyser.version import source_version


class FrozenPriceProvider:
    """Replay verified raw inputs without making a provider request."""

    def __init__(self, histories):
        self.histories = histories

    def fetch(self, instrument, market, resolution):
        return self.fetch_with_sources(instrument, market, resolution)[0]

    def fetch_with_sources(self, instrument, market, resolution):
        for reference in self.histories:
            meta = reference.metadata
            if (
                meta.instrument == instrument
                and meta.timeframe == resolution.requirement.timeframe
                and meta.adjustment == resolution.requirement.adjustment
            ):
                packet = load_snapshot(reference)
                if meta.market != market:
                    raise ValueError(
                        "Frozen price evidence belongs to another market configuration"
                    )
                response = RawPriceResponse(
                    **packet.response, payload=Path(packet.raw_reference).read_bytes()
                )
                if packet.resolution.periods != resolution.periods:
                    original = {p.session_end: p for p in packet.resolution.periods}
                    if any(original.get(p.session_end) != p for p in resolution.periods):
                        raise ValueError("Frozen evidence does not cover the requested periods")
                    _, findings, _ = validate_prices(response, packet.resolution)
                    if any(f.severity == "error" for f in findings):
                        raise ValueError("Cannot project invalid frozen price evidence")
                    payload = json.loads(response.payload, parse_float=str)
                    sessions = {p.session_end.isoformat() for p in resolution.periods}
                    payload["bars"] = [r for r in payload["bars"] if r["session"] in sessions]
                    payload["frozen_projection"] = {
                        "snapshot_id": str(meta.snapshot_id),
                        "manifest": reference.reference,
                        "manifest_sha256": reference.sha256,
                    }
                    response = response.model_copy(update={"payload": encode(payload)})
                # Keep the original immutable manifest and raw source chain beside
                # the derived response; projecting rows never relabels original bytes.
                sources = meta.sources + (
                    SourceRecord(
                        provider=response.provider,
                        provider_symbol=response.provider_symbol,
                        retrieved_at=response.retrieved_at,
                        reference=reference.reference,
                        sha256=reference.sha256,
                        session_start=packet.resolution.history_start,
                        session_end=packet.resolution.session_date,
                    ),
                )
                return response, sources
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

    def fetch_with_sources(self, *args):
        with self.lock:
            fetch = getattr(self.provider, "fetch_with_sources", None)
            return fetch(*args) if fetch else (self.provider.fetch(*args), ())


def collect_buy(
    database,
    runtime,
    snapshot,
    provider,
    *,
    history_resolver=None,
    progress=lambda message: None,
    provider_spec=None,
):
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
    progress(
        f"Market: {market.market_id}; cutoff: {request.requested_as_of.isoformat()}; "
        f"currency: {market.currency}; policy: {request.policy.version}"
    )
    progress(
        "Scope: PROTOTYPE; incomplete sector coverage; not a completed full-sector top-ten scan."
    )
    progress(f"Selected instruments ({len(selected)}): " + ", ".join(i.symbol for i in selected))

    def make_run(instrument, *, benchmark=False):
        resolutions = tuple(p for p in plans if p.venue == instrument.venue)
        if benchmark:
            resolver = history_resolver or HistoryResolver(
                ExchangeCalendarAdapter(), request.cutoff_policy
            )
            resolutions = tuple(
                resolver.resolve(market, instrument.venue, h, request.requested_as_of)
                for h in history
            )
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
            history_resolutions=resolutions,
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

    from stock_analyser.resume import CollectionJob, ResumePlan, execute_plan, provider_recipe

    reusable_context = {}
    shared, context_jobs = [], []
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
            if instrument.instrument_id not in reusable_context:
                child = make_run(instrument, benchmark=True)
                reusable_context[instrument.instrument_id] = child
                context_jobs.append(CollectionJob(run_id=child.run_id, instrument=instrument))
            child = reusable_context[instrument.instrument_id]
            shared.append(
                {
                    "key": mapping.key,
                    "run_id": str(child.run_id),
                    "instrument_id": str(instrument.instrument_id),
                    "status": "NOT_RUN",
                    "snapshots": [],
                }
            )
    children = [
        (
            reusable_context[i.instrument_id]
            if i.instrument_id in reusable_context
            else make_run(i),
            i,
        )
        for i in selected
    ]
    jobs = context_jobs + [
        CollectionJob(run_id=child.run_id, instrument=i)
        for child, i in children
        if i.instrument_id not in reusable_context
    ]
    plan = ResumePlan(
        jobs=tuple(jobs),
        provider=provider_spec or provider_recipe(provider),
        stock_concurrency=runtime.stock_concurrency,
    )
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
            "resume_plan": plan.model_dump(mode="json"),
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
            },
        },
        source_version=source_version(),
        component_versions={"buy_collection": "1"},
        status=OperationalStatus.RUNNING,
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
