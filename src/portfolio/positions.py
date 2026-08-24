"""Deterministic position engine: rebuild positions, realized P/L and FIFO lots from transactions.

Cost method for positions/realized P/L: AVERAGE COST (commissions included in cost basis on
buys and deducted from proceeds on sells). FIFO lots are maintained in parallel for future
tax logic; they are NOT used for the reported realized P/L (documented in README).

Short positions: handled symmetrically (negative quantity, average cost of the short).
A trade that crosses zero is split into a closing part and an opening part.
Corporate actions (splits, spin-offs) are NOT applied - see README limitations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.core import D, ZERO, f, q
from src.db.models import Position, TaxLot, Transaction
from src.logging_setup import get_logger

log = get_logger("positions")

QTY_EPS = Decimal("0.000000005")


@dataclass
class Trade:
    """Minimal trade record consumed by the engine (instrument currency)."""

    trade_date: date
    quantity: Decimal  # signed
    price: Decimal  # per unit, >= 0
    commission: Decimal = ZERO  # signed, negative = cost
    fees: Decimal = ZERO  # signed
    transaction_id: int | None = None
    sort_key: tuple = ()

    @property
    def costs(self) -> Decimal:
        """Total trading costs as a positive number."""
        return -(self.commission + self.fees)


@dataclass
class Lot:
    open_date: date
    quantity_open: Decimal
    quantity_remaining: Decimal
    unit_cost: Decimal
    open_transaction_id: int | None = None
    closed_date: date | None = None
    realized_pnl: Decimal = ZERO


@dataclass
class PositionState:
    quantity: Decimal = ZERO
    cost_basis: Decimal = ZERO  # signed like quantity (negative for shorts)
    realized_pnl: Decimal = ZERO
    first_trade_date: date | None = None
    last_trade_date: date | None = None
    lots: list[Lot] = field(default_factory=list)
    closed_trades: list["ClosedTrade"] = field(default_factory=list)

    @property
    def average_cost(self) -> Decimal | None:
        if abs(self.quantity) <= QTY_EPS:
            return None
        return self.cost_basis / self.quantity


@dataclass
class ClosedTrade:
    """A realization event (partial or full close), used for trading statistics."""

    close_date: date
    quantity: Decimal  # absolute quantity closed
    proceeds: Decimal  # net of costs
    cost: Decimal  # cost basis released
    realized_pnl: Decimal
    open_date: date | None  # FIFO-earliest lot opened date among those consumed


def compute_position(trades: list[Trade]) -> PositionState:
    """Replay trades chronologically. Pure function - fully testable."""
    state = PositionState()
    for t in sorted(trades, key=lambda x: (x.trade_date, x.sort_key, x.transaction_id or 0)):
        if t.quantity == 0:
            continue
        state.first_trade_date = state.first_trade_date or t.trade_date
        state.last_trade_date = t.trade_date
        remaining = t.quantity
        unit_cost_of_trade = t.price + (t.costs / abs(t.quantity))  # costs spread per unit

        # 1. closing part (opposite sign to current position)
        if state.quantity != 0 and (state.quantity > 0) != (remaining > 0):
            close_qty = min(abs(remaining), abs(state.quantity))
            sign = 1 if state.quantity > 0 else -1
            avg = state.cost_basis / state.quantity  # per unit, positive
            cost_released = avg * close_qty
            # For long close: proceeds = price*qty - costs_share; for short cover: pay price*qty + costs
            cost_share = t.costs * (close_qty / abs(t.quantity))
            if sign > 0:
                proceeds = t.price * close_qty - cost_share
                pnl = proceeds - cost_released
            else:
                proceeds = -(t.price * close_qty + cost_share)  # cash paid to cover
                pnl = cost_released - (t.price * close_qty + cost_share)
            state.realized_pnl += pnl
            state.cost_basis -= sign * cost_released
            state.quantity -= sign * close_qty
            open_date = _consume_lots(state.lots, close_qty, t.price, cost_share, t.trade_date, sign)
            state.closed_trades.append(
                ClosedTrade(
                    close_date=t.trade_date,
                    quantity=close_qty,
                    proceeds=proceeds,
                    cost=cost_released,
                    realized_pnl=pnl,
                    open_date=open_date,
                )
            )
            remaining -= -sign * close_qty  # remaining moves toward zero
            if abs(state.quantity) <= QTY_EPS:
                state.quantity = ZERO
                state.cost_basis = ZERO

        # 2. opening part (same sign as position, or from flat)
        if abs(remaining) > QTY_EPS:
            # cost per unit: buys add costs, short sales receive price minus costs
            per_unit = unit_cost_of_trade if remaining > 0 else (t.price - t.costs / abs(t.quantity))
            state.cost_basis += remaining * per_unit
            state.quantity += remaining
            state.lots.append(
                Lot(
                    open_date=t.trade_date,
                    quantity_open=remaining,
                    quantity_remaining=remaining,
                    unit_cost=per_unit,
                    open_transaction_id=t.transaction_id,
                )
            )
    return state


def _consume_lots(
    lots: list[Lot], close_qty: Decimal, price: Decimal, cost_share: Decimal, close_date: date, sign: int
) -> date | None:
    """FIFO: reduce open lots by close_qty; returns the earliest consumed lot's open date."""
    left = close_qty
    earliest: date | None = None
    for lot in lots:
        if left <= QTY_EPS:
            break
        if abs(lot.quantity_remaining) <= QTY_EPS:
            continue
        take = min(left, abs(lot.quantity_remaining))
        share = cost_share * (take / close_qty) if close_qty else ZERO
        if sign > 0:
            lot.realized_pnl += (price * take - share) - lot.unit_cost * take
        else:
            lot.realized_pnl += lot.unit_cost * take - (price * take + share)
        lot.quantity_remaining -= sign * take
        if abs(lot.quantity_remaining) <= QTY_EPS:
            lot.quantity_remaining = ZERO
            lot.closed_date = close_date
        earliest = earliest or lot.open_date
        left -= take
    return earliest


# --- DB integration -------------------------------------------------------------


def load_trades(session: Session, account_id: int | None = None) -> dict[tuple[int, int], list[Trade]]:
    """Group DB transactions into (account_id, instrument_id) -> [Trade]. Only buy/sell rows of
    non-cash instruments (FX conversions are handled by the cash ledger, not as positions)."""
    from src.db.models import Instrument

    stmt = (
        select(Transaction)
        .join(Instrument, Instrument.id == Transaction.instrument_id)
        .where(Transaction.transaction_type.in_(("buy", "sell")), Instrument.asset_type != "cash")
    )
    if account_id is not None:
        stmt = stmt.where(Transaction.account_id == account_id)
    stmt = stmt.order_by(Transaction.trade_date, Transaction.trade_datetime, Transaction.id)
    groups: dict[tuple[int, int], list[Trade]] = {}
    for tx in session.scalars(stmt):
        groups.setdefault((tx.account_id, tx.instrument_id), []).append(
            Trade(
                trade_date=tx.trade_date,
                quantity=D(tx.quantity),
                price=D(tx.price) if tx.price is not None else ZERO,
                commission=D(tx.commission),
                fees=D(tx.fees),
                transaction_id=tx.id,
                sort_key=(tx.trade_datetime.isoformat() if tx.trade_datetime else "", tx.id),
            )
        )
    return groups


def rebuild_positions(session: Session, keep_closed: bool = True) -> dict[tuple[int, int], PositionState]:
    """Recompute all positions and lots from transactions. Idempotent (full replace)."""
    from src.db.models import Instrument  # local import to avoid cycles

    groups = load_trades(session)
    session.execute(delete(TaxLot))
    session.execute(delete(Position))
    currencies = {i.id: i.currency for i in session.scalars(select(Instrument))}
    states: dict[tuple[int, int], PositionState] = {}
    open_count = 0
    for (acc_id, inst_id), trades in groups.items():
        st = compute_position(trades)
        states[(acc_id, inst_id)] = st
        is_open = abs(st.quantity) > QTY_EPS
        if is_open or keep_closed:
            session.add(
                Position(
                    account_id=acc_id,
                    instrument_id=inst_id,
                    quantity=float(q(st.quantity)),
                    average_cost=f(q(st.average_cost)),
                    cost_basis=f(q(st.cost_basis)) if is_open else 0.0,
                    realized_pnl=float(q(st.realized_pnl)),
                    currency=currencies.get(inst_id, "USD"),
                    first_trade_date=st.first_trade_date,
                    last_trade_date=st.last_trade_date,
                )
            )
        if is_open:
            open_count += 1
        for lot in st.lots:
            session.add(
                TaxLot(
                    account_id=acc_id,
                    instrument_id=inst_id,
                    open_transaction_id=lot.open_transaction_id,
                    open_date=lot.open_date,
                    quantity_open=float(q(lot.quantity_open)),
                    quantity_remaining=float(q(lot.quantity_remaining)),
                    unit_cost=float(q(lot.unit_cost)),
                    currency=currencies.get(inst_id, "USD"),
                    closed_date=lot.closed_date,
                    realized_pnl=float(q(lot.realized_pnl)),
                )
            )
    session.flush()
    log.info("rebuild-portfolio: %d instruments replayed, %d open positions", len(groups), open_count)
    return states
