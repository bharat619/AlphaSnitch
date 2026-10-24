import io
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from stock_analyser import cli
from stock_analyser.config import load_runtime


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        (["\x1b[B\r", "6\r"], "Sell is unavailable"),
        (["\x1b[B\r", "\x1b[A\r", "6\r"], "Buy collection started"),
        (["\x1b[A\r"], None),
        (["4\r", "6\r"], "Trade recording started"),
        (["\x03"], "interrupt"),
    ],
)
def test_interactive_menu_keyboard_navigation(monkeypatch, tmp_path, keys, expected):
    output = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=output, force_terminal=True))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setenv("TERM", "xterm-256color")
    config = tmp_path / "config.toml"
    config.write_text('market = "US"')
    runtime = load_runtime(config, data_dir=tmp_path / "runtime")
    monkeypatch.setattr(cli, "prompt_trade", lambda *a: cli.display("Trade recording started"))
    select = cli.questionary.select
    sequences = iter(keys)
    defaults = []
    monkeypatch.setattr(cli.Prompt, "ask", lambda *a, **k: "valid")
    monkeypatch.setattr(cli, "run_buy", lambda *a: cli.display("Buy collection started"))

    with create_pipe_input() as keyboard:

        def select_with_keyboard(*args, **kwargs):
            defaults.append(kwargs["default"])
            keyboard.send_text(next(sequences))
            return select(*args, **kwargs, input=keyboard, output=DummyOutput())

        monkeypatch.setattr(cli.questionary, "select", select_with_keyboard)
        if expected == "interrupt":
            with pytest.raises(KeyboardInterrupt):
                cli.menu(runtime)
            return
        cli.menu(runtime)

    if expected:
        assert expected in output.getvalue()
    else:
        assert "is unavailable" not in output.getvalue()
    if len(keys) > 1:
        assert defaults[1] == ("4" if keys[0] == "4\r" else "2")
    assert not runtime.data_dir.exists()
