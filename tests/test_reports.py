"""Publication must never expose a partially written individual artifact."""

import os

import pytest

from stock_analyser.reports import atomic_write


def test_failed_atomic_rename_preserves_old_file_and_cleans_temporary(monkeypatch, tmp_path):
    target = tmp_path / "report.md"
    target.write_bytes(b"previous complete report")

    def fail_rename(source, destination):
        assert source.parent == destination.parent
        assert source.read_bytes() == b"replacement"
        assert destination.read_bytes() == b"previous complete report"
        raise OSError("Injected rename failure")

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_rename)
        with pytest.raises(OSError, match="Injected"):
            atomic_write(target, b"replacement")
    assert target.read_bytes() == b"previous complete report"
    assert list(tmp_path.iterdir()) == [target]
    atomic_write(target, b"replacement")
    assert target.read_bytes() == b"replacement"


def test_legacy_report_stage_serialization_survives_resume_fields():
    """New progress fields must not change regenerated historical report bytes."""
    from stock_analyser.persistence.records import StageRecord

    legacy = {
        "schema_version": "1",
        "run_id": "d22943d4-d4b9-4ce6-9ab5-ac4aa3d5f453",
        "name": "collection",
        "instrument_id": None,
        "status": "SUCCEEDED",
        "updated_at": "2026-10-16T21:00:00Z",
        "reason": None,
    }
    assert StageRecord.model_validate(legacy).model_dump(mode="json") == legacy
