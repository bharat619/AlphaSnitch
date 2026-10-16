"""Menu and setup commands for the first implementation story."""

import hashlib
import json
import os
import sys
from pathlib import Path

import questionary
import typer
from rich.console import Console
from rich.prompt import Prompt

from stock_analyser.config import Runtime, load_runtime
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
                }
            ),
            indent=2,
        ),
        markup=False,
        soft_wrap=True,
    )


@app.command("init")
def initialize(ctx: typer.Context) -> None:
    """Create runtime directories; no database or provider calls yet."""
    try:
        ctx.obj.initialize()
    except Exception as exc:
        fail(exc)
    display("Runtime directories are ready. Use doctor to inspect the paths.")


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
