"""Small opt-in US smoke workflow, reusing collection and offline reports."""

from datetime import UTC, date, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

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


def collect_smoke(database, runtime, client, *, requested=None):
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
    history = tuple(
        HistoryRequirement(timeframe=t, minimum_bars=n) for t, n in (("daily", 3), ("weekly", 2))
    )
    resolver = HistoryResolver(
        ExchangeCalendarAdapter(),
        CutoffPolicy(
            earliest_session=requested.date() - timedelta(days=60),
            publication_timing="unverified",
        ),
    )
    provider = MassiveEvidenceProvider(client)
    results = []
    for instrument in instruments:
        resolutions = tuple(
            resolver.resolve(market, instrument.venue, h, requested) for h in history
        )
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
                "provider": "massive",
                "massive": runtime.massive.model_dump(),
                "purpose": "stock" if instrument.symbol == "AAPL" else "benchmark",
                "subscription_verified": False,
                "publication_timing": "unverified",
                "identity_source": "explicit smoke listing; not live reference discovery",
            },
            source_version=source_version(),
            component_versions={"massive": "1"},
            history_resolutions=resolutions,
        )
        with database.write() as repo:
            repo.put_security(Security(security_id=instrument.security_id, name=instrument.symbol))
            repo.put_instrument(instrument)
            repo.create_run(run)
        stop_remaining = False
        try:
            CollectionService(database, runtime.snapshots_dir).collect(
                run.run_id, instrument, provider
            )
        except Exception as exc:
            stop_remaining = isinstance(exc, ProviderCooldownError)
            with database.read() as repo:
                if repo.get_run(run.run_id).status.value != "FAILED":
                    raise
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
