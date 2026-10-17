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
