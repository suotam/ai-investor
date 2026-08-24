"""Generic crypto CSV importer.

Expected columns (header names are case-insensitive; extra columns are ignored):

    date, type, asset, quantity, price, currency, fee, [fee_currency], [external_id], [note]

  date        ISO date or datetime (2024-03-01 or 2024-03-01 14:05:00)
  type        buy | sell | deposit | withdrawal | fee | interest | staking (=interest) | transfer_in | transfer_out
  asset       e.g. BTC, ETH (for deposit/withdrawal of fiat use the fiat code, e.g. CZK)
  quantity    positive number of units
  price       price per unit in `currency` (required for buy/sell; ignored otherwise)
  currency    fiat/quote currency of the trade (e.g. CZK, EUR, USD)
  fee         fee amount (>= 0) in fee_currency (default: `currency`)
  external_id optional provider id (strongly recommended for idempotency)

Conventions:
  * buy:  cash impact = -(quantity*price) - fee
  * sell: cash impact = +(quantity*price) - fee
  * deposit/withdrawal of fiat: external cash flow (affects TWR/MWR)
  * transfer_in/transfer_out of crypto: quantity moves at price (cost basis) - price required;
    these are NOT external cash flows from the performance point of view unless you mark
    them as such manually (limitation, see README).
Fees in a non-fiat currency (e.g. fee paid in BTC) are recorded as a separate 'fee' transaction
reducing the crypto quantity - their fiat value is not estimated (we never guess prices).
"""
from __future__ import annotations

import csv
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from src.connectors.crypto.base import CryptoConnector
from src.core import D, InstrumentRef, NormalizedCashFlow, NormalizedTransaction, ParsedStatement, stable_hash

SOURCE = "crypto_csv"
FIAT = {"CZK", "EUR", "USD", "GBP", "CHF", "PLN"}
REQUIRED = {"date", "type", "asset", "quantity", "currency"}


class CsvFormatError(ValueError):
    pass


def _parse_date(raw: str) -> tuple[date, datetime | None]:
    raw = raw.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.date(), (dt if "%H" in fmt else None)
        except ValueError:
            continue
    raise CsvFormatError(f"Unrecognized date format: {raw!r}")


class CsvCryptoImporter(CryptoConnector):
    source = SOURCE

    def __init__(self, path: str | Path, account_external_id: str = "crypto-csv", base_currency: str = "CZK"):
        self.path = Path(path)
        self.account_external_id = account_external_id
        self.base_currency = base_currency.upper()

    def load(self) -> ParsedStatement:
        with open(self.path, "r", encoding="utf-8-sig", newline="") as fh:
            sample = fh.read(4096)
            fh.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.DictReader(fh, dialect=dialect)
            if reader.fieldnames is None:
                raise CsvFormatError("Empty CSV")
            reader.fieldnames = [c.strip().lower() for c in reader.fieldnames]
            missing = REQUIRED - set(reader.fieldnames)
            if missing:
                raise CsvFormatError(f"Missing required columns: {sorted(missing)}")
            rows = list(reader)

        txs: list[NormalizedTransaction] = []
        cfs: list[NormalizedCashFlow] = []
        warnings: list[str] = []
        dates: list[date] = []

        for idx, row in enumerate(rows, start=2):
            r = {k: (v or "").strip() for k, v in row.items() if k}
            if not any(r.values()):
                continue
            try:
                tx_date, tx_dt = _parse_date(r["date"])
            except CsvFormatError as exc:
                warnings.append(f"row {idx}: {exc}; skipped")
                continue
            dates.append(tx_date)
            kind = r["type"].lower()
            asset = r["asset"].upper()
            currency = r["currency"].upper()
            quantity = abs(D(r["quantity"]))
            price = D(r["price"]) if r.get("price") else None
            fee = abs(D(r["fee"])) if r.get("fee") else Decimal("0")
            fee_ccy = (r.get("fee_currency") or currency).upper()
            ext_id = r.get("external_id") or None
            note = r.get("note") or None
            row_key = ext_id or stable_hash("row", tx_date, kind, asset, quantity, price, currency, fee, idx)

            if kind in ("staking", "reward"):
                kind = "interest"

            if kind in ("buy", "sell", "transfer_in", "transfer_out"):
                if price is None:
                    warnings.append(f"row {idx}: {kind} without price skipped (cost basis would be a guess)")
                    continue
                signed_qty = quantity if kind in ("buy", "transfer_in") else -quantity
                gross = -(signed_qty * price)
                fee_in_quote = fee if fee_ccy == currency else Decimal("0")
                txs.append(
                    NormalizedTransaction(
                        external_id=ext_id,
                        transaction_type="buy" if signed_qty > 0 else "sell",
                        trade_date=tx_date,
                        trade_datetime=tx_dt,
                        quantity=signed_qty,
                        price=price,
                        currency=currency,
                        gross_amount=gross,
                        commission=-fee_in_quote,
                        fees=Decimal("0"),
                        net_amount=gross - fee_in_quote,
                        instrument=InstrumentRef(symbol=asset, currency=currency, asset_type="crypto", name=asset),
                        notes=note or kind,
                    )
                )
                if fee > 0 and fee_ccy != currency:
                    txs.append(
                        NormalizedTransaction(
                            external_id=f"{row_key}:fee" if ext_id else None,
                            transaction_type="sell",
                            trade_date=tx_date,
                            trade_datetime=tx_dt,
                            quantity=-fee,
                            price=None,
                            currency=currency,
                            gross_amount=Decimal("0"),
                            commission=Decimal("0"),
                            fees=Decimal("0"),
                            net_amount=Decimal("0"),
                            instrument=InstrumentRef(symbol=fee_ccy, currency=currency, asset_type="crypto", name=fee_ccy),
                            notes=f"fee paid in {fee_ccy} for row {idx}",
                        )
                    )
                    warnings.append(f"row {idx}: fee paid in {fee_ccy}; fiat value of fee not estimated")
            elif kind in ("deposit", "withdrawal"):
                if asset not in FIAT:
                    warnings.append(
                        f"row {idx}: {kind} of non-fiat asset {asset} is not an external cash flow; "
                        "use transfer_in/transfer_out with a price instead; skipped"
                    )
                    continue
                amount = quantity if kind == "deposit" else -quantity
                amount -= fee
                cfs.append(
                    NormalizedCashFlow(
                        external_id=ext_id,
                        flow_type=kind,
                        flow_date=tx_date,
                        amount=amount,
                        currency=asset,
                        is_external=True,
                        description=note or kind,
                    )
                )
            elif kind in ("fee", "interest"):
                if asset in FIAT:
                    amount = quantity if kind == "interest" else -quantity
                    cfs.append(
                        NormalizedCashFlow(
                            external_id=ext_id,
                            flow_type=kind,
                            flow_date=tx_date,
                            amount=amount,
                            currency=asset,
                            is_external=False,
                            description=note or kind,
                        )
                    )
                else:
                    # crypto received (staking) or paid (fee) in kind: quantity change at zero cost
                    signed_qty = quantity if kind == "interest" else -quantity
                    txs.append(
                        NormalizedTransaction(
                            external_id=ext_id,
                            transaction_type="buy" if signed_qty > 0 else "sell",
                            trade_date=tx_date,
                            trade_datetime=tx_dt,
                            quantity=signed_qty,
                            price=Decimal("0"),
                            currency=currency,
                            gross_amount=Decimal("0"),
                            commission=Decimal("0"),
                            fees=Decimal("0"),
                            net_amount=Decimal("0"),
                            instrument=InstrumentRef(symbol=asset, currency=currency, asset_type="crypto", name=asset),
                            notes=note or kind,
                        )
                    )
            else:
                warnings.append(f"row {idx}: unknown type {kind!r} skipped")

        return ParsedStatement(
            account_external_id=self.account_external_id,
            account_base_currency=self.base_currency,
            account_name=f"Crypto CSV ({self.account_external_id})",
            period_from=min(dates) if dates else None,
            period_to=max(dates) if dates else None,
            transactions=txs,
            cash_flows=cfs,
            warnings=warnings,
        )
