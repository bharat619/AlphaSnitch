"""FIFO arithmetic and strict confirmation boundary tests."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from stock_analyser.contracts import Instrument
from stock_analyser.ledger import Fee, TradeConfirmation, fifo, validate_instrument


def trade(side="BUY", quantity="10", price="100", **kwargs):
    return TradeConfirmation(
        instrument_id=UUID_INSTRUMENT,
        venue="XNAS",
        side=side,
        quantity=quantity,
        price=price,
        currency="USD",
        executed_at=datetime(2025, 1, 1, tzinfo=UTC),
        idempotency_key=str(uuid4()),
        **kwargs,
    )


UUID_INSTRUMENT = uuid4()


def test_fifo_partial_multiple_lots_and_full_close():
    history = [(uuid4(), trade()), (uuid4(), trade("SELL", "4", "110"))]
    position = fifo(history)
    assert (position.quantity, position.cost_basis, position.realized_pnl) == (6, 600, 40)
    with pytest.raises(ValueError, match="Insufficient"):
        fifo([*history, (uuid4(), trade("SELL", "7"))])
    history += [(uuid4(), trade(quantity="2", price="200")), (uuid4(), trade("SELL", "7", "120"))]
    position = fifo(history)
    assert (position.quantity, position.cost_basis, position.realized_pnl) == (1, 200, 80)
    position = fifo([*history, (uuid4(), trade("SELL", "1", "250"))])
    assert (position.quantity, position.cost_basis, position.realized_pnl) == (0, 0, 130)


def test_fee_currency_and_fx_and_residual_basis():
    unknown = Fee(amount="3", currency="EUR")
    position = fifo([(uuid4(), trade(fees=(unknown,)))])
    assert position.quantity == 10 and position.cost_basis is None
    assert not position.accounting_complete
    converted = Fee(amount="3", currency="EUR", fx_rate="2")
    history = [
        (uuid4(), trade(quantity="3", price="0.1", fees=(converted,))),
        (uuid4(), trade("SELL", "1", "3", fees=(Fee(amount="0.1", currency="USD"),))),
        (uuid4(), trade("SELL", "2", "3")),
    ]
    position = fifo(history)
    assert position.cost_basis == 0 and position.realized_pnl == Decimal("2.6")
    assert position.accounting_complete
    position = fifo([(uuid4(), trade()), (uuid4(), trade("SELL", "10", fees=(unknown,)))])
    assert position.cost_basis == 0 and position.realized_pnl is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("quantity", "0"),
        ("quantity", "-1"),
        ("price", "0"),
        ("price", "NaN"),
        ("quantity", "0.000000001"),
        ("quantity", 0.1),
        ("price", "Infinity"),
        ("currency", "usd"),
        ("currency", "ZZZ"),
        ("fees", [{"amount": 0.1, "currency": "USD"}]),
        ("fees", [{"amount": "1", "currency": "ZZZ"}]),
        ("currency", "US"),
        ("executed_at", "2025-01-01T00:00:00"),
        ("executed_at", "2100-01-01T00:00:00Z"),
        ("idempotency_key", " "),
        ("fees", [{"amount": "-1", "currency": "USD"}]),
    ],
)
def test_invalid_confirmations(field, value):
    payload = trade().model_dump(mode="json")
    payload[field] = value
    with pytest.raises(ValidationError):
        TradeConfirmation.model_validate(payload)


def test_instrument_currency_venue_precision():
    instrument = Instrument(
        instrument_id=UUID_INSTRUMENT,
        security_id=uuid4(),
        market_id="US",
        venue="XNAS",
        symbol="TEST",
        quote_currency="USD",
        quantity_increment="1",
        price_increment="0.01",
    )
    validate_instrument(trade(), instrument)
    with pytest.raises(ValueError, match="quantity"):
        validate_instrument(trade(quantity="0.1"), instrument)
    with pytest.raises(ValueError, match="price"):
        validate_instrument(trade(price="1.001"), instrument)
    with pytest.raises(ValueError, match="venue/currency"):
        validate_instrument(trade(), instrument.model_copy(update={"quote_currency": "INR"}))


def test_actual_base_instrument_and_universe_payloads_keep_immutable_identity():
    """Fixture generated using the exact base commit, not reconstructed by new models."""
    import json
    from pathlib import Path

    from stock_analyser.persistence.repositories import instrument_revision
    from stock_analyser.universe import UniverseSnapshot, replay_universe

    legacy = json.loads(
        (Path(__file__).parent / "fixtures" / "us014-legacy-ledger-inputs.json").read_text()
    )
    assert legacy["source_commit"] == "68cd8b6326f76d74eca50060f5732e0b0968208e"
    instrument = Instrument.model_validate(legacy["instrument"])
    assert instrument.model_dump(mode="json") == legacy["instrument"]
    assert instrument_revision(instrument) == legacy["instrument_revision"]
    snapshot = UniverseSnapshot.model_validate(legacy["snapshot"])
    assert snapshot.model_dump(mode="json") == legacy["snapshot"]
    assert replay_universe(snapshot) == snapshot


def test_nondefault_instrument_precision_is_serialized_and_changes_identity():
    from stock_analyser.persistence.repositories import instrument_revision

    instrument = Instrument(
        instrument_id=uuid4(),
        security_id=uuid4(),
        market_id="US",
        venue="XNAS",
        symbol="CUSTOM",
        quote_currency="USD",
    )
    custom = instrument.model_copy(
        update={"quantity_increment": Decimal("1"), "price_increment": Decimal("0.01")}
    )
    payload = custom.model_dump(mode="json")
    assert payload["quantity_increment"] == "1"
    assert payload["price_increment"] == "0.01"
    assert Instrument.model_validate(payload) == custom
    assert instrument_revision(custom) != instrument_revision(instrument)


@pytest.mark.parametrize("ratio", [None, "0", "-1", 2.0, "Infinity"])
def test_split_requires_valid_decimal_ratio(ratio):
    from stock_analyser.ledger import CorporateAction

    with pytest.raises(ValueError):
        CorporateAction(
            instrument_id=UUID_INSTRUMENT,
            kind="SPLIT",
            ratio=ratio,
            effective_at=datetime(2025, 1, 1, tzinfo=UTC),
            reason="Synthetic",
            idempotency_key="split",
        )


def test_split_preserves_each_lot_basis_and_precedes_same_timestamp_sale():
    from stock_analyser.ledger import CorporateAction

    split = CorporateAction(
        instrument_id=UUID_INSTRUMENT,
        kind="SPLIT",
        ratio="2",
        effective_at=datetime(2025, 1, 1, tzinfo=UTC),
        reason="Synthetic",
        idempotency_key="split",
    )
    buy = trade().model_copy(update={"executed_at": datetime(2024, 12, 31, tzinfo=UTC)})
    position = fifo([(uuid4(), buy), (uuid4(), trade("SELL", "15", "60"))], [(uuid4(), split)])
    assert (position.quantity, position.cost_basis, position.realized_pnl) == (5, 250, 150)
