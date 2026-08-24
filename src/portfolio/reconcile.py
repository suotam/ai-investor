"""Reconciliation report: break the internally derived ledger into auditable categories.

Everything here is derived from imported transactions/cash_flows - no broker-side values are
fabricated. When OpenPositions/CashReport imports exist (v2), broker figures can be added as a
comparison column.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.core import D, ZERO, q
from src.db.models import Account, CashFlow, Instrument, Transaction
from src.portfolio.cash import cash_instrument_symbols, transaction_cash_effects
from src.portfolio.valuation import PortfolioValuation, value_portfolio

FLOW_CATEGORIES = {
    "deposit": "External deposits",
    "withdrawal": "External withdrawals",
    "dividend": "Dividends",
    "interest": "Interest",
    "fee": "Fees",
    "tax": "Taxes",
    "transfer": "Transfers",
    "other": "Other cash events",
}


@dataclass
class AccountReconciliation:
    account: Account
    # category -> currency -> amount
    categories: dict[str, dict[str, Decimal]] = field(default_factory=dict)
    cash_by_currency: dict[str, Decimal] = field(default_factory=dict)

    def add(self, category: str, currency: str, amount: Decimal) -> None:
        bucket = self.categories.setdefault(category, {})
        bucket[currency] = bucket.get(currency, ZERO) + amount
        self.cash_by_currency[currency] = self.cash_by_currency.get(currency, ZERO) + amount


def reconcile(
    session: Session, base_currency: str, as_of: date | None = None
) -> tuple[list[AccountReconciliation], PortfolioValuation]:
    as_of = as_of or date.today()
    accounts = {a.id: a for a in session.scalars(select(Account))}
    cash_pairs = cash_instrument_symbols(session)
    instruments = {i.id: i for i in session.scalars(select(Instrument))}
    recs: dict[int, AccountReconciliation] = {
        aid: AccountReconciliation(account=acc) for aid, acc in accounts.items()
    }

    for tx in session.scalars(select(Transaction).where(Transaction.trade_date <= as_of)):
        pair = cash_pairs.get(tx.instrument_id)
        category = "FX conversions" if pair else "Security trade cash legs"
        if tx.instrument_id is None:
            category = "Other transaction cash"
        for ccy, amount in transaction_cash_effects(tx, pair):
            recs[tx.account_id].add(category, ccy, amount)

    for cf in session.scalars(select(CashFlow).where(CashFlow.flow_date <= as_of)):
        recs[cf.account_id].add(FLOW_CATEGORIES.get(cf.flow_type, "Other cash events"), cf.currency, D(cf.amount))

    val = value_portfolio(session, base_currency, as_of)
    return [recs[a] for a in sorted(recs)], val


def format_report(
    recs: list[AccountReconciliation], val: PortfolioValuation, base_currency: str
) -> str:
    lines: list[str] = []
    Q2 = Decimal("0.01")

    def amt(v: Decimal | None) -> str:
        return "Unavailable" if v is None else f"{q(v, Q2):,}"

    for rec in recs:
        a = rec.account
        lines.append(f"ACCOUNT {a.name} ({a.provider}:{a.account_external_id or '-'})")
        order = [
            "External deposits", "External withdrawals", "Security trade cash legs", "FX conversions",
            "Dividends", "Interest", "Fees", "Taxes", "Transfers", "Other cash events", "Other transaction cash",
        ]
        for cat in order:
            bucket = rec.categories.get(cat)
            if not bucket:
                continue
            parts = ", ".join(f"{amt(v)} {ccy}" for ccy, v in sorted(bucket.items()))
            lines.append(f"  {cat:<28}{parts}")
        lines.append("  CASH BY CURRENCY (derived)")
        for ccy, v in sorted(rec.cash_by_currency.items()):
            lines.append(f"    {ccy:<6}{amt(v)}")
        lines.append("")

    lines.append("POSITIONS")
    if not val.positions:
        lines.append("  (none)")
    for r in sorted(val.positions, key=lambda x: x.symbol):
        lines.append(
            f"  {r.symbol:<10} qty {q(r.quantity, Q2):>14,}  cost {amt(r.cost_basis_local):>14} {r.currency}"
            f"  mkt {amt(r.market_value_local):>14} {r.currency}"
            f"  = {amt(r.market_value_base):>14} {val.base_currency}"
            + (f"  [{'; '.join(r.issues)}]" if r.issues else "")
        )
    lines.append("")
    lines.append(f"PORTFOLIO ({base_currency}, as of {val.as_of})")
    lines.append(f"  Cash          {amt(val.cash_base)}")
    lines.append(f"  Invested      {amt(val.invested_value_base)}")
    lines.append(f"  Total equity  {amt(val.total_value_base)}")
    lines.append(f"  Net external flows to date  {amt(val.net_external_flows_base)}")
    if val.total_value_base is not None and val.net_external_flows_base is not None:
        lines.append(f"  Equity - net flows (= lifetime P/L incl. FX)  {amt(val.total_value_base - val.net_external_flows_base)}")
    if val.issues:
        lines.append("  Issues:")
        for i in val.issues:
            lines.append(f"    - {i}")
    lines.append("")
    lines.append("Note: all figures are derived from imported transactions/cash flows;")
    lines.append("broker-side balances (CashReport/OpenPositions) are not imported in v1.")
    return "\n".join(lines)
