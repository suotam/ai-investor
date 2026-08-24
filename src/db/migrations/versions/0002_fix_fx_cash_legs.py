"""Fix FX-conversion cash legs imported before the accounting fix.

IBKR reports netCash="0" for forex conversion trades (assetCategory CASH); the importer used
to store that 0 as `transactions.net_amount`, so the quote-currency leg of every FX conversion
was missing from the cash ledger (cash was overstated by the amount converted).

The correct value is deterministic from data we already store:
    net_amount = gross_amount (proceeds) + commission + fees
so this DATA migration recomputes it for all buy/sell transactions on cash-type instruments.
Rows where net_amount already equals that sum are unaffected (the UPDATE is idempotent).

Note: commissions charged in a currency different from the trade currency cannot be represented
here; the parser now emits a warning for those (none exist in data imported so far).

Revision ID: 0002_fix_fx_cash_legs
Revises: 0001_v1_core
"""
from __future__ import annotations

from alembic import op

revision = "0002_fix_fx_cash_legs"
down_revision = "0001_v1_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE transactions
        SET net_amount = COALESCE(gross_amount, 0) + COALESCE(commission, 0) + COALESCE(fees, 0)
        WHERE transaction_type IN ('buy', 'sell')
          AND instrument_id IN (SELECT id FROM instruments WHERE asset_type = 'cash')
        """
    )


def downgrade() -> None:
    # Data fix; the pre-fix values (netCash=0) are wrong and are not restored.
    pass
