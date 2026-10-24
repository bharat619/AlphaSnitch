"""Menu, diagnostics, and PostgreSQL initialization commands."""

import hashlib
import json
import os
import sys
from pathlib import Path
from uuid import UUID

import questionary
import typer
from rich.console import Console
from rich.prompt import Prompt

from stock_analyser.cache import CacheMode
from stock_analyser.config import ConfigurationError, Runtime, load_runtime
from stock_analyser.persistence import Database
from stock_analyser.security import configure_logging, redact, redact_values, safe_error
from stock_analyser.version import source_version

app = typer.Typer(
    help="Local stock analyser. Run without a command for the menu.",
    no_args_is_help=False,
    pretty_exceptions_enable=False,
)
console = Console(highlight=False)
WORKFLOWS = ("Buy", "Sell", "Portfolio", "Record offline trade", "Reports")
report_app = typer.Typer(help="Browse, reopen, and regenerate saved data-readiness reports.")
app.add_typer(report_app, name="report")
universe_app = typer.Typer(help="Select and replay an explicit prototype sector population.")
app.add_typer(universe_app, name="universe")
scan_app = typer.Typer(help="Collect sector evidence without producing recommendations.")
app.add_typer(scan_app, name="scan")


def run_buy(runtime, sector, input_path=None, fixture=None):
    from uuid import uuid4

    from stock_analyser.buy import FrozenPriceProvider, collect_buy
    from stock_analyser.calendars import ExchangeCalendarAdapter, HistoryResolver
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.universe import SelectionPolicy, SelectionRequest, select_universe
    from stock_analyser.universe_demo import fixture_request

    if sector.lower() != "technology":
        raise ValueError("Only technology is supported by the current selection policy")
    if (input_path is None) == (fixture is None):
        raise ValueError(
            "Choose --input frozen-selection.json or --fixture valid. "
            "Live full-sector coverage remains unqualified (US-011)."
        )
    if fixture is not None:
        if fixture not in ("valid", "zero", "shortfall", "one-stock-failure", "unassessable"):
            raise ValueError("Unknown buy fixture scenario")
        request, resolver = fixture_request(
            runtime.snapshots_dir / "buy-fixtures" / str(uuid4()),
            eligible_count=0 if fixture == "zero" else 3 if fixture == "shortfall" else 12,
            policy=SelectionPolicy(minimum_price_usd=100) if fixture == "zero" else None,
        )

        class BuyFixtureProvider(RawFixtureProvider):
            failure_symbol = None

            def fetch(self, instrument, market, resolution):
                scenario = "valid"
                if instrument.symbol == self.failure_symbol:
                    scenario = {
                        "one-stock-failure": "provider-failure",
                        "unassessable": "missing-bar",
                    }.get(fixture, "valid")
                return RawFixtureProvider(scenario).fetch(instrument, market, resolution)

        provider = BuyFixtureProvider()
    else:
        request = SelectionRequest.model_validate_json(input_path.read_bytes())
        resolver = HistoryResolver(ExchangeCalendarAdapter(), request.cutoff_policy)
        provider = FrozenPriceProvider(request.histories)
    snapshot = select_universe(request, resolver)
    if fixture and snapshot.result.selected:
        provider.failure_symbol = snapshot.result.selected[0].symbol
    database = report_database(runtime)
    try:
        database.initialize()
        run_id, status, artifacts = collect_buy(
            database, runtime, snapshot, provider, history_resolver=resolver, progress=display
        )
        display(f"Run: {run_id}; collection: {status.value}; report saved")
        for artifact in artifacts:
            display(f"{artifact.name}: {artifact.reference}")
        return status
    finally:
        database.close()


@scan_app.command("buy")
def scan_buy(
    ctx: typer.Context,
    sector: str = typer.Option(..., help="Sector: technology."),
    input_path: Path | None = typer.Option(None, "--input", help="Frozen SelectionRequest JSON."),
    fixture: str | None = typer.Option(
        None, help="valid, zero, shortfall, one-stock-failure, unassessable"
    ),
) -> None:
    """Save collection outcomes for every selected stock in an explicit prototype population."""
    try:
        status = run_buy(ctx.obj, sector, input_path, fixture)
    except Exception as exc:
        fail(exc)
    if status.value != "SUCCEEDED":
        raise typer.Exit(1)


def run_sell(runtime, input_path=None, fixture=None):
    from uuid import uuid4

    from stock_analyser.buy import FrozenPriceProvider
    from stock_analyser.fixtures import RawFixtureProvider
    from stock_analyser.sell import SellEvidence, collect_sell

    if (input_path is None) == (fixture is None):
        raise ValueError(
            "Choose --input frozen-sell-evidence.json or --fixture valid/one-holding-failure"
        )
    resolver = None
    if fixture is not None:
        if fixture not in ("valid", "one-holding-failure"):
            raise ValueError("Unknown sell fixture scenario")
        from stock_analyser.universe_demo import fixture_request

        request, resolver = fixture_request(runtime.snapshots_dir / "sell-fixtures" / str(uuid4()))
        evidence = SellEvidence(
            market=request.market,
            requested_as_of=request.requested_as_of,
            history=request.policy.requirements(),
            cutoff_policy=request.cutoff_policy,
        )

        class SellFixtureProvider(RawFixtureProvider):
            failed_id = None

            def fetch(self, instrument, market, resolution):
                if fixture == "one-holding-failure":
                    if self.failed_id is None:
                        self.failed_id = instrument.instrument_id
                    if instrument.instrument_id == self.failed_id:
                        raise ConnectionError("Synthetic holding collection failure")
                return super().fetch(instrument, market, resolution)

        provider = SellFixtureProvider()
    else:
        evidence = SellEvidence.model_validate_json(input_path.read_bytes())
        provider = FrozenPriceProvider(evidence.histories)
    database = report_database(runtime)
    try:
        database.initialize()
        run_id, status, artifacts = collect_sell(
            database, runtime, evidence, provider, history_resolver=resolver, progress=display
        )
        display(f"Run: {run_id}; collection: {status.value}; report saved")
        for artifact in artifacts:
            display(f"{artifact.name}: {artifact.reference}")
        return status
    finally:
        database.close()


@scan_app.command("sell")
def scan_sell(
    ctx: typer.Context,
    input_path: Path | None = typer.Option(
        None, "--input", help="Frozen SellEvidence JSON; holdings come from the ledger."
    ),
    fixture: str | None = typer.Option(
        None, help="Synthetic evidence: valid or one-holding-failure; never creates trades."
    ),
) -> None:
    """Collect evidence for every open holding at one frozen portfolio revision."""
    try:
        status = run_sell(ctx.obj, input_path, fixture)
    except Exception as exc:
        fail(exc)
    if status.value != "SUCCEEDED":
        raise typer.Exit(1)


def display_universe(snapshot) -> None:
    result = snapshot.result
    display(f"Universe snapshot: {snapshot.snapshot_id}")
    display("Scope: PROTOTYPE; full-sector acceptance remains blocked by US-011.")
    display(f"Enumeration complete: {result.enumeration_complete}")
    display(f"Unresolved classifications: {len(result.unresolved_classifications)}")
    display(f"Selected: {len(result.selected)}; shortfall: {result.shortfall}")
    for index, instrument in enumerate(result.selected, start=1):
        display(f"{index}. {instrument.symbol} ({instrument.instrument_id})")


@universe_app.command("demo")
def universe_demo(
    ctx: typer.Context,
    shortfall: bool = typer.Option(False, help="Demonstrate seven eligible securities."),
) -> None:
    """Persist synthetic ties, exclusions, cached histories and ordered selection."""
    from uuid import uuid4

    from stock_analyser.universe import save_universe, select_universe
    from stock_analyser.universe_demo import fixture_request

    database = None
    try:
        runtime = ctx.obj
        initialize_database(runtime)
        database = Database(runtime.database_url.get_secret_value())
        request, resolver = fixture_request(
            runtime.snapshots_dir / "universe-fixtures" / str(uuid4()),
            eligible_count=7 if shortfall else 12,
        )
        snapshot = select_universe(request, resolver)
        path = save_universe(database, runtime.snapshots_dir, snapshot)
        display_universe(snapshot)
        display(f"Saved: {path}")
        display(f"Replay: stock-analyser universe replay {snapshot.snapshot_id}")
    except Exception as exc:
        fail(exc)
    finally:
        if database:
            database.close()


@universe_app.command("select")
def universe_select(
    ctx: typer.Context,
    input_path: Path = typer.Option(..., "--input", help="Frozen SelectionRequest JSON."),
) -> None:
    """Select from supplied reference/enrichment data and cached history manifests."""
    from stock_analyser.calendars import ExchangeCalendarAdapter, HistoryResolver
    from stock_analyser.universe import SelectionRequest, save_universe, select_universe

    database = None
    try:
        request = SelectionRequest.model_validate_json(input_path.read_bytes())
        snapshot = select_universe(
            request, HistoryResolver(ExchangeCalendarAdapter(), request.cutoff_policy)
        )
        initialize_database(ctx.obj)
        database = Database(ctx.obj.database_url.get_secret_value())
        path = save_universe(database, ctx.obj.snapshots_dir, snapshot)
        display_universe(snapshot)
        display(f"Saved: {path}")
    except Exception as exc:
        fail(exc)
    finally:
        if database:
            database.close()


@universe_app.command("replay")
def universe_replay(
    ctx: typer.Context,
    snapshot_id: UUID,
    as_json: bool = typer.Option(False, "--json", help="Show complete frozen inputs and result."),
) -> None:
    """Verify cached evidence and reproduce the saved order without provider calls."""
    from stock_analyser.universe import replay_universe

    database = None
    try:
        if ctx.obj.database_url is None:
            raise ConfigurationError("Set STOCK_ANALYSER_DATABASE_URL before universe replay.")
        database = Database(ctx.obj.database_url.get_secret_value())
        with database.read() as repo:
            snapshot = repo.get_universe(snapshot_id)
        replay_universe(snapshot)
        if as_json:
            display(snapshot.model_dump_json(indent=2))
        else:
            display_universe(snapshot)
            display("Replay verified: ordered selection and all ranking inputs match.")
    except Exception as exc:
        fail(exc)
    finally:
        if database:
            database.close()


def workflow_label(name: str) -> str:
    return (
        name
        if name in ("Buy", "Sell", "Reports", "Portfolio", "Record offline trade")
        else f"{name} [unavailable — not implemented]"
    )


def display(message: str) -> None:
    console.print(redact(message), markup=False, soft_wrap=True)


def fail(exc: Exception) -> None:
    display(f"Error: {safe_error(exc)}")
    raise typer.Exit(1)


def menu(runtime: Runtime) -> None:
    display(f"Stock analyser — active market: {runtime.market}")
    interactive = sys.stdin.isatty() and console.is_terminal and os.environ.get("TERM") != "dumb"
    selected = "1"
    while True:
        try:
            if interactive:
                choices = [
                    questionary.Choice(
                        workflow_label(name),
                        value=str(index),
                        shortcut_key=str(index),
                    )
                    for index, name in enumerate(WORKFLOWS, start=1)
                ]
                choices.append(questionary.Choice("Exit", value="6", shortcut_key="6"))
                choice = questionary.select(
                    "Choose an action",
                    choices=choices,
                    default=selected,
                    use_shortcuts=True,
                    instruction="(↑/↓ or 1–6, Enter to select, Ctrl+C to exit)",
                ).unsafe_ask()
            else:
                for index, name in enumerate(WORKFLOWS, start=1):
                    display(f"{index}. {workflow_label(name)}")
                display("6. Exit")
                choice = Prompt.ask("Choose an action", console=console, default="6")
        except EOFError:
            return
        if choice is None or choice == "6":
            return
        if choice in {str(index) for index in range(1, 6)}:
            selected = choice
            if choice == "5":
                try:
                    display_report_history(runtime)
                    display("Open: stock-analyser report show <run-id>")
                    display("Retry: stock-analyser report regenerate <run-id>")
                except Exception as exc:
                    display(f"Error: {safe_error(exc)}")
            elif choice == "1":
                try:
                    sector = Prompt.ask("Sector", console=console, default="technology")
                    source = Prompt.ask(
                        "Frozen selection JSON path or fixture scenario "
                        "(valid/zero/shortfall/one-stock-failure/unassessable)",
                        console=console,
                    )
                    scenarios = {"valid", "zero", "shortfall", "one-stock-failure", "unassessable"}
                    run_buy(
                        runtime,
                        sector,
                        None if source in scenarios else Path(source),
                        source if source in scenarios else None,
                    )
                except EOFError:
                    return
                except Exception as exc:
                    display(f"Error: {safe_error(exc)}")
            elif choice == "2":
                try:
                    source = Prompt.ask(
                        "Frozen sell evidence JSON path or fixture scenario "
                        "(valid/one-holding-failure)",
                        console=console,
                    )
                    scenarios = {"valid", "one-holding-failure"}
                    run_sell(
                        runtime,
                        None if source in scenarios else Path(source),
                        source if source in scenarios else None,
                    )
                except EOFError:
                    return
                except Exception as exc:
                    display(f"Error: {safe_error(exc)}")
            elif choice in ("3", "4"):
                try:
                    if choice == "3":
                        show_portfolio(runtime)
                    else:
                        prompt_trade(runtime)
                except EOFError:
                    return
                except Exception as exc:
                    display(f"Error: {safe_error(exc)}")
            else:
                display(f"{WORKFLOWS[int(choice) - 1]} is unavailable — not implemented.")
        else:
            display("Choose a number from 1 to 6.")


@app.callback(invoke_without_command=True)
def start(
    ctx: typer.Context,
    config: Path | None = typer.Option(None, "--config", help="TOML configuration file."),
    data_dir: Path | None = typer.Option(None, "--data-dir", help="Runtime data directory."),
    market: str | None = typer.Option(None, "--market", help="Enabled market: US."),
) -> None:
    configure_logging()
    try:
        ctx.obj = load_runtime(config, data_dir, market)
    except Exception as exc:
        fail(exc)
    if ctx.invoked_subcommand is None:
        menu(ctx.obj)


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Show resolved paths and provenance without credentials or network calls."""
    runtime: Runtime = ctx.obj
    configuration = runtime.model_dump(mode="json")
    configuration_hash = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    console.print(
        json.dumps(
            redact_values(
                {
                    "runtime": configuration,
                    "configuration_sha256": configuration_hash,
                    "source": source_version(),
                    "workflows": {
                        "reports": "available",
                        "analysis": "unavailable — not implemented",
                    },
                    "offline_ready": True,
                    "database_configured": runtime.database_url is not None,
                }
            ),
            indent=2,
        ),
        markup=False,
        soft_wrap=True,
    )


@app.command("init")
def initialize(ctx: typer.Context) -> None:
    """Create runtime directories and migrate/seed the configured PostgreSQL database."""
    try:
        ctx.obj.initialize()
        initialize_database(ctx.obj)
    except Exception as exc:
        fail(exc)
    display("Runtime directories and PostgreSQL schema are ready.")


@app.command("qualify-universe")
def qualify_universe(
    ctx: typer.Context,
    output: Path = typer.Option(..., help="New directory for aggregate coverage findings."),
    max_pages: int = typer.Option(40, min=1, max=100),
    profile_limit: int = typer.Option(10, min=0, max=25),
) -> None:
    """Run the bounded live US Technology coverage spike; FAIL keeps the sector gate closed."""
    from platformdirs import user_data_path

    from stock_analyser.config import load_massive_key
    from stock_analyser.massive import live_client
    from stock_analyser.universe_spike import run_spike

    try:
        import yfinance as yf

        if output.exists():
            raise ValueError(
                "Choose a new output directory; previous findings cannot be overwritten"
            )
        query = yf.EquityQuery(
            "and",
            [
                yf.EquityQuery("eq", ["region", "us"]),
                yf.EquityQuery("eq", ["sector", "Technology"]),
            ],
        )

        def screen(offset, size):
            return yf.screen(query, offset=offset, size=size, sortField="ticker", sortAsc=True)

        def profile(symbol):
            return yf.Ticker(symbol).get_info()

        lock_path = user_data_path("stock-analyser", appauthor=False) / "massive.lock"
        with live_client(load_massive_key(), ctx.obj.massive, lock_path) as client:
            report = run_spike(
                client, screen, profile, output, max_pages=max_pages, profile_limit=profile_limit
            )
    except Exception as exc:
        fail(exc)
    display(f"Coverage findings saved: {output / 'findings.json'}")
    display("Full-sector acceptance: " + report["gate"]["status"])
    if not report["gate"]["full_sector_accepted"]:
        raise typer.Exit(1)


def initialize_database(runtime: Runtime) -> None:
    if runtime.database_url is None:
        raise ConfigurationError("Set STOCK_ANALYSER_DATABASE_URL before running init.")
    database = Database(runtime.database_url.get_secret_value())
    try:
        database.initialize()
    except Exception:
        # Database exceptions may contain credentials, SQL payloads, or connection details.
        raise ConfigurationError(
            "PostgreSQL initialization failed. Check the connection, server, and migration version."
        ) from None
    finally:
        database.close()


def main() -> None:
    try:
        exit_code = app(standalone_mode=False)
        if isinstance(exit_code, int):
            raise SystemExit(exit_code)
    except typer.Exit as exc:
        raise SystemExit(exc.exit_code) from None
    except (KeyboardInterrupt, typer.Abort):
        display("Exiting.")
        raise SystemExit(130) from None
    except Exception as exc:
        display(f"Error: {safe_error(exc)}")
        raise SystemExit(1) from None


@app.command("collect-fixture")
def collect_fixture(
    ctx: typer.Context,
    scenario: str = typer.Option(
        "valid", help="Synthetic scenario: valid, missing-bar, or provider-failure."
    ),
    research_scenario: str | None = typer.Option(
        None,
        help="Research fixture: relevant, irrelevant, empty, failed, known-event, "
        "unknown-event, or conflicting-event.",
    ),
) -> None:
    """Freeze one synthetic stock's evidence and persist quality outcomes."""
    from datetime import UTC, date, datetime
    from uuid import uuid4

    from stock_analyser.calendars import CutoffPolicy, HistoryResolver
    from stock_analyser.collection import CollectionService
    from stock_analyser.contracts import AnalysisRequest, HistoryRequirement, Security
    from stock_analyser.fixtures import (
        RawFixtureProvider,
        research_fixture,
        synthetic_markets,
        synthetic_session_calendars,
    )
    from stock_analyser.persistence.records import RunRecord
    from stock_analyser.reports import ReportService

    database = None
    try:
        provider = RawFixtureProvider(scenario)
        runtime = ctx.obj
        if runtime.database_url is None:
            raise ConfigurationError("Set STOCK_ANALYSER_DATABASE_URL before collecting fixtures.")
        database = Database(runtime.database_url.get_secret_value())
        database.initialize()
        markets, instruments, _ = synthetic_markets()
        market = markets.resolve("US")
        instrument = instruments.resolve("US", "ACME")
        _, calendars = synthetic_session_calendars()
        requested = datetime(2026, 10, 16, 21, tzinfo=UTC)
        research = (
            research_fixture(instrument, market, requested, research_scenario)
            if research_scenario
            else None
        )
        history = HistoryRequirement(timeframe="daily", minimum_bars=3)
        resolution = HistoryResolver(
            calendars[0],
            CutoffPolicy(earliest_session=date(2026, 10, 5), publication_timing="synthetic"),
        ).resolve(market, instrument.venue, history, requested)
        run = RunRecord(
            run_id=uuid4(),
            request=AnalysisRequest(
                request_id=uuid4(),
                mode="buy",
                market_id="US",
                instrument_ids=(instrument.instrument_id,),
                requested_as_of=requested,
                strategy_version="synthetic-1",
                history=(history,),
            ),
            market=market,
            configuration={"fixture": scenario, "research_fixture": research_scenario},
            source_version=source_version(),
            component_versions={"fixture": "raw-synthetic-1"},
            history_resolutions=(resolution,),
        )
        with database.write() as repo:
            repo.put_security(Security(security_id=instrument.security_id, name="Synthetic ACME"))
            repo.put_instrument(instrument)
            repo.create_run(run)
            if research is not None:
                from stock_analyser.research import ResearchRecord

                repo.put_research(
                    ResearchRecord(run_id=run.run_id, instrument=instrument, **research)
                )
        try:
            CollectionService(database, runtime.snapshots_dir).collect(
                run.run_id, instrument, provider
            )
        except Exception:
            # CollectionService persists a sanitized failure and any completed evidence.
            # Render that state even when the provider failed before returning a packet.
            with database.read() as repo:
                if repo.get_run(run.run_id).status.value != "FAILED":
                    raise
        artifacts = ReportService(database, runtime.reports_dir).generate(run.run_id)
        with database.read() as repo:
            status = repo.get_run(run.run_id).status
            snapshots = repo.list_snapshots(run.run_id)
        references = {a.name: a.reference for a in artifacts}
        display(
            json.dumps(
                {
                    "run_id": str(run.run_id),
                    "status": status.value,
                    "report_saved": True,
                    "manifest": references["manifest.json"],
                    "report": references["report.md"],
                    "report_json": references["report.json"],
                    "evidence": [s.metadata.model_dump(mode="json") for s in snapshots],
                },
                indent=2,
            )
        )
        if status.value != "SUCCEEDED":
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        fail(exc)
    finally:
        if database is not None:
            database.close()


def report_database(runtime: Runtime) -> Database:
    if runtime.database_url is None:
        raise ConfigurationError("Set STOCK_ANALYSER_DATABASE_URL to access reports.")
    return Database(runtime.database_url.get_secret_value())


def display_report_history(runtime: Runtime, limit: int = 20) -> None:
    from stock_analyser.reports import ReportService

    database = report_database(runtime)
    try:
        with database.read() as repo:
            runs = repo.list_runs(limit)
        if not runs:
            display("No report history yet.")
        service = ReportService(database, runtime.reports_dir)
        for run in runs:
            try:
                service.show(run.run_id)
                state = "SAVED"
            except ValueError:
                state = "INCOMPLETE — regenerate"
            display(
                f"{run.run_id}  {run.created_at.isoformat()}  {run.market.market_id} "
                f"{run.request.mode}  collection={run.status.value}  report={state}"
            )
    finally:
        database.close()


@report_app.command("history")
def report_history(ctx: typer.Context, limit: int = typer.Option(20, min=1, max=1000)) -> None:
    """List recent runs, including failed or incomplete report writes."""
    try:
        display_report_history(ctx.obj, limit)
    except Exception as exc:
        fail(exc)


@report_app.command("show")
def report_show(
    ctx: typer.Context,
    run_id: UUID,
    as_json: bool = typer.Option(False, "--json", help="Show the structured report."),
) -> None:
    """Reopen saved files and verify all three artifact hashes."""
    from stock_analyser.reports import ReportService

    database = None
    try:
        database = report_database(ctx.obj)
        display(
            ReportService(database, ctx.obj.reports_dir).show(
                run_id, "report.json" if as_json else "report.md"
            )
        )
    except Exception as exc:
        fail(exc)
    finally:
        if database is not None:
            database.close()


@report_app.command("regenerate")
def report_regenerate(ctx: typer.Context, run_id: UUID) -> None:
    """Retry artifact publication from persisted inputs without provider calls."""
    from stock_analyser.reports import ReportService

    database = None
    try:
        database = report_database(ctx.obj)
        artifacts = ReportService(database, ctx.obj.reports_dir).generate(run_id)
        display(f"Report saved for run {run_id}: {artifacts[0].reference}")
    except Exception as exc:
        fail(exc)
    finally:
        if database is not None:
            database.close()


@app.command("collect-massive")
def collect_massive(
    ctx: typer.Context,
    cache_mode: CacheMode = typer.Option(CacheMode.REUSE),
    news: bool = typer.Option(False, help="Also collect bounded Massive news discovery metadata."),
) -> None:
    """Opt in to bounded live AAPL/SPY daily/weekly collection and saved reports."""
    from contextlib import ExitStack

    from platformdirs import user_data_path

    from stock_analyser.config import load_massive_key
    from stock_analyser.live import collect_smoke
    from stock_analyser.massive import live_client

    database = None
    try:
        runtime = ctx.obj
        # Fixed per-user path: changing --data-dir cannot bypass the process lease.
        lock_path = user_data_path("stock-analyser", appauthor=False) / "massive.lock"
        with ExitStack() as stack:
            client = (
                None
                if cache_mode == CacheMode.REPLAY
                else stack.enter_context(
                    live_client(load_massive_key(), runtime.massive, lock_path)
                )
            )
            database = report_database(runtime)
            database.initialize()
            results = collect_smoke(database, runtime, client, cache_mode=cache_mode, news=news)
        display(json.dumps({"reports": results, "fresh_readiness": False}, indent=2))
        if any(not r["history_complete"] or r["collection_stopped"] for r in results):
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        fail(exc)
    finally:
        if database is not None:
            database.close()


@app.command("collect-prices")
def collect_prices(
    ctx: typer.Context,
    daily_bars: int = typer.Option(3, min=1, max=10000),
    weekly_bars: int = typer.Option(260, min=1, max=1000),
    cache_mode: CacheMode = typer.Option(
        CacheMode.REUSE,
        help="Reuse fresh evidence, refresh overlap, replay offline, or bypass cache.",
    ),
    news: bool = typer.Option(False, help="Also collect bounded Massive news discovery metadata."),
) -> None:
    """Collect AAPL/SPY using one capable approved source for mandatory history."""
    from contextlib import ExitStack

    from platformdirs import user_data_path

    from stock_analyser.config import load_massive_key
    from stock_analyser.live import collect_smoke
    from stock_analyser.massive import live_client

    database = None
    try:
        runtime = ctx.obj
        with ExitStack() as stack:

            def massive_client():
                lock = user_data_path("stock-analyser", appauthor=False) / "massive.lock"
                return stack.enter_context(live_client(load_massive_key(), runtime.massive, lock))

            database = report_database(runtime)
            database.initialize()
            results = collect_smoke(
                database,
                runtime,
                routed=True,
                daily_bars=daily_bars,
                weekly_bars=weekly_bars,
                massive_client_factory=massive_client,
                cache_mode=cache_mode,
                news=news,
            )
        display(json.dumps({"reports": results, "fresh_readiness": False}, indent=2))
        if any(not r["history_complete"] or r["collection_stopped"] for r in results):
            raise typer.Exit(1)
    except typer.Exit:
        raise
    except Exception as exc:
        fail(exc)
    finally:
        if database is not None:
            database.close()


def show_portfolio(runtime):
    from stock_analyser.ledger import portfolio_state

    database = report_database(runtime)
    try:
        positions, revision = portfolio_state(database)
        display(f"Portfolio revision: {revision}")
        if not positions:
            display("Portfolio is empty.")
        for position in positions:
            display(position.model_dump_json(indent=2))
    finally:
        database.close()


def confirm_trade(runtime, confirmation):
    from stock_analyser.ledger import record_execution

    display("Confirm actual offline execution (no broker order will be placed):")
    display(confirmation.model_dump_json(indent=2))
    if not typer.confirm("Record this execution?", default=False):
        display("Cancelled; no ledger write.")
        return
    database = report_database(runtime)
    try:
        receipt = record_execution(database, confirmation)
        display("Execution receipt:")
        display(receipt.model_dump_json(indent=2))
    finally:
        database.close()


def prompt_trade(runtime):
    from stock_analyser.ledger import TradeConfirmation

    values = {}
    for field in (
        "instrument_id",
        "venue",
        "side",
        "quantity",
        "price",
        "currency",
        "executed_at",
        "idempotency_key",
    ):
        values[field] = Prompt.ask(field, console=console)
    values["fees"] = json.loads(Prompt.ask("Itemized fees JSON", default="[]", console=console))
    confirm_trade(runtime, TradeConfirmation.model_validate(values))


@app.command("record-trade")
def record_trade(ctx: typer.Context, input_path: Path = typer.Option(..., "--input")):
    """Review a TradeConfirmation JSON file and explicitly confirm its recording."""
    from stock_analyser.ledger import TradeConfirmation

    try:
        confirm_trade(ctx.obj, TradeConfirmation.model_validate_json(input_path.read_bytes()))
    except Exception as exc:
        fail(exc)


@app.command("portfolio")
def portfolio_command(ctx: typer.Context):
    """Display FIFO holdings and currency-labelled accounting (including closed positions)."""
    try:
        show_portfolio(ctx.obj)
    except Exception as exc:
        fail(exc)


@app.command("execution-receipt")
def receipt_command(ctx: typer.Context, key: str):
    """Replay the immutable receipt for an idempotency key."""
    from stock_analyser.ledger import execution_receipt

    database = None
    try:
        database = report_database(ctx.obj)
        display(execution_receipt(database, key).model_dump_json(indent=2))
    except Exception as exc:
        fail(exc)
    finally:
        if database:
            database.close()


@app.command("correct-execution")
def correct_execution_command(ctx: typer.Context, input_path: Path = typer.Option(..., "--input")):
    """Review and append a correction (replacement=null reverses the original execution)."""
    from stock_analyser.ledger import Correction

    try:
        confirm_ledger_event(ctx.obj, Correction.model_validate_json(input_path.read_bytes()))
    except Exception as exc:
        fail(exc)


@app.command("record-corporate-action")
def corporate_action_command(ctx: typer.Context, input_path: Path = typer.Option(..., "--input")):
    """Apply an explicit split or flag a complex action for reconciliation."""
    from stock_analyser.ledger import CorporateAction

    try:
        confirm_ledger_event(ctx.obj, CorporateAction.model_validate_json(input_path.read_bytes()))
    except Exception as exc:
        fail(exc)


def confirm_ledger_event(runtime, operation):
    from stock_analyser.ledger import record_ledger_event

    display(operation.model_dump_json(indent=2))
    if not typer.confirm("Record this ledger event?", default=False):
        display("Cancelled; no ledger write.")
        return
    database = report_database(runtime)
    try:
        display(record_ledger_event(database, operation).model_dump_json(indent=2))
    finally:
        database.close()


@app.command("ledger-history")
def ledger_history_command(ctx: typer.Context):
    """Show immutable executions, corrections and corporate actions with receipts."""
    from stock_analyser.ledger import ledger_history

    database = None
    try:
        database = report_database(ctx.obj)
        for receipt in ledger_history(database):
            display(receipt.model_dump_json(indent=2))
    except Exception as exc:
        fail(exc)
    finally:
        if database:
            database.close()
