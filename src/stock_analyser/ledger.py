"""Confirmed offline executions and deterministic FIFO portfolio bookkeeping.

Decimals are JSON strings. FIFO uses execution time, then ledger insertion order.
Proportional fee allocation uses 80-digit arithmetic; final closes consume the
remaining basis exactly. Unknown FX propagates an unavailable accounting result.
"""

from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Annotated, Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import AwareDatetime, BeforeValidator, Field, model_validator

from stock_analyser.contracts import Contract, Currency, Instrument, Text
from stock_analyser.persistence import schema as s

# Explicitly supported ISO currencies; expand this reviewed list as markets require.
CURRENCIES = frozenset(
    (
        "USD EUR INR GBP JPY CAD AUD CHF CNY HKD SGD NZD KRW TWD BRL MXN ZAR SEK "
        "NOK DKK PLN CZK HUF ILS AED SAR TRY THB IDR MYR PHP VND PKR BDT LKR NPR "
        "BHD KWD OMR QAR RUB UAH CLP COP PEN ARS ISK RON BGN RSD GEL NGN KES EGP "
        "MAD TND GHS UGX TZS XOF XAF XPF XCD BMD BSD BBD JMD TTD DOP GTQ HNL NIO "
        "CRC PAB UYU BOB PYG MUR SCR MVR BND MOP KHR LAK MMK KZT UZS AZN ALL BAM "
        "MKD MDL DZD IQD JOD LBP FJD PGK WST TOP VUV SBD"
    ).split()
)


def decimal_input(value):
    if isinstance(value, (float, bool)):
        raise ValueError("Use decimal strings, not floating point values")
    return value


def iso_currency(value):
    if not isinstance(value, str) or value not in CURRENCIES:
        raise ValueError("Unsupported ISO currency")
    return value


LedgerCurrency = Annotated[Currency, BeforeValidator(iso_currency)]
Amount = Annotated[
    Decimal, BeforeValidator(decimal_input), Field(max_digits=28, decimal_places=8, ge=0)
]
Positive = Annotated[
    Decimal, BeforeValidator(decimal_input), Field(max_digits=28, decimal_places=8, gt=0)
]


class Fee(Contract):
    amount: Amount
    currency: LedgerCurrency
    description: Text = "fee"
    # Execution-currency units per one fee-currency unit, explicitly supplied.
    fx_rate: Positive | None = None


class TradeConfirmation(Contract):
    instrument_id: UUID
    venue: Text
    side: Literal["BUY", "SELL"]
    quantity: Positive
    price: Positive
    currency: LedgerCurrency
    fees: tuple[Fee, ...] = ()
    executed_at: AwareDatetime
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
    recommendation_id: Text | None = None
    run_id: UUID | None = None

    @model_validator(mode="after")
    def timestamp_valid(self):
        if self.executed_at > datetime.now(UTC):
            raise ValueError("Execution timestamp cannot be in the future")
        return self


class Lot(Contract):
    execution_id: UUID
    quantity: Decimal
    cost_basis: Decimal | None


class Position(Contract):
    instrument_id: UUID
    venue: Text
    currency: LedgerCurrency
    quantity: Decimal
    cost_basis: Decimal | None
    realized_pnl: Decimal | None
    accounting_complete: bool
    lots: tuple[Lot, ...]


class ExecutionReceipt(Contract):
    execution_id: UUID
    confirmation: TradeConfirmation
    position: Position


def _fees(trade):
    total = Decimal(0)
    for fee in trade.fees:
        if fee.currency == trade.currency:
            total += fee.amount
        elif fee.amount and fee.fx_rate is None:
            return None
        else:
            total += fee.amount * (fee.fx_rate or Decimal(1))
    return total


def fifo(trades: list[tuple[UUID, TradeConfirmation]]) -> Position:
    """Replay all executions; reject any negative holding, including backdated sales."""
    if not trades:
        raise ValueError("No executions")
    lots = []
    realized = Decimal(0)
    with localcontext() as context:
        context.prec = 80
        for execution_id, trade in sorted(trades, key=lambda item: item[1].executed_at):
            fees = _fees(trade)
            if trade.side == "BUY":
                basis = None if fees is None else trade.quantity * trade.price + fees
                lots.append(
                    Lot(execution_id=execution_id, quantity=trade.quantity, cost_basis=basis)
                )
                continue
            if sum((lot.quantity for lot in lots), Decimal(0)) < trade.quantity:
                raise ValueError("Insufficient shares for sale at execution timestamp")
            remaining = trade.quantity
            basis = Decimal(0)
            while remaining:
                lot = lots[0]
                used = min(remaining, lot.quantity)
                allocated = (
                    None
                    if lot.cost_basis is None
                    else lot.cost_basis
                    if used == lot.quantity
                    else lot.cost_basis * used / lot.quantity
                )
                basis = None if basis is None or allocated is None else basis + allocated
                remaining -= used
                if used == lot.quantity:
                    lots.pop(0)
                else:
                    lots[0] = Lot(
                        execution_id=lot.execution_id,
                        quantity=lot.quantity - used,
                        cost_basis=None if allocated is None else lot.cost_basis - allocated,
                    )
            if realized is None or basis is None or fees is None:
                realized = None
            else:
                realized += trade.quantity * trade.price - fees - basis
        basis = (
            None
            if any(lot.cost_basis is None for lot in lots)
            else sum((lot.cost_basis for lot in lots), Decimal(0))
        )
        return Position(
            instrument_id=trade.instrument_id,
            venue=trade.venue,
            currency=trade.currency,
            quantity=sum((lot.quantity for lot in lots), Decimal(0)),
            cost_basis=basis,
            realized_pnl=realized,
            accounting_complete=basis is not None and realized is not None,
            lots=tuple(lots),
        )


def _history(connection, instrument_id=None):
    query = sa.select(s.executions).order_by(s.executions.c.sequence)
    if instrument_id is not None:
        query = query.where(s.executions.c.instrument_id == instrument_id)
    return [ExecutionReceipt.model_validate(row.payload) for row in connection.execute(query)]


def record_execution(database, confirmation: TradeConfirmation) -> ExecutionReceipt:
    """Called only after user confirmation; share validation and insertion are atomic."""
    confirmation = TradeConfirmation.model_validate(confirmation.model_dump(mode="json"))
    with database.write() as repo:
        previous = repo.connection.scalar(
            sa.select(s.executions.c.payload).where(
                s.executions.c.idempotency_key == confirmation.idempotency_key
            )
        )
        if previous is not None:
            receipt = ExecutionReceipt.model_validate(previous)
            if receipt.confirmation != confirmation:
                raise ValueError("Idempotency key already used for a different execution")
            return receipt
        instrument = repo.get_instrument(confirmation.instrument_id)
        validate_instrument(confirmation, instrument)
        execution_id = uuid4()
        history = _history(repo.connection, confirmation.instrument_id)
        position = fifo(
            [
                *((r.execution_id, r.confirmation) for r in history),
                (execution_id, confirmation),
            ]
        )
        receipt = ExecutionReceipt(
            execution_id=execution_id, confirmation=confirmation, position=position
        )
        repo.connection.execute(
            sa.insert(s.executions).values(
                execution_id=execution_id,
                instrument_id=confirmation.instrument_id,
                idempotency_key=confirmation.idempotency_key,
                payload=receipt.model_dump(mode="json"),
            )
        )
        return receipt


def validate_instrument(confirmation, instrument: Instrument):
    if confirmation.venue != instrument.venue or confirmation.currency != instrument.quote_currency:
        raise ValueError("Execution venue/currency must match instrument")
    for name, increment in (
        ("quantity", instrument.quantity_increment),
        ("price", instrument.price_increment),
    ):
        with localcontext() as context:
            context.prec = 80
            if getattr(confirmation, name) % increment:
                raise ValueError(f"Execution {name} must be a multiple of {increment}")


def portfolio(database) -> list[Position]:
    with database.read() as repo:
        grouped = {}
        for receipt in _history(repo.connection):
            grouped.setdefault(receipt.confirmation.instrument_id, []).append(
                (receipt.execution_id, receipt.confirmation)
            )
        return [fifo(trades) for trades in grouped.values()]


def execution_receipt(database, key: str) -> ExecutionReceipt:
    with database.read() as repo:
        payload = repo.connection.scalar(
            sa.select(s.executions.c.payload).where(s.executions.c.idempotency_key == key)
        )
        if payload is None:
            raise LookupError("Execution receipt not found")
        return ExecutionReceipt.model_validate(payload)
