"""Reproducible release checklist. Run with the locked development environment.

The fixture command exits zero only with no failures/errors/skips. Release completion
is a separate, fail-closed decision: fixture success cannot qualify a live sector.
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def fixture_result(xml_path):
    cases = list(ET.parse(xml_path).getroot().iter("testcase"))
    counts = {
        name: sum(case.find(name) is not None for case in cases)
        for name in ("failure", "error", "skipped")
    }
    return {"tests": len(cases), **counts, "passed": bool(cases) and not any(counts.values())}


def release_gate(fixtures, live, sector):
    blockers = []
    if not fixtures.get("passed"):
        blockers.append("FIXTURE_ACCEPTANCE_NOT_PASSED")
    if live.get("status") != "OBSERVED":
        blockers.append("LIVE_SMOKE_NOT_OBSERVED")
    # The current qualifier deliberately never grants full-sector acceptance.
    # Keep every independent blocker visible even if a summary boolean changes.
    gate = sector.get("gate", {})
    if (
        gate.get("status") != "PASS"
        or gate.get("full_sector_accepted") is not True
        or gate.get("blockers") != []
    ):
        blockers.append("PILOT_SECTOR_COVERAGE_NOT_QUALIFIED")
    return {"complete": not blockers, "blockers": blockers}


def run(command, *, env, timeout=1800):
    try:
        result = subprocess.run(command, cwd=ROOT, env=env, timeout=timeout, check=False)
        return {"exit_code": result.returncode, "timed_out": False}
    except subprocess.TimeoutExpired:
        return {"exit_code": None, "timed_out": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="New evidence directory; existing directories are refused",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Opt in to live AAPL/SPY and bounded sector qualification",
    )
    args = parser.parse_args()
    if not os.environ.get("STOCK_ANALYSER_TEST_DATABASE_URL"):
        parser.error("Set STOCK_ANALYSER_TEST_DATABASE_URL to a disposable PostgreSQL server")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = {**os.environ, "STOCK_ANALYSER_ACCEPTANCE_EVIDENCE": str(output / "reopen.json")}
    xml = output / "pytest.xml"
    checks = run([sys.executable, "-m", "pytest", "-q", "--junitxml", str(xml)], env=env)
    fixtures = fixture_result(xml) if xml.exists() else {"passed": False}
    fixtures["process"] = checks
    fixtures["passed"] = fixtures["passed"] and checks["exit_code"] == 0
    live = {"status": "NOT_RUN", "publication_timing": "unverified"}
    previous_sector = ROOT / "docs/evidence/technology-universe-2026-10-24.json"
    sector = json.loads(previous_sector.read_text())
    sector_source = {
        "kind": "previous_observation",
        "reference": str(previous_sector.relative_to(ROOT)),
        "sha256": hashlib.sha256(previous_sector.read_bytes()).hexdigest(),
    }
    if args.live:
        sector = {}
        sector_source = {"kind": "current_run", "reference": "sector/findings.json"}
        # Live writes use a separate runtime. Explicit opt-in is required and the
        # application DB must be chosen by the operator, never inferred from tests.
        if not os.environ.get("STOCK_ANALYSER_DATABASE_URL"):
            live["status"] = "DATABASE_NOT_CONFIGURED"
        else:
            prefix = [
                sys.executable,
                "-m",
                "stock_analyser",
                "--data-dir",
                str(output / "live-runtime"),
            ]
            smoke = run(
                [*prefix, "collect-prices", "--cache-mode", "refresh"], env=env, timeout=180
            )
            live = {
                "status": "OBSERVED" if smoke["exit_code"] == 0 else "FAILED",
                "process": smoke,
                "publication_timing": "unverified",
                "scope": "AAPL/SPY only; not publication or sector readiness",
            }
            run(
                [
                    *prefix,
                    "qualify-universe",
                    "--output",
                    str(output / "sector"),
                    "--max-pages",
                    "2",
                    "--profile-limit",
                    "2",
                ],
                env=env,
                timeout=180,
            )
            findings = output / "sector" / "findings.json"
            if findings.exists():
                sector = json.loads(findings.read_text())
    evidence = {
        "schema_version": "1",
        "recorded_at": datetime.now(UTC).isoformat(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_tracked_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True
            ).strip()
        ),
        "lock_sha256": hashlib.sha256((ROOT / "uv.lock").read_bytes()).hexdigest(),
        "fixture_readiness": fixtures,
        "live_observation": live,
        "pilot_sector": sector.get("gate", {"status": "NOT_RUN"}),
        "pilot_sector_source": sector_source,
        "milestone": release_gate(fixtures, live, sector),
        "limitations": "Data readiness only; no recommendation or production reliability claim",
    }
    (output / "release.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    return 0 if fixtures["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
