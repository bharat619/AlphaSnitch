"""Confirmed offline executions and deterministic FIFO portfolio bookkeeping.

Decimals are JSON strings. FIFO uses execution time, then ledger insertion order.
Proportional fee allocation uses 80-digit arithmetic; final closes consume the
remaining basis exactly. Unknown FX propagates an unavailable accounting result.
"""

import hashlib
import json
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
    reconciliation_required: tuple[UUID, ...] = ()


class ExecutionReceipt(Contract):
    execution_id: UUID
    confirmation: TradeConfirmation
    position: Position
    portfolio_revision: str | None = None


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


def fifo(
    trades: list[tuple[UUID, TradeConfirmation]],
    splits: list[tuple[UUID, "CorporateAction"]] | None = None,
    template: TradeConfirmation | None = None,
) -> Position:
    """Replay all executions; reject any negative holding, including backdated sales."""
    if not trades and template is None:
        raise ValueError("No executions")
    template = template or trades[0][1]
    lots = []
    realized = Decimal(0)
    with localcontext() as context:
        context.prec = 80
        # A split takes effect before executions at the same timestamp.
        timeline = [(t.executed_at, 1, eid, t) for eid, t in trades]
        timeline += [(a.effective_at, 0, eid, a) for eid, a in (splits or [])]
        for _, kind, execution_id, trade in sorted(timeline, key=lambda item: item[:2]):
            if kind == 0:
                lots = [
                    lot.model_copy(update={"quantity": lot.quantity * trade.ratio}) for lot in lots
                ]
                continue
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
            instrument_id=template.instrument_id,
            venue=template.venue,
            currency=template.currency,
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
        events = _events(repo.connection)
        projected = [
            *history,
            ExecutionReceipt(
                execution_id=execution_id,
                confirmation=confirmation,
                position=_empty_position(confirmation),
            ),
        ]
        position = _replay(projected, events)
        revision = _revision(_history(repo.connection), events, execution_id)
        receipt = ExecutionReceipt(
            execution_id=execution_id,
            confirmation=confirmation,
            position=position,
            portfolio_revision=revision,
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


def portfolio_state(database) -> tuple[list[Position], str]:
    """Read positions and their revision from one consistent database snapshot."""
    with database.read() as repo:
        repo.connection.execution_options(isolation_level="REPEATABLE READ")
        history = _history(repo.connection)
        grouped = {}
        for receipt in history:
            grouped.setdefault(receipt.confirmation.instrument_id, []).append(receipt)
        events = _events(repo.connection)
        return ([_replay(rows, events) for rows in grouped.values()], _revision(history, events))


def portfolio(database) -> list[Position]:
    return portfolio_state(database)[0]


def freeze_open_holdings(database, market_id):
    """Freeze identities, remaining lots and effective entry links in one snapshot."""
    with database.read() as repo:
        repo.connection.execution_options(isolation_level="REPEATABLE READ")
        history, events = _history(repo.connection), _events(repo.connection)
        grouped = {}
        for receipt in history:
            grouped.setdefault(receipt.confirmation.instrument_id, []).append(receipt)
        holdings = []
        for instrument_id, rows in grouped.items():
            position = _replay(rows, events)
            instrument = repo.get_instrument(instrument_id)
            if position.quantity <= 0 or instrument.market_id != market_id:
                continue
            active = {r.execution_id: r.confirmation for r in rows}
            for event in events:
                operation = event.operation
                if isinstance(operation, Correction) and operation.target_execution_id in active:
                    active[operation.target_execution_id] = operation.replacement
            entries = []
            for lot in position.lots:
                trade = active[lot.execution_id]
                entries.append(
                    {
                        "execution_id": str(lot.execution_id),
                        "confirmation": trade.model_dump(mode="json"),
                        "entry_thesis": None,
                        "invalidation": None,
                        "context_status": "UNAVAILABLE: ledger retains execution links; "
                        "thesis/invalidation text was not recorded",
                    }
                )
            holdings.append(
                {
                    "instrument": instrument.model_dump(mode="json"),
                    "position": position.model_dump(mode="json"),
                    "entries": entries,
                    "exposure": {
                        "quantity": str(position.quantity),
                        "currency": position.currency,
                        "cost_basis": str(position.cost_basis)
                        if position.cost_basis is not None
                        else None,
                        "market_value": None,
                        "market_value_status": "UNAVAILABLE: no current valuation at selection",
                    },
                }
            )
        return holdings, _revision(history, events)


def execution_receipt(database, key: str) -> ExecutionReceipt:
    with database.read() as repo:
        payload = repo.connection.scalar(
            sa.select(s.executions.c.payload).where(s.executions.c.idempotency_key == key)
        )
        if payload is None:
            raise LookupError("Execution receipt not found")
        return ExecutionReceipt.model_validate(payload)


class Correction(Contract):
    target_execution_id: UUID
    # None reverses the execution. A replacement retains the original execution ID.
    replacement: TradeConfirmation | None = None
    reason: Text
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]


class CorporateAction(Contract):
    instrument_id: UUID
    kind: Literal["SPLIT", "MERGER", "SPIN_OFF", "AMBIGUOUS"]
    effective_at: AwareDatetime
    reason: Text
    idempotency_key: Annotated[str, Field(min_length=1, max_length=200, pattern=r"\S")]
    ratio: Positive | None = None

    @model_validator(mode="after")
    def valid_action(self):
        if self.effective_at > datetime.now(UTC):
            raise ValueError("Action timestamp cannot be in the future")
        if (self.kind == "SPLIT") != (self.ratio is not None):
            raise ValueError("Only a split requires an explicit new-shares/old-shares ratio")
        return self


class LedgerEventReceipt(Contract):
    event_id: UUID
    recorded_at: AwareDatetime
    operation: Correction | CorporateAction
    previous_correction_id: UUID | None = None
    status: Literal["APPLIED", "RECONCILIATION_REQUIRED"]
    portfolio_revision: str
    position: Position | None


def _events(connection):
    return [
        LedgerEventReceipt.model_validate(payload)
        for payload in connection.scalars(
            sa.select(s.ledger_events.c.payload).order_by(s.ledger_events.c.sequence)
        )
    ]


def _revision(history, events, extra_id=None):
    value = {
        "executions": [str(r.execution_id) for r in history]
        + ([str(extra_id)] if extra_id else []),
        "events": [str(e.event_id) for e in events],
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def portfolio_revision(database):
    return portfolio_state(database)[1]


def _empty_position(trade):
    return Position(
        instrument_id=trade.instrument_id,
        venue=trade.venue,
        currency=trade.currency,
        quantity=Decimal(0),
        cost_basis=Decimal(0),
        realized_pnl=Decimal(0),
        accounting_complete=True,
        lots=(),
    )


def _replay(history, events):
    active = {r.execution_id: r.confirmation for r in history}
    instrument_id = history[0].confirmation.instrument_id
    splits, unresolved = [], []
    for event in events:
        operation = event.operation
        if isinstance(operation, Correction) and operation.target_execution_id in active:
            active[operation.target_execution_id] = operation.replacement
        elif isinstance(operation, CorporateAction) and operation.instrument_id == instrument_id:
            if operation.kind == "SPLIT":
                splits.append((event.event_id, operation))
            else:
                unresolved.append(event.event_id)
    position = fifo(
        [(eid, t) for eid, t in active.items() if t is not None],
        splits,
        template=history[0].confirmation,
    )
    return position.model_copy(update={"reconciliation_required": tuple(unresolved)})


def record_ledger_event(database, operation: Correction | CorporateAction) -> LedgerEventReceipt:
    """Append atomically; replay never rewrites execution receipts or reports."""
    operation = type(operation).model_validate(operation.model_dump(mode="json"))
    with database.write() as repo:
        events = _events(repo.connection)
        for event in events:
            if event.operation.idempotency_key == operation.idempotency_key:
                if event.operation != operation:
                    raise ValueError("Idempotency key already used for a different ledger event")
                return event
        history = _history(repo.connection)
        previous_id = None
        if isinstance(operation, Correction):
            target = next(
                (r for r in history if r.execution_id == operation.target_execution_id), None
            )
            if target is None:
                raise LookupError("Target execution not found")
            instrument_id = target.confirmation.instrument_id
            if operation.replacement is not None:
                if operation.replacement.instrument_id != instrument_id:
                    raise ValueError("Correction cannot change instrument")
                validate_instrument(operation.replacement, repo.get_instrument(instrument_id))
            previous_id = next(
                (
                    e.event_id
                    for e in reversed(events)
                    if isinstance(e.operation, Correction)
                    and e.operation.target_execution_id == operation.target_execution_id
                ),
                None,
            )
        else:
            instrument_id = operation.instrument_id
            repo.get_instrument(instrument_id)
        affected = [r for r in history if r.confirmation.instrument_id == instrument_id]
        event_id = uuid4()
        receipt = LedgerEventReceipt(
            event_id=event_id,
            recorded_at=datetime.now(UTC),
            operation=operation,
            previous_correction_id=previous_id,
            status="RECONCILIATION_REQUIRED"
            if isinstance(operation, CorporateAction) and operation.kind != "SPLIT"
            else "APPLIED",
            portfolio_revision="pending",
            position=None,
        )
        projected = [*events, receipt]
        position = _replay(affected, projected) if affected else None
        receipt = receipt.model_copy(
            update={"position": position, "portfolio_revision": _revision(history, projected)}
        )
        repo.connection.execute(
            sa.insert(s.ledger_events).values(
                event_id=event_id,
                instrument_id=instrument_id,
                idempotency_key=operation.idempotency_key,
                payload=receipt.model_dump(mode="json"),
            )
        )
        return receipt


def ledger_history(database) -> list[ExecutionReceipt | LedgerEventReceipt]:
    """Original execution receipts followed by append-only corrective/action receipts."""
    with database.read() as repo:
        repo.connection.execution_options(isolation_level="REPEATABLE READ")
        return [*_history(repo.connection), *_events(repo.connection)]
