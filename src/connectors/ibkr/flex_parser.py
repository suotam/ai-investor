"""Parse IBKR Activity Flex XML into normalized records.

Supported sections: AccountInformation, Trades (execution level), CashTransactions.
Unsupported sections (Transfers, CorporateActions, ...) produce explicit warnings - we never guess.
"""
from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import date, datetime
from decimal import Decimal

from src.core import D, InstrumentRef, NormalizedCashFlow, NormalizedTransaction, ParsedStatement

ASSET_CATEGORY_MAP = {
    "STK": "stock",
    "ETF": "etf",
    "FUND": "etf",
    "CASH": "cash",
    "BOND": "bond",
    "OPT": "option",
    "FOP": "option",
    "WAR": "other",
    "FUT": "other",
    "CRYPTO": "crypto",
}

CASH_TYPE_MAP = {
    "deposits/withdrawals": ("deposit", True),
    "deposits & withdrawals": ("deposit", True),
    "dividends": ("dividend", False),
    "payment in lieu of dividends": ("dividend", False),
    "withholding tax": ("tax", False),
    "broker interest paid": ("interest", False),
    "broker interest received": ("interest", False),
    "bond interest received": ("interest", False),
    "bond interest paid": ("interest", False),
    "other fees": ("fee", False),
    "broker fees": ("fee", False),
    "commission adjustments": ("fee", False),
    "price adjustments": ("other", False),
    "advisor fees": ("fee", False),
}

UNSUPPORTED_SECTIONS = (
    "Transfers",
    "CorporateActions",
    "OptionEAE",
    "ConversionRates",
)


class FlexParseError(ValueError):
    pass


def parse_flex_date(value: str | None) -> date | None:
    if not value:
        return None
    v = value.strip().split(";")[0].split(" ")[0]
    for fmt in ("%Y%m%d", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_flex_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace(";", " ").replace(",", " ")
    for fmt in ("%Y%m%d %H%M%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt)
        except ValueError:
            continue
    return None


def _dec(attrs: dict[str, str], key: str) -> Decimal | None:
    raw = attrs.get(key)
    if raw is None or raw.strip() == "":
        return None
    return D(raw)


def parse_flex_statement(xml_text: str) -> ParsedStatement:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise FlexParseError(f"Invalid Flex XML: {exc}") from exc

    if root.tag == "FlexStatementResponse":
        raise FlexParseError(
            f"Flex error response: {root.findtext('ErrorCode')} {root.findtext('ErrorMessage')}"
        )
    if root.tag != "FlexQueryResponse":
        raise FlexParseError(f"Unexpected root element {root.tag}")

    statements = root.findall("./FlexStatements/FlexStatement")
    if not statements:
        raise FlexParseError("No FlexStatement found in response")

    warnings: list[str] = []
    if len(statements) > 1:
        warnings.append(f"{len(statements)} FlexStatements found; all will be merged")

    account_id: str | None = None
    base_currency: str | None = None
    account_name: str | None = None
    period_from: date | None = None
    period_to: date | None = None
    transactions: list[NormalizedTransaction] = []
    cash_flows: list[NormalizedCashFlow] = []

    for stmt in statements:
        account_id = account_id or stmt.get("accountId")
        period_from = period_from or parse_flex_date(stmt.get("fromDate"))
        period_to = period_to or parse_flex_date(stmt.get("toDate"))

        info = stmt.find("./AccountInformation")
        if info is not None:
            base_currency = base_currency or info.get("currency")
            account_name = account_name or info.get("name")
            account_id = account_id or info.get("accountId")

        for trade in stmt.findall("./Trades/*"):
            if trade.tag != "Trade":
                continue  # Lot / Order / SymbolSummary rows are not executions
            lod = (trade.get("levelOfDetail") or "EXECUTION").upper()
            if lod != "EXECUTION":
                continue
            tx, warn = _parse_trade(trade.attrib)
            if warn:
                warnings.append(warn)
            if tx:
                transactions.append(tx)

        for ct in stmt.findall("./CashTransactions/CashTransaction"):
            lod = (ct.get("levelOfDetail") or "DETAIL").upper()
            if lod != "DETAIL":
                continue
            cf, warn = _parse_cash_transaction(ct.attrib)
            if warn:
                warnings.append(warn)
            if cf:
                cash_flows.append(cf)

        for section in UNSUPPORTED_SECTIONS:
            node = stmt.find(f"./{section}")
            if node is not None and len(node) > 0:
                warnings.append(
                    f"Section {section} contains {len(node)} rows but is NOT imported in v1 "
                    "(unsupported: positions/cash may be incomplete)"
                )

    if not account_id:
        raise FlexParseError("Could not determine accountId from Flex statement")

    return ParsedStatement(
        account_external_id=account_id,
        account_base_currency=(base_currency or "USD").upper(),
        account_name=account_name,
        period_from=period_from,
        period_to=period_to,
        transactions=transactions,
        cash_flows=cash_flows,
        warnings=warnings,
    )


def _instrument_from_attrs(a: dict[str, str]) -> InstrumentRef:
    category = (a.get("assetCategory") or "STK").upper()
    asset_type = ASSET_CATEGORY_MAP.get(category, "other")
    if asset_type == "stock" and (a.get("subCategory") or "").upper() in ("ETF", "ETN", "ETC"):
        asset_type = "etf"
    provider_ids = {}
    if a.get("conid"):
        provider_ids["ibkr_conid"] = a["conid"]
    return InstrumentRef(
        symbol=(a.get("symbol") or "").strip(),
        currency=(a.get("currency") or "USD").upper(),
        asset_type=asset_type,
        exchange=(a.get("listingExchange") or a.get("exchange") or None),
        name=a.get("description") or None,
        isin=a.get("isin") or None,
        cusip=a.get("cusip") or None,
        figi=a.get("figi") or None,
        provider_ids=provider_ids,
    )


def _parse_trade(a: dict[str, str]) -> tuple[NormalizedTransaction | None, str | None]:
    symbol = (a.get("symbol") or "").strip()
    if not symbol:
        return None, "Trade without symbol skipped"
    quantity = _dec(a, "quantity")
    if quantity is None:
        return None, f"Trade {a.get('tradeID')} without quantity skipped"
    buy_sell = (a.get("buySell") or "").upper()
    # IBKR quantity is already signed for sells in most report versions; normalise defensively.
    if buy_sell.startswith("SELL") and quantity > 0:
        quantity = -quantity
    if buy_sell.startswith("BUY") and quantity < 0:
        quantity = -quantity
    tx_type = "sell" if quantity < 0 else "buy"

    price = _dec(a, "tradePrice")
    proceeds = _dec(a, "proceeds")  # signed cash impact before commission
    if proceeds is None:
        multiplier = _dec(a, "multiplier") or Decimal("1")
        proceeds = -(quantity * (price or Decimal("0")) * multiplier)
    commission = _dec(a, "ibCommission") or Decimal("0")
    commission_ccy = (a.get("ibCommissionCurrency") or a.get("currency") or "").upper()
    trade_ccy = (a.get("currency") or "").upper()
    external_id = a.get("transactionID") or a.get("tradeID") or a.get("ibExecID") or None
    instrument = _instrument_from_attrs(a)

    warn = None
    if instrument.asset_type == "cash":
        # FX conversion (e.g. USD.CZK): IBKR reports netCash="0" because the cash movement is
        # carried by BOTH legs: `proceeds` in the quote currency (attr `currency`) and
        # `quantity` in the base currency of the pair. netCash is therefore IGNORED here and
        # the quote-currency cash leg is computed deterministically:
        #   net_amount = proceeds + commission (when the commission is charged in the quote ccy)
        # The base-currency leg (+quantity) is applied by the cash ledger (transaction_cash_effects).
        net_cash = proceeds
        if commission != 0:
            if commission_ccy == trade_ccy or not commission_ccy:
                net_cash += commission
            else:
                warn = (
                    f"FX trade {external_id}: commission {commission} {commission_ccy} charged in a "
                    f"different currency than the trade ({trade_ccy}); NOT applied to cash - adjust manually"
                )
    else:
        net_cash = _dec(a, "netCash")
        if net_cash is None:
            net_cash = proceeds + commission
        if commission != 0 and commission_ccy and commission_ccy != trade_ccy:
            warn = (
                f"Trade {external_id}: commission currency {commission_ccy} differs from trade currency "
                f"{trade_ccy}; cash ledger may be off by the commission"
            )
    if instrument.asset_type in ("option", "other"):
        warn = f"Trade {external_id} asset category {a.get('assetCategory')} imported but valuation is unsupported in v1"

    tx = NormalizedTransaction(
        external_id=external_id,
        transaction_type=tx_type,
        trade_date=parse_flex_date(a.get("tradeDate") or a.get("dateTime") or a.get("reportDate")) or date.today(),
        trade_datetime=parse_flex_datetime(a.get("dateTime")),
        settlement_date=parse_flex_date(a.get("settleDateTarget")),
        quantity=quantity,
        price=price,
        currency=(a.get("currency") or instrument.currency).upper(),
        gross_amount=proceeds,
        commission=commission,
        fees=Decimal("0"),
        net_amount=net_cash,
        fx_rate=_dec(a, "fxRateToBase"),
        instrument=instrument,
        notes=a.get("transactionType") or None,
    )
    return tx, warn


def _parse_cash_transaction(a: dict[str, str]) -> tuple[NormalizedCashFlow | None, str | None]:
    raw_type = (a.get("type") or "").strip()
    mapped = CASH_TYPE_MAP.get(raw_type.lower())
    if mapped is None:
        return None, f"Unknown cash transaction type '{raw_type}' skipped (id={a.get('transactionID')})"
    flow_type, is_external = mapped
    amount = _dec(a, "amount")
    if amount is None:
        return None, f"Cash transaction {a.get('transactionID')} without amount skipped"
    if flow_type == "deposit" and amount < 0:
        flow_type = "withdrawal"
    instrument = _instrument_from_attrs(a) if a.get("symbol") else None
    cf = NormalizedCashFlow(
        external_id=a.get("transactionID") or None,
        flow_type=flow_type,
        flow_date=parse_flex_date(a.get("settleDate") or a.get("dateTime") or a.get("reportDate")) or date.today(),
        amount=amount,
        currency=(a.get("currency") or "USD").upper(),
        is_external=is_external,
        description=a.get("description") or raw_type,
        instrument=instrument,
    )
    return cf, None
