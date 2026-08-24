# Investor OS

A **local-first, read-only, deterministic** personal investment platform.

* **v1 — portfolio core**: imports broker/crypto transactions into a SQLite source of truth,
  rebuilds positions deterministically, values the portfolio in the base currency, computes
  performance (TWR / simple / XIRR), reconciles the cash ledger and shows everything in a
  local Streamlit dashboard.
* **v2 — research layer**: the investment reasoning & memory layer - investments (research
  pipeline), immutable thesis versions, explicit assumptions, risks, catalysts, thesis
  breakers, evidence, KPIs, valuation scenarios, an immutable decision journal, predictions
  and a deterministic review/health system. Fully usable **without any AI**.
* **v3 — intelligence layer**: external primary-source data with full provenance (SEC/EDGAR
  filings + XBRL, FRED macro, Form 4 insiders, 13F, congressional CSV imports, news provider
  interface), a deterministic intelligence-event inbox, technical context, discovery
  candidates, daily/weekly briefs, and an OPTIONAL local AI that produces PROPOSALS which
  only a human can accept. AI never writes to research tables.

## Philosophy (v2)

The system distinguishes and never silently mixes:
**FACTS** (kpi_observations, portfolio data) - **OBSERVATIONS/EVIDENCE** (evidence table,
raw and side-by-side, never auto-converted to conclusions) - **ASSUMPTIONS**
(thesis_assumptions, user-controlled status) - **INTERPRETATIONS** (thesis text) -
**FORECASTS** (predictions with explicit probability) - **DECISIONS** (immutable journal
with frozen context) - **OUTCOMES** (prediction resolutions, portfolio results).

**No-hindsight principle**: historical reasoning is never rewritten. A thesis change creates
a new immutable `thesis_versions` row; decisions keep pointing at the version that existed
when they were made; decision snapshots freeze portfolio context at decision time. The
system can always answer "what exactly did we believe on March 5, and why?".

**AI boundary**: nothing in v2 requires the local LLM. When AI arrives (llama.cpp at
127.0.0.1:8080), it may only *propose*: AI PROPOSAL -> USER REVIEW -> USER ACCEPT -> NEW
IMMUTABLE VERSION. `created_by` (USER | SYSTEM | AI | IMPORT) keeps provenance distinct.
AI never silently modifies a thesis and is never a source of truth for any number.

**Conventions**: confidence and probability are both 0-100 integers; confidence = subjective
strength of belief, probability = explicit likelihood of a defined event - never
interchanged. Temporal fields separate event_date / source_published_at / observed_at /
created_at to prevent look-ahead bias.

## Principles

| Principle | How v1 implements it |
|---|---|
| Local-first | SQLite at `data/investor.db`, dashboard bound to `127.0.0.1`, no telemetry, no cloud |
| Deterministic | All money math in Python `Decimal`; positions are a pure function of transactions |
| Source-traceable | Every row carries `source`, `source_hash`, `imported_at`; raw IBKR XML archived under `data/raw/ibkr/` |
| Idempotent | Re-importing the same statement inserts 0 rows (unique external id **and** content hash) |
| Never guess | Anything that cannot be computed from data is `None` → shown as **Unavailable** |
| Read-only | IBKR Flex Web Service only. No order/trading endpoints exist in the codebase |
| LLM is not a source of truth | `src/ai/` is an empty placeholder; no number is ever produced by an LLM |

## Architecture

```
investor-os/
├── config/settings.yaml        non-secret configuration (base currency, benchmarks, provider)
├── .env / .env.example         secrets (IBKR Flex token + query id) — never committed
├── alembic.ini                 migrations config
├── dashboard/
│   ├── app.py                  portfolio UI (Overview, Positions, Allocation, Performance, Trading Stats, Sync)
│   ├── pages/1_Research.py     v2: research pipeline + investment cockpit with manual-entry forms
│   ├── pages/2_Needs_Attention.py  v2: deterministic review queue
│   └── data.py                 read-only adapters engine → DataFrames (no math in the UI)
├── data/                       investor.db + raw/ archives (git-ignored)
├── logs/investor.log           rotating job log (git-ignored)
├── src/
│   ├── main.py                 `python -m src.main …`
│   ├── cli/main.py             Typer CLI
│   ├── config.py               YAML + .env loading, secret masking
│   ├── core.py                 Decimal helpers, hashing, normalized records
│   ├── db/models.py            SQLAlchemy ORM (source of truth)
│   ├── db/migrations/          Alembic (versions/0001_v1_core_…)
│   ├── connectors/ibkr/        flex_client (HTTP, read-only) · flex_parser (XML→records) · sync (orchestration)
│   ├── connectors/crypto/      base.py (interface) · csv_importer.py
│   ├── market_data/            base.py (MarketDataProvider) · yahoo.py (isolated) · service.py (cache + update job)
│   ├── portfolio/              instruments (resolution) · importer (idempotent) · positions (engine) · cash · fx · valuation/snapshot
│   ├── analytics/              performance (TWR/simple/XIRR/drawdown/history) · attribution · trading_stats
│   ├── research/               v2: investments · theses (immutable versions) · assumptions · items (risks/catalysts/breakers/premortem/redteam) · evidence · kpis · valuation · decisions · predictions · reviews · health
│   └── ai/                     reserved for the local LLM mentor (empty)
└── tests/                      pytest; fixtures/ibkr_flex_sample.xml, fixtures/crypto_sample.csv
```

Data flow: `connector → ParsedStatement (normalized) → importer (idempotent) → transactions/cash_flows`
→ `rebuild-portfolio` (positions, tax_lots) → `update-prices` (prices, fx_rates) → `snapshot`
/ dashboard (valuation + performance computed on read).

### Database tables

v1 (portfolio, migrations 0001-0002): `accounts`, `instruments`, `transactions`, `cash_flows`,
`positions`, `tax_lots`, `prices`, `fx_rates`, `portfolio_snapshots`, `benchmarks`, `import_runs`.

v2 (research, migration 0003): `investments`, `theses`, `thesis_versions`,
`thesis_assumptions`, `thesis_breakers`, `risks`, `catalysts`, `evidence`, `investment_kpis`,
`kpi_observations`, `valuation_models`, `valuation_scenarios`, `decisions`, `predictions`,
`premortems`, `red_team_entries`.

**Portfolio layer vs research layer**: the portfolio DB remains the sole source of truth for
holdings and money; the research layer is the source of truth for reasoning. They meet only
through `investments.instrument_id` - the Research UI reads live position data from the v1
valuation engine and copies nothing. Future v3 tables (filings, insider/congress/13F, macro)
plug into `evidence` via evidence_type + raw_reference without schema rework.

## Windows setup

```powershell
cd C:\Users\PC\Documents\ai-investor
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env            # then edit .env (see IBKR section)
```

Python 3.12 is used. All commands below assume the venv is active.

## Quick start

```powershell
python -m src.main init-db                                   # create/upgrade DB via migrations
python -m src.main sync-ibkr --file tests\fixtures\ibkr_flex_sample.xml   # or: sync-ibkr (live, needs .env)
python -m src.main import-crypto tests\fixtures\crypto_sample.csv          # optional
python -m src.main rebuild-portfolio                         # positions + lots from transactions
python -m src.main update-prices                             # Yahoo history for traded instruments, benchmarks, FX
python -m src.main snapshot                                  # store today's valuation
python -m src.main reconcile                                 # audit: cash legs by category, positions, equity
python -m src.main status                                    # DB status, last jobs, summary
python -m src.main dashboard                                 # = streamlit run dashboard/app.py on 127.0.0.1:8501
```

`python -m src.main --help` and `python -m src.main <command> --help` list all options.

## IBKR Flex setup (read-only)

1. In IBKR Client Portal: **Performance & Reports → Flex Queries → Activity Flex Query → Create**.
2. Include at least these sections (all fields is fine): **Account Information**, **Trades**
   (level of detail: *Execution*), **Cash Transactions** (level of detail: *Detail*).
   Optional but recommended for later: Open Positions, Transfers, Corporate Actions.
3. Period: e.g. *Last 365 days* (re-runs never create duplicates, so overlapping periods are fine).
4. Format XML. Save and note the **Query ID**.
5. **Settings → Flex Web Service → enable**, generate a **token**.
6. Put both in `.env`:

```
IBKR_FLEX_TOKEN=...
IBKR_FLEX_QUERY_ID=...
```

Then `python -m src.main sync-ibkr`. The client calls `SendRequest` → gets a reference code →
polls `GetStatement` until the report is ready → archives the XML to
`data/raw/ibkr/YYYY-MM-DD/` (sha256 recorded in `import_runs`) → parses → imports.
The token is never logged (only a masked form appears in `status`) and is not part of the
archived XML. You can also download the Flex XML manually and use `--file`.

## Market data

`MarketDataProvider` (`src/market_data/base.py`) is the only interface the app uses.
`YahooProvider` (yfinance) is the v1 implementation; swap it by adding a provider and changing
`market_data.provider` in `config/settings.yaml`. Prices are cached in `prices`, FX in `fx_rates`;
`update-prices` fetches only bars after the last cached date (3-day overlap) — same-day
re-runs fetch nothing.

Price symbols are derived from broker symbols (`instruments.price_symbol`, e.g. IBIS → `SAP.DE`,
crypto → `BTC-USD`). If a symbol is wrong or missing you will see *no price data for …* in the
job output; fix `price_symbol` in the `instruments` table and re-run. A price can be quoted in a
different currency than the instrument (`prices.currency`, e.g. BTC bought in CZK, priced in USD)
— the valuation converts through FX.

## Accounting model (authoritative sources)

The ledger is double-entry-like: every economic event has explicit cash legs per
**account + currency**; base-currency conversion happens only at valuation time (original
currencies are never lost).

| Event | Authoritative source | Cash effect |
|---|---|---|
| Security BUY | Flex `Trades` (`netCash`, fallback `proceeds`+`ibCommission`) | `-(qty x price) + commission + fees` in trade currency (commission is negative) |
| Security SELL | Flex `Trades` | `+(qty x price) + commission + fees` in trade currency |
| FX conversion (assetCategory `CASH`, symbol `BASE.QUOTE`) | Flex `Trades` — **`netCash` is ignored: IBKR reports `0` there** | quote ccy: `proceeds + commission`; base ccy: `+quantity` (signed) |
| Deposit / withdrawal | Flex `CashTransactions` (`Deposits/Withdrawals`) | signed `amount`; marked external (drives TWR/MWR) |
| Dividend / payment in lieu | Flex `CashTransactions` | `+amount` |
| Withholding tax | Flex `CashTransactions` | `-amount` (sign from IBKR) |
| Interest | Flex `CashTransactions` | signed `amount` |
| Fees | Flex `CashTransactions` (`Other Fees`, …) | signed `amount` |

`CashTransactions` never carries security-trade cash legs, and trade legs are never derived
from `CashTransactions` — each event is applied exactly once. The single implementation of
per-transaction cash legs is `transaction_cash_effects` in `src/portfolio/cash.py`; both the
point-in-time ledger and the daily value history use it. Invariant (encoded in
`tests/test_accounting.py`): **equity = cash + market value = net external flows + P/L** — a
deposit is never counted twice once invested. FX conversions move cash between currency
ledgers and are not positions and not P/L; the revaluation of foreign cash at current FX
rates is part of equity (visible in `reconcile` as "Equity - net flows").

`python -m src.main reconcile` prints the derived ledger broken into these categories, cash
by currency, positions and portfolio equity for auditing against the broker statement.

## Performance methodology

Daily portfolio value series `V_t` in the base currency is rebuilt from transactions + cash
flows + cached prices/FX (`build_value_history`). `F_t` = investor deposits/withdrawals that day
(`cash_flows.is_external`).

* **Simple return** = `(V_end − V_0 − ΣF) / (V_0 + ΣF)` — P/L over contributed capital. Easy
  to read, ignores *when* money was added.
* **Time-weighted return (TWR)** = chain of daily `V_t / (V_{t−1} + F_t) − 1` (flows at start of
  day). Independent of deposit timing → the right number to compare with a benchmark
  (this is what "Total Return" and the normalized chart use).
* **Money-weighted return (MWR / XIRR)** = annualized IRR of `−V_0, −F_t, +V_end` — the
  investor's actual experience including timing (bisection, deterministic).

If *any* day in the history cannot be valued (missing price or FX) the TWR and drawdown are
**Unavailable** rather than approximated; the dashboard lists the offending instrument/day.
Benchmarks (`SPY`, `QQQ` by default) are normalized to 100 and converted to the base currency
when FX is available for every day; otherwise shown in their own currency (the label says which).

Cost method: **average cost** including commissions (long and short, symmetric; a trade that
crosses zero is split). FIFO lots are maintained in `tax_lots` in parallel for future tax
logic; the reported realized P/L uses average cost.

Attribution: P/L per instrument (realized + unrealized, base currency), allocation weights and
`contribution_to_simple_return = pnl_i / (V_0 + ΣF)`. Time-weighted contribution is **TODO v2**.

## Crypto CSV import

`python -m src.main import-crypto export.csv [--account anycoin]`

Columns (case-insensitive, extra columns ignored, `,`/`;`/tab delimited):

```
date,type,asset,quantity,price,currency,fee,fee_currency,external_id,note
2024-01-05,deposit,CZK,50000,,CZK,0,,dep-1,bank transfer
2024-01-06 10:15:00,buy,BTC,0.5,1000000,CZK,250,,tx-1,
2024-03-01,sell,BTC,0.2,1200000,CZK,120,,tx-3,
2024-03-15,staking,ETH,0.01,,CZK,0,,tx-4,reward (zero cost)
2024-04-01,withdrawal,CZK,20000,,CZK,0,,wd-1,
```

Types: `buy`, `sell`, `deposit`, `withdrawal` (fiat only, external flows), `fee`, `interest`/`staking`
(in-kind at zero cost), `transfer_in`/`transfer_out` (require a price = cost basis).
Provide `external_id` whenever the exchange gives one — otherwise idempotency relies on the
content hash of the row. The `CryptoConnector` interface (`src/connectors/crypto/base.py`) is
where an Anycoin/exchange/wallet connector plugs in later.

## Dashboard

`python -m src.main dashboard` (or `streamlit run dashboard/app.py --server.address 127.0.0.1`).
Tabs: **Overview** (value, cash, invested, P&L, TWR, benchmark, deposits, simple/MWR, max drawdown,
value-over-time + portfolio-vs-benchmark charts) · **Positions** · **Allocation** (instrument,
sector, currency, asset class — missing sector = *Unknown*) · **Performance** (P/L per instrument,
top/bottom contributors, drawdown curve) · **Trading Statistics** (with the reminder that outcome
metrics do not measure decision quality) · **Import / Sync** (last jobs, counts, errors).
The dashboard never computes financial numbers itself; it renders engine output and caches by
DB file mtime.

## Research layer (v2)

### Investments & pipeline
DISCOVERED -> WATCHLIST -> RESEARCHING -> READY_FOR_DECISION -> OWNED -> EXITED / REJECTED /
ARCHIVED. An investment exists independently of ownership (watchlist, candidate, rejected).
Each has a review cadence (`review_frequency`, `next_review_date`); "Mark reviewed" advances
the date deterministically (monthly=30d, quarterly=91d, semiannual=182d, annual=365d;
after_earnings/manual are set by hand).

### Thesis versioning (exact model)
`theses` is the stable identity (title, investment, active, pointer to current version).
`thesis_versions` rows are **immutable**: create_thesis writes v1; revise_thesis requires a
`reason_for_revision`, copies unspecified fields from the current version, inserts the next
version with `previous_version_id`, and repoints `theses.current_version_id`. No code path
updates an existing version. The UI shows CURRENT THESIS and THESIS HISTORY (side-by-side
comparison of any two versions).

### Assumptions, risks, breakers, catalysts
Assumptions carry category, importance, expected value/range + unit, optional KPI link and a
breaker condition; status (SUPPORTED / WEAKENING / UNKNOWN / CHALLENGED / BROKEN) is changed
only by the user in v2 (notes get an audit stamp). Risks (could hurt) are separate from
thesis breakers (invalidate the case); breakers have ACTIVE/TRIGGERED/RESOLVED with
timestamps. Catalysts track expected vs actual date and outcome. Free-form breakers are not
machine-evaluated in v2.

### Evidence
Raw records with direction (SUPPORTING / CONTRADICTING / NEUTRAL), type (manual, filing,
earnings, transcript, news, macro, market_data, insider, congress, institutional, research,
other), target (investment/thesis/assumption/risk/catalyst/valuation), reliability and the
temporal triple event_date / source_published_at / observed_at. Evidence is displayed side
by side and never merged into a verdict.

### KPIs
`investment_kpis` defines what matters per company (unit, frequency, good direction);
`kpi_observations` stores actual values per period (unique per kpi+period+source, so a
future "consensus" source can coexist with "company"). Assumptions linked via kpi_id give
ACTUAL vs OUR EXPECTATION; consensus ingestion is v3.

### Valuation (deterministic, Decimal)
Multiple models per investment (dcf, pe, ev_ebitda, ev_sales, pb, fcf_yield, sotp, custom -
v2 stores structured scenarios; no full DCF engine). Scenarios (bear/base/bull/custom) hold
probability, target price, horizon in months and explicit dividends. Computed in
`src/research/valuation.py`: scenario return = (target + dividends)/reference - 1;
annualized = (1+r)^(12/months) - 1; probability-weighted target = sum(p_i * (target_i+div_i))
(probabilities must sum to 100 +/- 0.5 or validation rejects the set); margin of safety =
(fair - reference)/fair, shown vs base and vs weighted fair value. Individual scenarios are
always displayed alongside the weighted number, and nothing is ever a BUY/SELL signal.

### Decision journal (exact snapshot model)
Decisions (BUY / ADD / HOLD / TRIM / SELL / WATCH / REJECT / NO_ACTION - inaction is a
decision) are **immutable**; corrections are new rows via `amends_decision_id`. At creation
the service freezes deterministic context into columns + `snapshot_json`: instrument price
(price cache), portfolio value, cash, position quantity/weight (v1 valuation engine), plus
the research context (current thesis version id/number, active assumptions with status, open
risks, breakers, valuation scenarios). Unavailable values are stored as None - never
fabricated - and never recomputed from later state.

### Predictions & calibration
Explicit statements with probability (0-100), optional resolution date/condition; status
OPEN -> RESOLVED_TRUE / RESOLVED_FALSE / AMBIGUOUS / CANCELLED (single resolution,
timestamped). v2 shows simple counts/hit-rate; stored fields already support Brier scores
and calibration curves later.

### Review & thesis health
"Needs Attention" lists: reviews due, broken/challenged assumptions, triggered breakers,
high-severity open risks, expired catalysts, predictions past their resolution date, stale
valuations (>180 days), stale theses (>120 days since revision). Thesis health shows
transparent counts plus a state from documented rules in `src/research/health.py`
(BROKEN: any broken assumption or triggered breaker; AT_RISK: challenged, >=2 weakening, or
a critical open risk; WATCH: weakening, high risk or staleness; HEALTHY otherwise; omitted
when no assumptions exist). It is not an AI score and never a trade signal.

### Research file import (import-research)

Author an investment thesis in YAML (JSON also works) and import it in one shot instead of
filling every form:

```powershell
python -m src.main import-research examples\research\nu_example.yaml --dry-run   # validate + plan, writes NOTHING
python -m src.main import-research path\to\investment.yaml                      # real import
python -m src.main import-research path\to\addendum.yaml --allow-existing       # add records to an existing investment
```

Structure (see the fully commented example in `examples/research/nu_example.yaml` -
generic demo content, never import it into your production DB as-is): top-level keys
`schema_version: 1`, `investment`, optional `instrument`, `thesis`, `assumptions`, `risks`,
`thesis_breakers`, `catalysts`, `kpis` (with nested `observations`), `evidence`,
`valuation: {models: [...]}` (with nested `scenarios`), `predictions`, `decisions`.
Enums are case-insensitive and validated against the v2 vocabulary before any write
(pydantic, unknown fields are an error); prediction `probability` is required (0-100);
scenario probabilities must sum to 100 +/- 0.5.

Safety behavior:

* **Duplicate safety**: aborts if the investment ticker already exists. `--allow-existing`
  reuses it and adds records, but a `thesis` section against an investment that already has
  a thesis is always refused - the importer never overwrites a thesis and never creates a
  revision (revisions stay a deliberate act in the dashboard).
* **One transaction**: the whole import is atomic; any failure (bad reference, duplicate
  KPI, validation) rolls back everything - no half-imported investments.
* **Instrument linking**: the file links to an existing v1 `instruments` row (never creates
  one). A bare symbol works when unambiguous; if several instruments share the symbol,
  specify `instrument.exchange` + `instrument.currency` or the import aborts with the list
  of candidates. No match -> the investment is created without an instrument link (the v2
  model supports research on companies you cannot trade yet).
* **Name references**: `evidence.target_name` resolves assumption/risk/catalyst names within
  the same file; a missing or ambiguous name aborts the import.
* **Historical decisions**: a `decisions` entry with `decided_at` uses the standard decision
  service - the frozen snapshot is computed deterministically from the price/FX cache and
  the transaction ledger *as of that date*; anything unavailable is stored as null.
  The importer never reconstructs a fake historical portfolio from today's state, and a
  file-supplied `instrument_price` is informational only (warning is printed).
* **Provenance & audit**: all rows are `created_by=IMPORT`; a successful import records an
  `import_runs` row with the file path and SHA-256. Everything the importer creates goes
  through the v2 service layer, so all existing invariants stay active.

### v2 commands

```powershell
python -m src.main investments        # research pipeline table
python -m src.main thesis show NU     # current thesis + assumptions + history
python -m src.main review             # Needs Attention report
python -m src.main import-research file.yaml [--dry-run] [--allow-existing]
python -m src.main dashboard          # sidebar pages: Research, Needs Attention
```

All manual entry (investments, theses, revisions, assumptions, KPIs, evidence, risks,
catalysts, breakers, valuations, decisions, predictions, red team, pre-mortem) happens in
the dashboard Research page forms.

## Intelligence layer (v3)

### Pipeline & safety
`DATA -> NORMALIZED FACT -> EVIDENCE/KPI OBSERVATION -> INTERPRETATION -> AI PROPOSAL ->
HUMAN REVIEW -> ACCEPT/REJECT -> THESIS REVISION (v2 service)`. These layers never collapse:
external data and AI cannot modify theses, assumptions, breakers, risks, catalysts,
valuations, decisions or prediction resolutions. The single AI write target is
`ai_proposals`; acceptance (dashboard Intelligence Inbox or `proposals accept`) validates a
typed payload and calls the existing v2 service - a THESIS_REVISION additionally requires an
explicit human `reason_for_revision` and creates a new immutable version. IBKR remains read
only; there is no trading code.

### Provenance & source hierarchy
Every external item is registered in `source_documents` (provider, source type, external id,
URL, published/retrieved timestamps, parser version, metadata) with the raw payload archived
under `data/raw/<category>/` named by content hash - re-downloads of identical content are
no-ops, changed content is archived as a new file and the original is never overwritten.
Source tiers are explicit: 1 = primary (SEC, FRED, official filings), 2 = reputable
providers/press, 3 = aggregators, 4 = social/unverified. AI context always includes the tier;
a primary filing outweighs a secondary article.

### SEC / EDGAR (Tier 1)
`sec sync <ticker>`: ticker -> CIK via the official mapping, submissions index, material
filings archived + `NEW_FILING` events (10-K/20-F are HIGH materiality). **Foreign private
issuers are first-class**: NU files 20-F and 6-K, never 10-K/10-Q - the connector stores what
the issuer actually files and filters by the configurable `sec.material_forms`. Set
`SEC_USER_AGENT` in `.env` (SEC requires a descriptive UA with contact); requests are
rate-limited (~1/0.15 s), retried and time-limited. `sec filings <ticker>` lists the archive.

### XBRL & KPI bridge
`sec sync` also ingests `companyfacts` (structured XBRL, us-gaap AND ifrs-full) into
`financial_facts` - append-only, hash-idempotent, restatements become new rows. Concept ->
metric mapping is a deliberate alias table (never one-tag-one-KPI guessing) plus issuer
overrides. The KPI bridge then distinguishes: (A) deterministic mappings -> automatic
`kpi_observations` with provenance (`source=sec_xbrl`, accession in source_reference),
(B) loose name matches -> a KPI_MAPPING proposal for human review, (C) issuer-specific KPIs
(customers, ARPAC, NPL 90+...) -> reported as unsupported, values never invented.

### Insiders (Form 4)
`insiders sync <ticker>` parses raw ownership XML: transaction codes are normalized
(open-market purchase/sale, option exercise, award, tax withholding, gift...) and a sale is
never automatically bearish. Deterministic 30/90/365-day aggregates (buyers, sellers, net
shares, net value) are context, not signals. Only open-market transactions create events.

### Congress (interface + CSV)
There is no stable free official PTR API, so v3 ships the `CongressProvider` interface with a
CSV importer (`congress import file.csv`) for exports you trust. Preserved caveats: amounts
are RANGES, disclosure lags the transaction, owner may be spouse/dependent, unresolvable
tickers stay NULL. Events fire only for tracked people (watchlist) or companies we research.

### Institutional (13F)
`institutional add-manager "Name" CIK` + `institutional sync`: 13F-HR information tables per
period, deterministic change detection (NEW/INCREASED/DECREASED/EXITED/UNCHANGED) between the
two latest periods. Displayed everywhere with the disclaimer: delayed up to 45 days, no
shorts, incomplete portfolio - context, never endorsement.

### Macro
`macro sync` pulls configured FRED series (no API key; official aggregation of BLS/BEA/OECD)
into vintage-aware `macro_observations` - a revised value becomes a NEW row, reads take the
latest vintage per date. Series link to investments (`investment_macro_links`: relationship,
why it matters, importance) and linked releases create MEDIUM events. Limitation: Mexico
policy rate and Brazil unemployment have no reliable key-free FRED series (previous OECD
codes discontinued).

### Technical context
Deterministic indicators from the existing v1 price cache: SMA 20/50/200, 52-week high/low +
distance, 20d realized volatility, ATR(14), RSI(14), drawdown. Output is factual statements
("price is 18% below the 52-week high"), available via `technical <ticker>` and on the
Company Intelligence page. Highs/lows are approximated from closes (OHLC not used). Never a
signal.

### News
`NewsProvider` interface with normalization, URL dedup and same-story clustering; only
metadata and short excerpts are stored (copyright). No commercial provider is bundled in v3 -
`NullNewsProvider` is the default (documented limitation).

### Intelligence events & materiality
Everything meaningful lands in `intelligence_events` (deduplicated by key): NEW_FILING,
EARNINGS_RELEASE, KPI_UPDATE, INSIDER_TRANSACTION, CONGRESS_TRANSACTION,
INSTITUTIONAL_CHANGE, MACRO_RELEASE, PRICE_EVENT, NEWS_EVENT. Materiality is deterministic
(rules in `src/intelligence/events.py`: earnings/annual filings HIGH; open-market insider
trades MEDIUM; congress/news LOW...). AI may comment but never overrides these categories.

### AI (optional, local)
Provider abstraction with a local OpenAI-compatible implementation (llama.cpp). Configure in
`config/settings.yaml` / `.env` (`ai.enabled`, `AI_BASE_URL`, `AI_MODEL`); default endpoint
`http://127.0.0.1:8080/v1`. **The app is fully functional with AI disabled or the server
down** - AI failure never touches portfolio, research or ingestion. `ai status` shows the
active provider. Privacy: no cloud LLM; research data never leaves the machine.

AI agents (all produce PENDING proposals only, with provider/model/prompt version/context
hash recorded for auditability):
* `ai analyze <ticker> --event <id>` - contradiction-first analysis: the model receives the
  full investment context packet (thesis, assumptions, breakers, risks, KPIs, valuation,
  decisions, predictions, evidence) plus ONE event, and must answer the 10-question set
  (what supports / what contradicts / which assumptions weakened / breakers closer /
  skeptical view / missing info / valuation vs quality / genuinely new / monitor next).
  Purely bullish summaries are structurally impossible.
* `ai redteam <ticker>` - adversarial attack on the thesis (bear case, fragile assumptions,
  base rates, incentives, accounting, competition, regulation, macro, valuation risk).
* `ai earnings <ticker> [--event id]` - structured earnings review (exec summary, top-5
  changes, KPI table prev/current/change, thesis verdict, assumption/breaker analysis,
  questions for next quarter, citations to stored sources).
Epistemic rules are in every prompt: SOURCE FACT / CALCULATION / INTERPRETATION / HYPOTHESIS
/ UNKNOWN - missing data must be answered UNKNOWN, never filled from pretrained memory.
Context packets are time-aware (`as_of`) so historical decision reviews cannot use later
information (no-hindsight, tested).

### Briefs, discovery, calendar
`brief daily` / `brief weekly` are fully deterministic aggregations (portfolio, material
events, pending proposals, needs-attention, prediction record, upcoming dates, "what changed
this week that actually matters"). `discovery run` builds research candidates from modular
factors (13F new/increased positions of tracked managers; clusters of >=2 insiders buying
in 90d; manual) - "PROMOTE TO RESEARCH" creates a DISCOVERED investment via the v2 service;
fundamental screens await a fundamentals data source (limitation). The calendar aggregates
only KNOWN dates (catalysts, prediction deadlines, thesis reviews) - nothing fabricated.

### Dashboard pages (v3)
**Intelligence Inbox** (proposals with WHAT HAPPENED / WHY IT MATTERS / SOURCE + tier /
side-by-side CURRENT vs PROPOSED thesis / ACCEPT with required reason / REJECT / DEFER;
events with deterministic severity), **Company Intelligence** (filings, structured
financials, insiders, 13F, macro links, technical context per investment), **Markets &
Discovery** (macro charts, insiders, congress, institutional changes, candidates, 7/30-day
calendar, watchlists).

### v3 commands

```powershell
python -m src.main sec sync NU              # DOWNLOAD+PROCESS: filings + XBRL + KPI bridge
python -m src.main sec filings NU
python -m src.main macro sync
python -m src.main insiders sync NU
python -m src.main institutional add-manager "Name" CIK
python -m src.main institutional sync
python -m src.main congress import file.csv
python -m src.main intelligence events --severity MEDIUM
python -m src.main technical NU
python -m src.main ai status                # which provider is active
python -m src.main ai analyze NU --event 3  # AI ANALYZE: proposals only
python -m src.main ai redteam NU
python -m src.main ai earnings NU --event 3
python -m src.main proposals list           # APPLY step is always human
python -m src.main proposals accept 1 --reason "..."
python -m src.main brief daily
python -m src.main brief weekly
python -m src.main discovery run
```

### Suggested daily workflow (Windows Task Scheduler friendly - each command is idempotent,
no daemon required)

```text
06:00  sec sync NU  +  macro sync  +  insiders sync NU  (+ institutional sync weekly)
06:10  intelligence events --severity MEDIUM
06:20  ai analyze ... (optional, if the llama.cpp server is running)
06:30  brief daily
```

### v3 limitations (documented, intentional)
* Congress: CSV imports only (no stable free official API); live scraping is a future
  provider behind the same interface.
* News: interface only, no bundled provider.
* Earnings transcripts and management commentary are not ingested (UNKNOWN in AI output).
* Issuer-specific KPIs absent from XBRL (customers, ARPAC, NPL 90+...) remain manual.
* Technical context approximates highs/lows from closes.
* Mexico policy rate / Brazil unemployment macro series unavailable key-free on FRED.
* Fundamental discovery screens await a fundamentals data source.
* AI requires a locally running llama.cpp server; quality depends on the local model.
* Deterministic vs AI-generated: everything in briefs, events, aggregates, facts and
  technical context is deterministic Python; only proposal content and analysis text inside
  the Inbox is AI-generated and is labeled with provider/model/prompt version.

## Tests

```powershell
python -m pytest
```

112 tests, no network, no IBKR account: Flex parsing, duplicate import (id + hash), raw
archiving/audit, instrument resolution, cash ledger, average-cost/short/cross-zero/FIFO engine,
valuation & weights, FX (direct/inverse/cross/staleness/unavailable), price cache incrementality,
value history, TWR/simple/XIRR/drawdown on hand-checkable scenarios, snapshot idempotency, crypto
CSV parsing/import, price currency ≠ instrument currency, and the accounting reconciliation suite
(`tests/test_accounting.py`): deposit→buy equity invariant, partial sell, multi-currency
deposit→FX→USD-stock (the real-account regression, with IBKR's `netCash="0"` forex rows),
commission signs, re-import stability, and the 0002 data migration.

v2 research tests (`tests/test_research.py`, `tests/test_research_valuation_decisions.py`):
lifecycle transitions, thesis creation/revision immutability, the critical no-hindsight test
(BUY keeps referencing v1 after revision to v2), assumptions + status changes, breakers,
risks/catalysts, evidence directions and linkage, KPIs + observations + expectations,
probability-weighted valuation (the 70/140/200 -> 144 case), invalid probability rejection
(110% fails), edge cases (no reference price -> Unavailable), frozen decision snapshots
(portfolio changes later, decision does not), unavailable snapshot values stay None,
prediction lifecycle + simple stats, review-due logic, needs-attention sections, thesis
health rules, portfolio linkage without duplication, and 0002->0003 migration data
preservation.

## Security

* Dashboard binds to `127.0.0.1` (config); never use `0.0.0.0`.
* Secrets only in `.env` (git-ignored). `status` prints a masked token, logs never contain it.
* IBKR access is the Flex Web Service (reporting only). There is no order placement, no
  TWS/Gateway API, no exchange trading API and no automation that could place trades.
* No telemetry (`--browser.gatherUsageStats false`), no cloud LLM.

## Known limitations (v1) — stated, not guessed

* **Corporate actions** (splits, spin-offs, mergers, symbol changes) are *not* applied. A split
  will make quantity/average cost wrong until you add adjusting transactions manually. The Flex
  `CorporateActions` section is detected and reported as a warning only.
* **Transfers** (ACATS, internal position transfers) are not imported (warning only) — positions
  transferred in have no cost basis.
* **Cash balance is derived** from imported events (see *Accounting model*). It matches the
  broker only if the Flex query includes *all* cash transactions (deposits, dividends, interest,
  fees, taxes) and FX conversions (`CASH` asset category trades). `reconcile` audits the derived
  ledger; the broker's own CashReport is not imported yet.
* An FX-trade **commission charged in a third currency** (different from the pair's quote
  currency) is not applied to cash - the import emits a warning and it must be adjusted manually.
* **Options/futures/warrants** are imported as transactions but not valued (flagged *Unavailable*).
* **Realized P/L = average cost**; tax lots are FIFO but Czech tax rules (3-year test, 100k CZK
  limit) are not implemented.
* **Dividends, interest, fees** affect cash and therefore the value history/TWR, but are not
  attributed to instruments in "P/L per instrument".
* **Today's P&L** is `V_today − V_yesterday − flows`; it depends on the latest cached close and
  is not intraday.
* **Same-day ordering** uses the Flex `dateTime`; if two executions share a timestamp, import
  order decides (no effect on end-of-day results, may affect intra-day cross-zero splits).
* **Benchmarks are ETFs** (SPY/QQQ total-return-ish but including ETF fees); index levels are not
  used.
* Price/FX **back-fill** is limited to 10 / 7 days — older gaps become *Unavailable*.
* yfinance is an unofficial API; historical data may be adjusted or missing. All prices are
  unadjusted closes; the `adjusted_close` column is stored for later use.
* Multi-account: supported in data (every table is per account) but the dashboard shows the
  consolidated view only.

### v2 limitations (intentional)

Evidence, KPI values, thesis changes, assumption statuses, breaker triggers and prediction
resolutions are all **manual**. There is no automatic earnings/SEC/news/insider/Congress/13F
/macro ingestion, no AI thesis changes, no automatic screening and no consensus data - those
are v3+. Free-form breaker conditions are not machine-evaluated. Calibration shows simple
counts only. Deleting research rows is not exposed in the UI (immutability first).

## What v4 should do next

Earnings-release parsing for issuer-specific KPIs (NU: customers, ARPAC, NPL - from 6-K
exhibits/IR PDFs) feeding the existing KPI bridge; transcript ingestion; a live congressional
provider; scheduled automation (Task Scheduler recipes); calibration statistics once enough
predictions resolve; and on the portfolio side broker-side reconciliation
(CashReport/OpenPositions) and corporate actions.
