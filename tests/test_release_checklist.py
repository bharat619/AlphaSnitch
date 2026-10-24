"""Release gates cannot confuse fixture success with live qualification."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "foundation_release", Path(__file__).resolve().parents[1] / "scripts/foundation_release.py"
)
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


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
