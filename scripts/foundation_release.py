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

from stock_analyser.qualification import evaluate_qualification

ROOT = Path(__file__).resolve().parents[1]
MANDATORY_CHECKLIST = "tests/test_persistence.py::test_foundation_release_fresh_process_checklist"


def suite_environment(output):
    env = {**os.environ, "STOCK_ANALYSER_ACCEPTANCE_EVIDENCE": str(output / "reopen.json")}
    # Local pytest preferences must not narrow a release acceptance run.
    env.pop("PYTEST_ADDOPTS", None)
    return env


def collect_suite(env):
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "--collect-only", "-q", "tests"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode != 0:
        return []
    return [
        line.strip()
        for line in result.stdout.splitlines()
        if line.startswith("tests/") and "::" in line
    ]


def fixture_acceptance(xml_path, expected, reopen_path):
    result = fixture_result(xml_path) if xml_path.exists() else {"passed": False}
    actual = []
    if xml_path.exists():
        for case in ET.parse(xml_path).getroot().iter("testcase"):
            # JUnit classnames encode module/class scope with dots.
            scope = case.get("classname", "").split(".")
            module = next(
                (i for i, part in enumerate(scope) if part.startswith("Test")), len(scope)
            )
            actual.append(
                "/".join(scope[:module])
                + ".py::"
                + "::".join([*scope[module:], case.get("name", "")])
            )
    result["expected_tests"] = len(expected)
    result["complete_suite"] = MANDATORY_CHECKLIST in expected and sorted(actual) == sorted(
        expected
    )
    result["fresh_process_checklist"] = False
    try:
        evidence = json.loads(reopen_path.read_text())
        reports = evidence["reports"]
        result["fresh_process_checklist"] = (
            evidence["configuration_migrations_trade_confirmation_resume"] == "PASS"
            and len(reports) == 3
            and sorted(report["mode"] for report in reports) == ["buy", "sell", "sell"]
            and all(
                report["fresh_process_reopen"] is True
                and set(report["artifacts"]) == {"manifest.json", "report.json", "report.md"}
                and all(
                    len(value) == 64 and all(c in "0123456789abcdef" for c in value)
                    for value in report["artifacts"].values()
                )
                for report in reports
            )
        )
        result["reopen_sha256"] = hashlib.sha256(reopen_path.read_bytes()).hexdigest()
    except (OSError, ValueError, KeyError, TypeError):
        pass
    result["passed"] = (
        result["passed"] and result["complete_suite"] and result["fresh_process_checklist"]
    )
    return result


def fixture_result(xml_path):
    cases = list(ET.parse(xml_path).getroot().iter("testcase"))
    counts = {
        name: sum(case.find(name) is not None for case in cases)
        for name in ("failure", "error", "skipped")
    }
    return {"tests": len(cases), **counts, "passed": bool(cases) and not any(counts.values())}


def release_gate(fixtures, live, sector, *, now=None):
    now = now or datetime.now(UTC)
    blockers = []
    if not fixtures.get("passed"):
        blockers.append("FIXTURE_ACCEPTANCE_NOT_PASSED")
    if live.get("status") != "OBSERVED":
        blockers.append("LIVE_SMOKE_NOT_OBSERVED")
    gate = evaluate_qualification(sector, now=now, live=True)
    if not gate["full_sector_accepted"]:
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
    parser.add_argument(
        "--qualification-evidence",
        type=Path,
        help="Explicit v2 aggregate evidence; recomputed at release time",
    )
    args = parser.parse_args()
    if not os.environ.get("STOCK_ANALYSER_TEST_DATABASE_URL"):
        parser.error("Set STOCK_ANALYSER_TEST_DATABASE_URL to a disposable PostgreSQL server")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    env = suite_environment(output)
    expected = collect_suite(env)
    xml = output / "pytest.xml"
    checks = run(
        [sys.executable, "-m", "pytest", "-o", "addopts=", "-q", "tests", "--junitxml", str(xml)],
        env=env,
    )
    fixtures = fixture_acceptance(xml, expected, output / "reopen.json")
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
    if args.qualification_evidence:
        previous_sector = args.qualification_evidence.resolve()
        sector = json.loads(previous_sector.read_text())
        sector_source = {
            "kind": "explicit_qualification",
            "reference": str(previous_sector),
            "sha256": hashlib.sha256(previous_sector.read_bytes()).hexdigest(),
        }
    if args.live:
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
        "pilot_sector": evaluate_qualification(sector, now=datetime.now(UTC), live=True),
        "pilot_sector_source": sector_source,
        "milestone": release_gate(fixtures, live, sector),
        "limitations": "Data readiness only; no recommendation or production reliability claim",
    }
    (output / "release.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))
    return 0 if fixtures["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
