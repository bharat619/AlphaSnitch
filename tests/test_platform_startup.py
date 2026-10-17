"""Platform locking must not be needed to import or use offline CLI commands."""

import os
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "arguments",
    [
        ["--help"],
        ["doctor"],
        ["report", "--help"],
        ["report", "show", "00000000-0000-0000-0000-000000000001"],
    ],
)
def test_offline_cli_without_fcntl(tmp_path, arguments):
    script = """
import importlib.abc
import sys

class NoFcntl(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "fcntl":
            raise ModuleNotFoundError("fcntl unavailable on Windows")

# Python's own subprocess implementation may probe these modules. Load it before
# restricting imports so this test isolates the application's import boundary.
import subprocess
sys.modules.pop("fcntl", None)
sys.meta_path.insert(0, NoFcntl())
from stock_analyser.cli import main
main()
"""
    result = subprocess.run(
        [sys.executable, "-c", script, "--data-dir", str(tmp_path), *arguments],
        cwd=tmp_path,
        env={k: v for k, v in os.environ.items() if not k.startswith("STOCK_ANALYSER_")},
        capture_output=True,
        text=True,
        timeout=20,
    )
    if arguments[:2] == ["report", "show"]:
        assert result.returncode == 1
        assert "Set STOCK_ANALYSER_DATABASE_URL" in result.stdout
    else:
        assert result.returncode == 0, result.stderr
    assert "fcntl unavailable on Windows" not in result.stdout + result.stderr
