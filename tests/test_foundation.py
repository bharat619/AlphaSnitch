import io
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.text import Text
from typer.testing import CliRunner

from stock_analyser.cli import app
from stock_analyser.config import ConfigurationError, load_runtime
from stock_analyser.security import RedactingFormatter, redact
from stock_analyser.version import source_version

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    # Keep tests independent of the user's optional configuration and runtime state.
    import stock_analyser.config as config

    for name in tuple(os.environ):
        if name.startswith("STOCK_ANALYSER_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(config, "default_config_file", lambda: tmp_path / "absent.toml")
    monkeypatch.setattr(config, "user_data_path", lambda *a, **kw: tmp_path / "user-data")


def test_help_is_available_without_config_or_credentials(monkeypatch):
    monkeypatch.setenv("STOCK_ANALYSER_CONFIG", "/does/not/exist")
    monkeypatch.setenv("FORCE_COLOR", "1")
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    help_text = Text.from_ansi(result.output).plain
    assert "--config" in help_text
    assert "doctor" in help_text


def test_menu_and_unavailable_workflow(tmp_path):
    result = runner.invoke(app, ["--data-dir", str(tmp_path / "runtime")], input="1\n6\n")
    assert result.exit_code == 0
    for label in ("Buy", "Sell", "Portfolio", "Record offline trade", "Reports", "Exit"):
        assert label in result.output
    assert "Buy is unavailable" in result.output
    assert "active market: US" in result.output
    assert not (tmp_path / "runtime").exists()


def test_menu_rejects_invalid_choice_and_handles_eof():
    result = runner.invoke(app, [], input="bogus\n")
    assert result.exit_code == 0
    assert "Choose a number from 1 to 6" in result.output


def test_doctor_records_version_and_stable_configuration_without_writes(tmp_path):
    arguments = ["--data-dir", str(tmp_path / "runtime"), "doctor"]
    first = runner.invoke(app, arguments)
    second = runner.invoke(app, arguments)
    assert first.exit_code == second.exit_code == 0
    data = json.loads(first.output)
    assert data["offline_ready"] is True
    assert data["source"]["package_version"] == "0.1.0"
    assert data["configuration_sha256"] == json.loads(second.output)["configuration_sha256"]
    assert not (tmp_path / "runtime").exists()


def test_runtime_path_precedence_and_init(tmp_path, monkeypatch):
    config = tmp_path / "config.toml"
    config.write_text('[paths]\ndata_dir = "configured"\nreports_dir = "output"\n')
    monkeypatch.setenv("STOCK_ANALYSER_DATA_DIR", str(tmp_path / "environment"))
    monkeypatch.setenv("STOCK_ANALYSER_CACHE_DIR", str(tmp_path / "shared-cache"))
    runtime = load_runtime(config, tmp_path / "cli")
    assert runtime.data_dir == tmp_path / "cli"
    assert runtime.reports_dir == tmp_path / "output"
    assert runtime.cache_dir == tmp_path / "shared-cache"
    assert load_runtime(config).data_dir == tmp_path / "environment"
    monkeypatch.delenv("STOCK_ANALYSER_DATA_DIR")
    assert load_runtime(config).data_dir == tmp_path / "configured"
    runtime.initialize()
    for name in ("database", "snapshots", "cache", "reports", "logs"):
        assert getattr(runtime, f"{name}_dir").is_dir()
    result = runner.invoke(app, ["--config", str(config), "init"])
    assert result.exit_code == 0


def test_default_data_path_uses_user_directory(tmp_path):
    runtime = load_runtime()
    assert runtime.data_dir == tmp_path / "user-data"
    assert not runtime.data_dir.exists()


@pytest.mark.parametrize(
    "content",
    [
        'market = "IN"',
        'unknown = "unconfigured-secret"',
        'market = ["unconfigured-secret"]',
        'market = "unconfigured-secret',
    ],
)
def test_bad_config_does_not_echo_raw_values(tmp_path, content):
    path = tmp_path / "bad.toml"
    path.write_text(content)
    result = runner.invoke(app, ["--config", str(path), "doctor"])
    assert result.exit_code == 1
    assert "unconfigured-secret" not in result.output
    assert "only market US is supported" in result.output


def test_missing_explicit_config_and_unsupported_market(tmp_path, monkeypatch):
    with pytest.raises(ConfigurationError, match="does not exist"):
        load_runtime(tmp_path / "missing.toml")
    result = runner.invoke(app, ["--market", "IN", "doctor"])
    assert result.exit_code == 1
    assert "Only US is enabled" in result.output
    monkeypatch.setenv("STOCK_ANALYSER_MARKET", "IN")
    with pytest.raises(ConfigurationError, match="Unsupported market"):
        load_runtime()


def test_init_failure_is_clear_and_redacted(tmp_path, monkeypatch):
    target = tmp_path / "sensitive-provider-key"
    target.write_text("blocks directory creation")
    monkeypatch.setenv("MASSIVE_API_KEY", "sensitive-provider-key")
    result = runner.invoke(app, ["--data-dir", str(target), "init"])
    assert result.exit_code == 1
    assert "sensitive-provider-key" not in result.output
    assert "[REDACTED]" in result.output


def test_redaction_covers_values_headers_and_query_strings():
    raw = (
        "provider failed with live-secret; apiKey=unknown-secret&symbol=AAPL; "
        'Authorization: Bearer bearer-secret; "password": "another secret"'
    )
    safe = redact(raw, {"MASSIVE_API_KEY": "live-secret"})
    for secret in ("live-secret", "unknown-secret", "bearer-secret", "another secret"):
        assert secret not in safe
    assert "symbol=AAPL" in safe


def test_logs_redact_format_arguments_and_tracebacks(monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-sensitive-value")
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.Logger("test")
    logger.addHandler(handler)
    try:
        raise ValueError("provider returned aws-sensitive-value api_key=unknown-key")
    except ValueError:
        logger.exception("Request credential: %s", "aws-sensitive-value")
    assert "aws-sensitive-value" not in output.getvalue()
    assert "unknown-key" not in output.getvalue()
    assert "ValueError" in output.getvalue()


@pytest.mark.parametrize(
    "secret", ['prefix"private-tail', r"prefix\private-tail", 'pré"private-tail\n']
)
def test_doctor_redacts_credentials_before_json_serialization(monkeypatch, tmp_path, secret):
    monkeypatch.setenv("MASSIVE_API_KEY", secret)
    result = runner.invoke(app, ["--data-dir", str(tmp_path / secret), "doctor"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["runtime"]["data_dir"] == str(tmp_path / "[REDACTED]")
    assert "private-tail" not in result.output


@pytest.mark.parametrize(
    "secret", ['prefix"private-tail', r"prefix\private-tail", 'pré"private-tail\n']
)
def test_logs_redact_serialized_and_structured_values(monkeypatch, secret):
    monkeypatch.setenv("MASSIVE_API_KEY", secret)
    output = io.StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(RedactingFormatter("%(message)s"))
    logger = logging.Logger("escaped-credentials")
    logger.addHandler(handler)
    logger.info({"response": [secret]})
    for ensure_ascii in (True, False):
        logger.info("response: %s", json.dumps({"message": secret}, ensure_ascii=ensure_ascii))
    assert "private-tail" not in output.getvalue()
    assert output.getvalue().count("[REDACTED]") == 3


def test_redaction_handles_unknown_credentials_with_escaped_quotes():
    payload = json.dumps({"api_key": 'prefix"private-tail', "symbol": "AAPL"})
    safe = redact(payload, {})
    assert "private-tail" not in safe
    assert json.loads(safe) == {"api_key": "[REDACTED]", "symbol": "AAPL"}


def test_utf8_configuration_loads_under_ascii_locale(tmp_path):
    config = tmp_path / "config.toml"
    # Exercise UTF-8 decoding without requiring an ASCII filesystem encoding
    # to represent a Unicode path (which fails during path resolution on Linux).
    config.write_bytes('# café\n[paths]\nreports_dir = "reports"\n'.encode())
    env = {key: value for key, value in os.environ.items() if not key.startswith("STOCK_ANALYSER_")}
    env.update(LC_ALL="C", PYTHONUTF8="0", PYTHONCOERCECLOCALE="0")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys; from pathlib import Path; "
                "from stock_analyser.config import load_runtime; "
                "print(json.dumps(str(load_runtime(Path(sys.argv[1])).reports_dir)))"
            ),
            str(config),
        ],
        env=env,
        capture_output=True,
        text=True,
        encoding="ascii",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == str(tmp_path / "reports")


def test_utf8_configuration_preserves_unicode_paths(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[paths]\nreports_dir = "café"\n', encoding="utf-8")
    assert load_runtime(config).reports_dir == tmp_path / "café"


def test_example_configuration_is_valid():
    assert load_runtime(Path("config.example.toml")).market == "US"


def test_source_provenance_when_git_is_unavailable(monkeypatch):
    def unavailable(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("stock_analyser.version.subprocess.check_output", unavailable)
    assert source_version() == {"package_version": "0.1.0", "commit": None, "dirty": None}


@pytest.mark.parametrize(
    "arguments", [["-c", "from stock_analyser.cli import app"], ["-m", "stock_analyser", "--help"]]
)
def test_startup_without_system_timezone_data(arguments):
    result = subprocess.run(
        [sys.executable, *arguments],
        env={**os.environ, "PYTHONTZPATH": ""},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
