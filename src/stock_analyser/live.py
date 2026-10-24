"""Small opt-in US smoke workflow, reusing collection and offline reports."""

from datetime import UTC, date, datetime, timedelta
from importlib.metadata import version
from uuid import NAMESPACE_URL, uuid4, uuid5

from stock_analyser.cache import CacheMode, EvidenceCache
from stock_analyser.calendars import CutoffPolicy, ExchangeCalendarAdapter, HistoryResolver
from stock_analyser.collection import CollectionService
from stock_analyser.contracts import (
    AnalysisRequest,
    HistoryRequirement,
    Instrument,
    Security,
    SymbolMapping,
)
from stock_analyser.markets import US_MARKET
from stock_analyser.massive import MassiveEvidenceProvider, ProviderCooldownError
from stock_analyser.persistence.records import RunRecord
from stock_analyser.reports import ReportInput, ReportService
from stock_analyser.version import source_version


def smoke_instrument(symbol: str, venue: str) -> Instrument:
    """Explicit smoke fixtures; not a universe/reference-data discovery service."""
    # Known smoke symbols cover their public trading history. These identity
    # bounds do not promise returned provider coverage (validated after fetching).
    # References: docs/yfinance-evidence.md, "Smoke symbol history".
    yfinance_start = {
        ("AAPL", "XNAS"): date(1980, 12, 12),
        ("SPY", "ARCX"): date(1993, 1, 29),
    }.get((symbol, venue), date(2020, 1, 1))
    return Instrument(
        instrument_id=uuid5(NAMESPACE_URL, f"alphasnitch:live:US:{venue}:{symbol}"),
        security_id=uuid5(NAMESPACE_URL, f"alphasnitch:live:US:{symbol}"),
        market_id="US",
        venue=venue,
        symbol=symbol,
        quote_currency="USD",
        provider_symbols=(
            SymbolMapping(
                provider="massive",
                symbol=symbol,
                effective_from=date(2020, 1, 1),
            ),
            SymbolMapping(provider="yfinance", symbol=symbol, effective_from=yfinance_start),
        ),
    )


def has_complete_history(data: ReportInput) -> bool:
    """Ignore only unverified publication timing when judging smoke coverage."""
    return not data.evidence_issues and all(
        any(
            packet.metadata.timeframe == requirement.timeframe
            and packet.metadata.adjustment == requirement.adjustment
            and len(packet.bars) >= requirement.required_bars
            and not any(
                finding.severity == "error" and finding.code != "PUBLICATION_UNVERIFIED"
                for finding in packet.findings
            )
            for packet in data.packets
        )
        for requirement in data.run.request.history
        if requirement.mandatory
    )


def collect_smoke(
    database,
    runtime,
    client=None,
    *,
    requested=None,
    routed=False,
    daily_bars=3,
    weekly_bars=2,
    massive_client_factory=None,
    cache_mode=CacheMode.REUSE,
    news=False,
):
    """AAPL and the configured compatible SPY benchmark each get a saved report.

    Each is an existing one-instrument run; a common smoke group and requested
    instant link the reports without changing the report contract's scope.
    """
    requested = requested or datetime.now(UTC)
    group = str(uuid4())
    market = US_MARKET
    instruments = (smoke_instrument("AAPL", "XNAS"),) + tuple(
        smoke_instrument(b.symbol, b.venue) for b in market.benchmarks
    )
    cache = EvidenceCache(runtime.cache_dir / "evidence")
    if cache_mode not in (CacheMode.OFF, CacheMode.REPLAY):
        instruments = tuple(
            Instrument.model_validate(
                cache.metadata(
                    {
                        "kind": "explicit-smoke-listing",
                        "version": "1",
                        "instrument": instrument.model_dump(mode="json"),
                    },
                    lambda instrument=instrument: instrument.model_dump(mode="json"),
                    refresh=cache_mode == CacheMode.REFRESH,
                )
            )
            for instrument in instruments
        )
    history = tuple(
        HistoryRequirement(timeframe=t, minimum_bars=n)
        for t, n in (("daily", daily_bars), ("weekly", weekly_bars))
    )
    resolver = HistoryResolver(
        ExchangeCalendarAdapter(),
        CutoffPolicy(
            earliest_session=date(1990, 1, 1),
            publication_timing="unverified",
        ),
    )
    plans = {
        instrument.instrument_id: tuple(
            resolver.resolve(market, instrument.venue, h, requested) for h in history
        )
        for instrument in instruments
    }
    selection = None
    if routed:
        from stock_analyser.routing import select_price_route
        from stock_analyser.yfinance import YFinancePriceProvider

        all_plans = tuple(p for per_instrument in plans.values() for p in per_instrument)
        selection = select_price_route(market, all_plans, runtime.price_routing)
        if (
            cache_mode != CacheMode.REPLAY
            and selection.route is not None
            and selection.route.provider == "massive"
            and client is None
        ):
            if massive_client_factory is None:
                raise ValueError("Selected Massive route requires a configured client")
            client = massive_client_factory()
        provider = (
            None
            if cache_mode == CacheMode.REPLAY
            else YFinancePriceProvider(all_plans)
            if selection.route is not None and selection.route.provider == "yfinance"
            else MassiveEvidenceProvider(client)
        )
    else:
        provider = None if cache_mode == CacheMode.REPLAY else MassiveEvidenceProvider(client)
    results = []
    for instrument in instruments:
        resolutions = plans[instrument.instrument_id]
        run = RunRecord(
            run_id=uuid4(),
            request=AnalysisRequest(
                request_id=uuid4(),
                mode="buy",
                market_id="US",
                instrument_ids=(instrument.instrument_id,),
                requested_as_of=requested,
                strategy_version="data-readiness-only-1",
                history=history,
            ),
            market=market,
            configuration={
                "smoke_group": group,
                "provider": (
                    selection.route.provider
                    if selection and selection.route
                    else None
                    if routed
                    else "massive"
                ),
                "massive": runtime.massive.model_dump(),
                "purpose": "stock" if instrument.symbol == "AAPL" else "benchmark",
                "subscription_verified": bool(
                    selection
                    and selection.route
                    and selection.route.provider == "massive"
                    and selection.massive_coverage
                ),
                "publication_timing": "unverified",
                "identity_source": "explicit smoke listing; not live reference discovery",
                "cache_parameters": {
                    "massive": runtime.massive.model_dump(mode="json"),
                    "yfinance": {
                        "version": version("yfinance"),
                        "auto_adjust": False,
                        "back_adjust": False,
                        "repair": False,
                        "actions": True,
                        "rounding": False,
                        "prepost": False,
                    },
                },
                "cache_mode": str(cache_mode),
                "news_requested": news,
            },
            source_version=source_version(),
            component_versions={"price_collection": "2", "history_routing": "1"}
            if routed
            else {"massive": "1"},
            history_resolutions=resolutions,
            price_routing=selection,
        )
        with database.write() as repo:
            repo.put_security(Security(security_id=instrument.security_id, name=instrument.symbol))
            repo.put_instrument(instrument)
            repo.create_run(run)
        stop_remaining = False
        try:
            CollectionService(database, runtime.snapshots_dir, cache=cache).collect(
                run.run_id, instrument, provider, cache_mode=cache_mode
            )
        except Exception as exc:
            stop_remaining = isinstance(exc, ProviderCooldownError)
            with database.read() as repo:
                if repo.get_run(run.run_id).status.value != "FAILED":
                    raise
        if news:
            from stock_analyser.massive_news import MassiveNewsProvider
            from stock_analyser.research import NewsCoverage, ResearchRecord

            coverage = None
            if cache_mode != CacheMode.REPLAY and not stop_remaining:
                try:
                    if client is None:
                        if massive_client_factory is None:
                            raise ValueError("News requires a configured Massive client")
                        client = massive_client_factory()
                    coverage = MassiveNewsProvider(client).fetch(instrument, market, requested)
                    stop_remaining = coverage.collection_stopped
                except Exception as exc:
                    stop_remaining = isinstance(exc, ProviderCooldownError)
            if coverage is None:
                replay = cache_mode == CacheMode.REPLAY
                coverage = NewsCoverage(
                    provider="massive",
                    provider_symbol=instrument.provider_symbol("massive", requested.date()),
                    window_start=requested - timedelta(days=runtime.massive.news_lookback_days),
                    window_end=requested,
                    retrieved_at=datetime.now(UTC),
                    status="NOT_REQUESTED" if replay else "FAILED",
                    reason="Offline price replay does not retrieve news; reopen the original "
                    "report for retained research."
                    if replay
                    else "News collection could not run; coverage remains unknown.",
                    findings=("OFFLINE_REPLAY" if replay else "COLLECTION_UNAVAILABLE",),
                    collection_stopped=stop_remaining,
                )
            with database.write() as repo:
                repo.put_research(
                    ResearchRecord(
                        run_id=run.run_id,
                        instrument=instrument,
                        news=coverage,
                    )
                )
        reports = ReportService(database, runtime.reports_dir)
        artifacts = reports.generate(run.run_id)
        coverage_complete = has_complete_history(reports.freeze(run.run_id))
        with database.read() as repo:
            snapshots = repo.list_snapshots(run.run_id)
        results.append(
            {
                "run_id": str(run.run_id),
                "symbol": instrument.symbol,
                "smoke_group": group,
                "report": next(a.reference for a in artifacts if a.name == "report.md"),
                "report_json": next(a.reference for a in artifacts if a.name == "report.json"),
                "snapshot_count": len(snapshots),
                "history_complete": coverage_complete,
                "collection_stopped": stop_remaining,
                "publication_timing": "unverified",
            }
        )
        if stop_remaining:
            break
    return results
