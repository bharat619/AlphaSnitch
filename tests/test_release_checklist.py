"""Release gates cannot confuse fixture success with live qualification."""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "foundation_release", Path(__file__).resolve().parents[1] / "scripts/foundation_release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_inherited_pytest_filter_cannot_narrow_release_suite(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTEST_ADDOPTS", "tests/test_release_checklist.py -k fixture_pass")
    env = release.suite_environment(tmp_path)
    assert "PYTEST_ADDOPTS" not in env
    expected = release.collect_suite(env)
    assert release.MANDATORY_CHECKLIST in expected
    assert (
        "tests/test_contracts.py::test_same_ticker_isolated_by_market_currency_provider_and_cutoff"
        in expected
    )


def test_subset_or_missing_reopen_cannot_pass_acceptance(tmp_path):
    xml = tmp_path / "tests.xml"
    reopen = tmp_path / "reopen.json"
    mandatory_case = (
        '<testcase classname="tests.test_persistence" '
        'name="test_foundation_release_fresh_process_checklist"/>'
    )
    xml.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_release_checklist" '
        'name="test_fixture_pass_never_implies_live_or_pilot_acceptance"/>'
        "</testsuite></testsuites>"
    )
    expected = [release.MANDATORY_CHECKLIST]
    assert release.fixture_acceptance(xml, expected, reopen)["passed"] is False
    xml.write_text(f"<testsuites><testsuite>{mandatory_case}</testsuite></testsuites>")
    result = release.fixture_acceptance(xml, expected, reopen)
    assert result["complete_suite"] is True
    assert result["passed"] is False
    evidence = {
        "configuration_migrations_trade_confirmation_resume": "PASS",
        "reports": [
            {
                "mode": mode,
                "fresh_process_reopen": True,
                "artifacts": {
                    name: "a" * 64 for name in ("manifest.json", "report.json", "report.md")
                },
            }
            for mode in ("buy", "sell", "sell")
        ],
    }
    reopen.write_text(json.dumps(evidence))
    assert release.fixture_acceptance(xml, expected, reopen)["passed"] is True
    # Correct evidence cannot compensate for a subset of the collected tests.
    assert (
        release.fixture_acceptance(xml, [*expected, "tests/test_other.py::test_other"], reopen)[
            "passed"
        ]
        is False
    )
    evidence["reports"][0]["fresh_process_reopen"] = False
    reopen.write_text(json.dumps(evidence))
    assert release.fixture_acceptance(xml, expected, reopen)["passed"] is False


def test_fixture_skips_and_missing_tests_do_not_clear_acceptance(tmp_path):
    xml = tmp_path / "tests.xml"
    for contents in (
        "<testsuites><testsuite><testcase><skipped/></testcase></testsuite></testsuites>",
        "<testsuites><testsuite><testcase><failure/></testcase></testsuite></testsuites>",
        "<testsuites><testsuite><testcase><error/></testcase></testsuite></testsuites>",
        "<testsuites/>",
    ):
        xml.write_text(contents)
        assert release.fixture_result(xml)["passed"] is False
    xml.write_text("<testsuites><testsuite><testcase/></testsuite></testsuites>")
    assert release.fixture_result(xml)["passed"] is True


def test_fixture_pass_never_implies_live_or_pilot_acceptance():
    gate = release.release_gate({"passed": True}, {"status": "NOT_RUN"}, {})
    assert gate == {
        "complete": False,
        "blockers": ["LIVE_SMOKE_NOT_OBSERVED", "PILOT_SECTOR_COVERAGE_NOT_QUALIFIED"],
    }
    sector = {
        "gate": {
            "status": "PASS",
            "full_sector_accepted": True,
            "blockers": ["RETENTION_RIGHTS_UNVERIFIED"],
        }
    }
    assert (
        release.release_gate({"passed": True}, {"status": "OBSERVED"}, sector)["complete"] is False
    )
    sector["gate"]["blockers"] = []
    assert (
        release.release_gate({"passed": True}, {"status": "OBSERVED"}, sector)["complete"] is True
    )
