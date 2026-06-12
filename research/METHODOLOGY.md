# Methodology

## 1. Data collection

### Venue

Polymarket centralised limit-order-book exchange. We connect to:

- CLOB websocket (`wss://ws-subscriptions-clob.polymarket.com/ws/market`)
  for book / price_change / last_trade_price events
- Gamma REST API (`gamma-api.polymarket.com`) for market metadata
- CLOB REST API (`clob.polymarket.com`) for periodic reconciliation

### Universe

We track the top `PM_TRACK_TOP_N = 150` most-liquid active markets at any
moment, defined by Polymarket's `liquidity` metadata. Each market has a YES
and a NO token, so 300 tokens are subscribed across 3 chunked WS
connections (100 tokens per connection — Polymarket's documented limit).

### Sampling period

A multi-day continuous live soak starting at the registration date in
`HYPOTHESES.md`. The 7-day G0-gate soak constitutes the *primary sample*.
Soak-day boundaries are UTC midnight. We will collect at least 14 calendar
days if opportunity frequency on the rarer signal kinds is the binding
constraint.

### Event log

Every event published on the internal bus is appended to a dated JSONL
file (`data/events/YYYY-MM-DD/events.jsonl`). The log is the canonical
artefact: the state DB is *derived* from the log plus the engine code at
the corresponding git SHA. We commit to never back-edit either.

Per-day volume is approximately 1 GB raw / 3.6 GB after WS replay
expansion. The log includes:

- `book` — full L2 snapshots at subscription start and after gaps
- `price_change` — level deltas
- `last_trade_price` — trade prints with size and side
- `system` — connect / disconnect / recon-drift events
- `signal` — every emitted signal with its features
- `execution` — every dry-run intent and risk decision (currently 0)

## 2. Signal emission

Ten scanners run concurrently against the live data. Each one is a Python
class with an injectable clock (so replay can use event-time). All
research scanners (every one except struct_arb) emit `ResearchSignal`
instances with `exec_sets = 0` by construction. The execution task
additionally filters by a strategy allowlist before any risk pipeline
runs (defense in depth). Source: `pm/signals/{microstructure,
relative_value, momentum, struct_arb}.py`.

Each signal carries:

- `strategy`, `kind` — taxonomy
- `group_id` — market_id or NegRisk group id
- `legs` — `[{token_id, market_id, side, price, size}, ...]` — `side` is
  the encoded directional prediction; `price` is the mid at signal time
- `gross_edge`, `fees`, `net_edge` — magnitude in price units where
  meaningful
- `features` — scanner-specific diagnostics (z-score, imbalance,
  mover/laggard, etc.)

## 3. Labeling protocol

### Forward-return outcome

`pm/signals/labeler.py` runs in-process, every 60 seconds. For every
unlabeled signal older than `PM_LABEL_HORIZON = 900` s (15 minutes), the
labeler computes the per-leg forward return:

- BUY-side leg: `O_leg = mid_at_label_time − price_at_signal_time`
- SELL-side leg: `O_leg = price_at_signal_time − mid_at_label_time`
- NA-side leg: `O_leg = mid_at_label_time − price_at_signal_time` (raw,
  unsigned for directional analysis; the leg's own features carry the
  context)

Signed so that **positive `O_leg` means the encoded direction was
correct**.

The signal-level outcome is the simple mean across legs that have a live
fresh book at label time. The signal's `pnl` is `outcome * exec_sets`
(zero for research signals by construction).

### Half-rule

A signal is labeled only when *at least half* of its legs have live fresh
books at label time. Otherwise it remains NULL and is retried in
subsequent passes. After `PM_LABEL_MAX_AGE = 86,400` s (24 hours) it
falls out of the retry window and stays NULL.

**Selection bias note.** The half-rule excludes signals on markets that
resolved or delisted before the horizon expired. These exclusions are
non-random with respect to outcome: markets that resolve mid-horizon may
have been informationally hot at signal time. We quantify the impact in
`CRITIQUE.md` § Selection bias and report `n_excluded` alongside `n_lab`
in every results table.

### Horizon

Fixed at 15 minutes for the primary analysis. The choice is motivated by:

- Equity microstructure literature uses 1–30 minute horizons for OFI
  studies (Cont et al. 2014 default to ~5 minutes)
- Prediction-market price moves are slower than equity; 15 minutes is a
  middle ground
- Long enough to be meaningful (not just bid/ask oscillation), short
  enough to be many-times-per-day-per-market

**Sensitivity.** We will compute the entire analysis at horizons of 5,
15, and 60 minutes (using the same event log) as a robustness check.
Results that are sensitive to horizon are flagged in the final paper.

### Why not realised P&L?

Forward mid-return ignores spread, slippage, queue position, and market
impact. It is a *signal* measure, not a *strategy* measure. We make no
P&L claim from this study; that requires Phase 1+ infrastructure that is
intentionally not enabled (see `PHASES.md`).

## 4. Statistical analysis

See `STATISTICS.md` for the full plan. In short:

- Per-kind primary metric: H_cond (conditional hit rate) with 10,000-
  resample bootstrap 95% CI
- Per-kind secondary metric: μ (mean outcome) with bootstrap CI
- Per-kind p-value: one-sided sign test where pre-prediction is
  directional; two-sided otherwise
- Family-wise correction: Benjamini–Hochberg FDR at q = 0.05 across 10
  kinds
- Null comparison: same-token same-time random-direction baseline
  generator (`pm/research/baseline.py`)

## 5. Event-time replay

`pm/backtest/signal_replay.py` re-runs the same scanners over the event
log using an *event-time* clock. This is used for:

1. **Threshold tuning.** Run with parameters different from production
   and see what signal counts and outcome distributions would have
   resulted. Used only for *exploratory* analysis; the primary tests use
   the live-emitted signals.
2. **Counterfactual baseline.** Run with the random-direction baseline
   generator instead of the real scanners. Gives the null distribution
   that conditional hit rate is tested against.

### Reproducibility

`scripts/research_report.py --as-of <ISO date>` reproduces every table in
the paper from:

- the event log up to that date
- the markets table snapshot at that date
- the git SHA of the engine code

We commit the markets snapshot at the registration date and at the
analysis date. If the live universe drifts (markets resolve, new ones
appear), the snapshot is the canonical universe for that analysis.

## 6. Software environment

- Python 3.12 (CPython, Windows + Linux tested)
- SQLite WAL (state DB)
- aiohttp 3.x (REST), websockets 16.x (WS)
- numpy / scipy *not* used in the live engine to keep the runtime simple;
  added in `pm/research/stats.py` for the analysis module
- Deterministic seeding of all RNGs in `pm/research/` (`numpy.random.
  default_rng(42)`)

## 7. Code review and replication

We commit to:

- Tagging the analysis-time git SHA as `analysis-vN`
- Publishing the markets snapshot, the event log day-files for the
  sample period, and `research_report.py` output
- Independent replication: a second analyst with read-only access to the
  artefacts can re-run `research_report.py` and check the published
  tables

Where Polymarket terms of service permit, the event log is publicly
shareable as a research dataset (raw exchange data with no private
information). If TOS forbid, we publish aggregated signal-level data
instead.
