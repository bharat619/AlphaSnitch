"""Source provenance without requiring Git in installed distributions."""

import subprocess
from pathlib import Path

from stock_analyser import __version__


def source_version() -> dict[str, str | bool | None]:
    result: dict[str, str | bool | None] = {
        "package_version": __version__,
        "commit": None,
        "dirty": None,
    }
    root = Path(__file__).resolve().parents[2]
    if not (root / ".git").exists():
        return result
    try:
        result["commit"] = subprocess.check_output(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=3,
        ).strip()
        result["dirty"] = bool(
            subprocess.check_output(
                ["git", "-C", str(root), "status", "--porcelain"],
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=3,
            ).strip()
        )
    except (OSError, subprocess.SubprocessError):
        pass
    return result
