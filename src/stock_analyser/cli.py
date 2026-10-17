"""Menu, diagnostics, and PostgreSQL initialization commands."""

import hashlib
import json
import os
import sys
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.prompt import Prompt

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
                        f"{name} [unavailable — not implemented]",
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
                    display(f"{index}. {name} [unavailable — not implemented]")
                display("6. Exit")
                choice = Prompt.ask("Choose an action", console=console, default="6")
        except EOFError:
            return
        if choice is None or choice == "6":
            return
        if choice in {str(index) for index in range(1, 6)}:
            selected = choice
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
                    "workflows": "unavailable — not implemented",
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
    scenario: str = typer.Option("valid", help="Synthetic scenario: valid or missing-bar."),
) -> None:
    """Freeze one synthetic stock's evidence and persist quality outcomes."""
    from datetime import UTC, date, datetime
    from uuid import uuid4

    from stock_analyser.calendars import CutoffPolicy, HistoryResolver
    from stock_analyser.collection import CollectionService
    from stock_analyser.contracts import AnalysisRequest, HistoryRequirement, Security
    from stock_analyser.fixtures import (
        RawFixtureProvider,
        synthetic_markets,
        synthetic_session_calendars,
    )
    from stock_analyser.persistence.records import RunRecord

    database = None
    try:
        provider = RawFixtureProvider(scenario)
        runtime = ctx.obj
        if runtime.database_url is None:
            raise ConfigurationError("Set STOCK_ANALYSER_DATABASE_URL before collecting fixtures.")
        runtime.initialize()
        database = Database(runtime.database_url.get_secret_value())
        database.initialize()
        markets, instruments, _ = synthetic_markets()
        market = markets.resolve("US")
        instrument = instruments.resolve("US", "ACME")
        _, calendars = synthetic_session_calendars()
        requested = datetime(2026, 10, 16, 21, tzinfo=UTC)
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
            configuration={"fixture": scenario},
            source_version=source_version(),
            component_versions={"fixture": "raw-synthetic-1"},
            history_resolutions=(resolution,),
        )
        with database.write() as repo:
            repo.put_security(Security(security_id=instrument.security_id, name="Synthetic ACME"))
            repo.put_instrument(instrument)
            repo.create_run(run)
        packets = CollectionService(database, runtime.snapshots_dir).collect(
            run.run_id, instrument, provider
        )
        with database.read() as repo:
            status = repo.get_run(run.run_id).status
            manifest = repo.list_artifacts(run.run_id)[0].reference
        display(
            json.dumps(
                {
                    "run_id": str(run.run_id),
                    "status": status.value,
                    "manifest": manifest,
                    "evidence": [p.metadata.model_dump(mode="json") for p in packets],
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
