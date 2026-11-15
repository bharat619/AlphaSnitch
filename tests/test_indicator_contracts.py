from datetime import timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from stock_analyser.indicator_example import freeze_example
from stock_analyser.indicators import InputProvenance, ObservationSpec, PublicationEvidence, observe


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "5", None])
@pytest.mark.parametrize("field", ["price_lookback", "volume_lookback", "regular_session_seconds"])
def test_explicit_positive_integer_parameters(field, value):
    parameters = dict(
        feature_id="prior_high", price_lookback=5, volume_lookback=5, regular_session_seconds=21600
    )
    parameters[field] = value
    with pytest.raises(ValidationError):
        ObservationSpec(**parameters)


def test_no_implicit_lookback_or_duration():
    with pytest.raises(ValidationError):
        ObservationSpec(feature_id="relative_volume")


def test_verified_assertions_need_audit_references():
    with pytest.raises(ValidationError):
        InputProvenance(volume_adjustment="split_consistent")
    with pytest.raises(ValidationError):
        PublicationEvidence(session="2026-10-30", verified_at="2026-10-30T20:10:00Z")


def test_contract_and_semantic_replay(tmp_path):
    snapshot, provenance, calendar, stamp = freeze_example(tmp_path)
    spec = ObservationSpec(
        feature_id="relative_volume",
        price_lookback=5,
        volume_lookback=5,
        regular_session_seconds=21600,
    )
    result = observe(
        snapshot, spec, provenance, calendar, stamp.date(), as_of=stamp, calculated_at=stamp
    )
    replay = observe(
        snapshot,
        spec,
        provenance,
        calendar,
        stamp.date(),
        as_of=stamp,
        calculated_at=stamp + timedelta(seconds=3),
    )
    assert result.value == Decimal("1.6")
    assert result.semantic_bytes() == replay.semantic_bytes()
    assert result.model_dump_json() != replay.model_dump_json()
    assert result.units == "ratio"
    assert result.snapshot_id == snapshot.metadata.snapshot_id
    assert result.snapshot_sha256 == snapshot.sha256
    assert result.contributors[-1].session == stamp.date()
    assert all(r.first_proven_at <= result.available_at for r in result.contributors)
    assert result.available_at == stamp
    assert result.specification.formula_version == "prior-session-1"


def test_provenance_bound_to_exact_snapshot(tmp_path):
    snapshot, provenance, calendar, stamp = freeze_example(tmp_path)
    provenance = provenance.model_copy(update={"snapshot_sha256": "0" * 64})
    result = observe(
        snapshot,
        ObservationSpec(
            feature_id="prior_high",
            price_lookback=5,
            volume_lookback=5,
            regular_session_seconds=21600,
        ),
        provenance,
        calendar,
        stamp.date(),
        as_of=stamp,
        calculated_at=stamp,
    )
    assert result.value is None
    assert "INCOMPATIBLE_PROVENANCE" in result.reasons
