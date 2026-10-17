"""Local .env setup works without exports and preserves shell overrides."""

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from stock_analyser.cli import app
from stock_analyser.config import ConfigurationError, load_environment, load_runtime


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in tuple(os.environ):
        if name.startswith("STOCK_ANALYSER_"):
            monkeypatch.delenv(name)
    # Register dotenv mutations with monkeypatch so each test restores the environment.
    for name in (
        "STOCK_ANALYSER_DATABASE_URL",
        "STOCK_ANALYSER_POSTGRES_PASSWORD",
        "STOCK_ANALYSER_DATA_DIR",
    ):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)


def test_cli_loads_local_dotenv_without_exposing_credentials(tmp_path):
    url = "postgresql+psycopg://user:dotenv-private-password@localhost/stock"
    Path(".env").write_text(
        f'STOCK_ANALYSER_DATABASE_URL="{url}"\n'
        "STOCK_ANALYSER_POSTGRES_PASSWORD=dotenv-private-password\n"
        f'STOCK_ANALYSER_DATA_DIR="{tmp_path / "café"}"\n',
        encoding="utf-8",
    )
    result = CliRunner().invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert data["database_configured"] is True
    assert data["runtime"]["data_dir"] == str(tmp_path / "café")
    assert "dotenv-private-password" not in result.output
    assert "database_url" not in data["runtime"]
    assert load_runtime().database_url.get_secret_value() == url


def test_shell_variables_override_dotenv(monkeypatch):
    Path(".env").write_text("STOCK_ANALYSER_DATABASE_URL=from-file\n")
    monkeypatch.setenv("STOCK_ANALYSER_DATABASE_URL", "from-shell")
    assert load_runtime().database_url.get_secret_value() == "from-shell"


def test_only_current_directory_dotenv_is_loaded(tmp_path, monkeypatch):
    Path(".env").write_text("STOCK_ANALYSER_DATABASE_URL=from-parent\n")
    child = tmp_path / "child"
    child.mkdir()
    monkeypatch.chdir(child)
    Path(".env.local").write_text("STOCK_ANALYSER_DATABASE_URL=from-local\n")
    assert load_runtime().database_url is None


def test_dotenv_decoding_failure_does_not_expose_values():
    Path(".env").write_bytes(b"STOCK_ANALYSER_DATABASE_URL=private-secret\xff")
    with pytest.raises(ConfigurationError, match="Cannot read .env"):
        load_environment()


def test_existing_massive_local_key_is_private_and_shell_wins(monkeypatch):
    from stock_analyser.config import load_massive_key

    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    Path(".env.local").write_text("MASSIVE_API_KEY=local-private-key\n")
    key = load_massive_key()
    assert key.get_secret_value() == "local-private-key"
    assert "local-private-key" not in repr(key)
    monkeypatch.setenv("MASSIVE_API_KEY", "shell-private-key")
    assert load_massive_key().get_secret_value() == "shell-private-key"
    result = CliRunner().invoke(app, ["doctor"])
    assert "shell-private-key" not in result.output
    assert "local-private-key" not in result.output


def test_massive_allowance_configuration_and_invalid_secrets(monkeypatch):
    from stock_analyser.config import load_massive_key

    Path("config.toml").write_text("[massive]\nrequests_per_minute=3\nmax_requests=20\n")
    runtime = load_runtime(Path("config.toml"))
    assert runtime.massive.requests_per_minute == 3
    assert runtime.massive.max_requests == 20
    monkeypatch.setenv("MASSIVE_API_KEY", "invalid secret")
    with pytest.raises(ConfigurationError) as error:
        load_massive_key()
    assert "invalid secret" not in str(error.value)
    Path("config.toml").write_text("[massive]\nrequests_per_minute=0\n")
    with pytest.raises(ConfigurationError):
        load_runtime(Path("config.toml"))
