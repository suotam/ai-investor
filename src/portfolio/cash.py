"""Cash ledger derived from transactions + cash flows, per account and currency.

Double-entry-like treatment (see README "Accounting model"):
  * security BUY/SELL: cash leg = transactions.net_amount (proceeds +/- costs) in the trade
    currency; the security leg lives in positions (cost basis).
  * FX conversion (cash-type instrument, symbol BASE.QUOTE): two cash legs -
      quote currency: net_amount (proceeds + commission), base currency: +quantity.
  * deposits/withdrawals/dividends/interest/fees/taxes: cash_flows.amount.

`transaction_cash_effects` is the SINGLE implementation of the per-transaction cash legs and
is used by both the point-in-time ledger and the daily value history - never reimplement it.

The derived balance equals the broker's cash only if ALL cash events are imported (trades,
deposits, dividends, interest, fees, taxes, FX conversions). Missing Flex sections => cash off.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import D, ZERO
from src.db.models import CashFlow, Instrument, Transaction


def transaction_cash_effects(tx: Transaction, cash_pair_symbol: str | None) -> list[tuple[str, Decimal]]:
    """Signed cash legs of one transaction as [(currency, amount)].

    cash_pair_symbol: the instrument symbol (e.g. "USD.CZK") when the transaction's instrument
    is a cash-type FX pair, else None.
    """
    effects: list[tuple[str, Decimal]] = []
    if tx.net_amount is not None and tx.net_amount != 0:
        effects.append((tx.currency, D(tx.net_amount)))
    if cash_pair_symbol and tx.transaction_type in ("buy", "sell"):
        # base leg of the FX pair: BUY USD.CZK qty=+430.8 => +430.8 USD; SELL => negative
        base = cash_pair_symbol.split(".")[0]
        if tx.quantity:
            effects.append((base, D(tx.quantity)))
    return effects


def cash_instrument_symbols(session: Session) -> dict[int, str]:
    return {
        i.id: i.symbol for i in session.scalars(select(Instrument).where(Instrument.asset_type == "cash"))
    }


def cash_balances(session: Session, as_of: date | None = None) -> dict[tuple[int, str], Decimal]:
    """Returns {(account_id, currency): balance}."""
    balances: dict[tuple[int, str], Decimal] = {}

    def add(acc: int, ccy: str, amount: Decimal) -> None:
        balances[(acc, ccy)] = balances.get((acc, ccy), ZERO) + amount

    cash_instruments = cash_instrument_symbols(session)

    tx_stmt = select(Transaction)
    if as_of:
        tx_stmt = tx_stmt.where(Transaction.trade_date <= as_of)
    for tx in session.scalars(tx_stmt):
        for ccy, amount in transaction_cash_effects(tx, cash_instruments.get(tx.instrument_id)):
            add(tx.account_id, ccy, amount)

    cf_stmt = select(CashFlow)
    if as_of:
        cf_stmt = cf_stmt.where(CashFlow.flow_date <= as_of)
    for cf in session.scalars(cf_stmt):
        add(cf.account_id, cf.currency, D(cf.amount))

    return balances


def external_flows(session: Session, account_id: int | None = None) -> list[tuple[date, int, str, Decimal]]:
    """Investor deposits/withdrawals as (date, account_id, currency, signed amount)."""
    stmt = select(CashFlow).where(CashFlow.is_external.is_(True))
    if account_id is not None:
        stmt = stmt.where(CashFlow.account_id == account_id)
    stmt = stmt.order_by(CashFlow.flow_date, CashFlow.id)
    return [(cf.flow_date, cf.account_id, cf.currency, D(cf.amount)) for cf in session.scalars(stmt)]
