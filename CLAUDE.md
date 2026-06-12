# CLAUDE.md — pm-system

Prediction market trading system. Polymarket primary, Kalshi opportunistic.
Phase 0 in progress: foundations + event log + metadata sync + engine supervisor.
**No live orders are placed yet. All signals are logged only.**

---

## Stack

- Python 3.12, asyncio throughout (one engine process + one monitor process)
- SQLite WAL (`data/state.db`) for operational state
- Append-only JSONL event log (`data/events/YYYY-MM-DD/`) for replay/backtest
- `websockets` for Polymarket CLOB WS, `aiohttp` for REST
- `pyyaml` for fee schedules, no other heavy deps yet
- No framework, no ORM, no message broker — keep it simple

Install deps: `pip install pyyaml websockets aiohttp`

---

## Repo layout

```
pm-system/
├── config/
│   ├── fees.yaml              # versioned fee schedules — verify monthly against live docs
│   └── settings.py            # all config, env-var driven, no secrets in code
├── pm/
│   ├── core/
│   │   ├── events.py          # Event dataclass + topic constants (bus vocabulary)
│   │   ├── bus.py             # async pub/sub, bounded queues, drop-oldest for market data
│   │   ├── books.py           # in-memory L2 order books (snapshot + delta, staleness)
│   │   └── db.py              # SQLite WAL schema + upsert/query helpers
│   ├── execution/
│   │   └── fee_engine.py      # versioned fee schedules, per-category rates, min_edge()
│   ├── ingestion/
│   │   └── ws_polymarket.py   # CLOB WebSocket consumer, chunked subs, reconnect/backoff
│   ├── signals/
│   │   └── struct_arb.py      # S1 partition + complement scanner (signal-only)
│   └── monitoring/            # (empty — monitor process goes here)
├── tests/
├── data/                      # .gitignore this; created at runtime
└── systemd/                   # unit files go here
```

---

## Architecture

One asyncio engine process. Components communicate over an in-process `Bus`
(pub/sub, topic-routed). Everything published on the bus is also appended to
the on-disk event log by `event_logger` — that log is the replay/backtest dataset.

```
ws_polymarket  ──book/price_change──▶  bus  ──▶  event_logger  (disk)
metadata_sync  ──system events──────▶  bus  ──▶  struct_arb_task
rest_recon     ──recon results──────▶  bus  ──▶  db heartbeats
                                              ──▶  monitor (separate process via heartbeat file)
```

Bus topics (defined in `pm/core/events.py`):
- `book` — full L2 snapshot
- `price_change` — level deltas
- `last_trade_price` — trades
- `tick_size_change`
- `market_event` — new_market / market_resolved / best_bid_ask
- `signal` — fired by signal scanners (LOSSLESS queue — never drop)
- `system` — connect/disconnect/recon/errors (LOSSLESS queue — never drop)

---

## Config & secrets

All settings in `config/settings.py` — read from env vars, with defaults.
Secrets (Telegram token etc.) come from environment only — never hardcoded.

For local dev, export vars or create a `.env` and `source` it.
For production, use systemd `EnvironmentFile=`.

Key env vars:
```
PM_DB_PATH          path to state.db          (default: data/state.db)
PM_EVENTS_DIR       path to event log dir     (default: data/events)
PM_TG_TOKEN         Telegram bot token        (default: empty = disabled)
PM_TG_CHAT          Telegram chat ID
PM_TRACK_TOP_N      markets to track          (default: 150)
PM_WS_CHUNK         assets per WS connection  (default: 100)
```

---

## Fee engine

`pm/execution/fee_engine.py` — **the load-bearing component. All min-edge
thresholds in the system query this. Never hardcode a fee number anywhere else.**

Polymarket 2026 model: `fee = shares × category_rate × p × (1−p)`, makers free.
Kalshi: `ceil_to_cent(shares × rate × p × (1−p))` per side.

Fee schedules are versioned in `config/fees.yaml` by `effective_date`.
**Verify the live schedules before trading and re-verify monthly.**

```python
from pm.execution.fee_engine import FeeEngine
from pathlib import Path

fe = FeeEngine.from_yaml(Path("config/fees.yaml"))
fee = fe.taker_fee("polymarket", "politics", price=0.50, shares=100)  # → $1.00
min_e = fe.min_edge("polymarket", "politics", price=0.50, statistical_buffer=0.04)
```

---

## Database

`pm/core/db.py` — SQLite WAL, autocommit, `row_factory = sqlite3.Row`.

Key tables:
- `markets` — active market universe, NegRisk groupings, token IDs
- `rules_text` — resolution rules text per market, every version ever seen (for diff/change alerts)
- `signal_log` — every signal fired, with outcome/pnl filled in at resolution (meta-label training data)
- `heartbeats` — component liveness (engine writes; monitor reads)
- `recon_log` — WS vs REST price diffs

```python
from pm.core.db import connect, upsert_market, log_signal, beat
conn = connect(Path("data/state.db"))
beat(conn, "engine")
```

---

## Order books

`pm/core/books.py` — `BookStore` holds one `Book` per CLOB token (asset_id).
`ws_polymarket` calls `books.handle_ws_message(msg)` on every `book`/`price_change` event.

```python
book = books.peek(token_id)
if book and not book.is_stale(30):
    bid = book.best_bid()   # (price, size) or None
    ask = book.best_ask()
```

---

## Structural arb scanner (S1)

`pm/signals/struct_arb.py` — signal-only until Gate G1 is passed.

**Partition arb** (NegRisk groups — mutually exclusive, exhaustive outcomes):
- BUY-ALL: sum of YES asks + fees < $1.00 − buffer → riskless $1 payout per set
- SELL-ALL: sum of YES bids − fees > $1.00 + buffer → needs inventory (flagged)

**Complement check** (single market): YES ask + NO ask + fees < $1.00 − buffer

```python
scanner = StructArbScanner(books, fee_engine, buffer=0.01, min_sets=10.0)
# called by the scan task every scan_interval seconds
for group_id, legs_meta in neg_risk_groups.items():
    for sig in scanner.scan_partition(group_id, legs_meta):
        if scanner.should_emit(sig):
            log_signal(conn, strategy="struct_arb", ...)
```

IMPORTANT: partition buy-all is only riskless if the group is truly exhaustive.
Every NegRisk group must be manually verified before any execution is enabled.

---

## Research signals (S2 microstructure, S3 relative value, S4 momentum) + labeler

Research signals are observational by contract: `exec_sets=0` (can never form
an execution plan) AND their strategies are absent from the execution task's
`PM_EXECUTION_STRATEGIES` allowlist. They exist to build the meta-label
dataset; promote one to executable only after labeler validation + review.

`pm/signals/microstructure.py` (strategy `microstructure`):
- `ofi_pressure` — sustained depth imbalance at the touch while spread is tight
- `liquidity_shock` — spread blowout + depth evaporation vs own baseline
- `trade_through` — trade printing beyond fees away from mid (informed flow)

`pm/signals/relative_value.py` (strategy `rel_value`):
- `partition_sum_drift` — NegRisk group's YES-mid sum z-scored against its OWN
  rolling baseline (groups may legitimately not sum to 1.00); features carry
  `mover_token` (repriced) and `laggard_token` (stale quote = candidate edge)
- `complement_drift` — YES_mid + NO_mid departing from 1.00 beyond fees

`pm/signals/momentum.py` (strategy `momentum`):
- `directional_momentum` — sustained signed mid drift over the window, z-scored
  against the token's own per-step volatility (random-walk null). Whether it
  persists or reverts in prediction markets is what the labeler is for.
- `boundary_overshoot` — YES mid pinned beyond `boundary_low`/`high` for the
  full window, then a meaningful interior-direction bounce in the latest
  sample. The classic "extreme price + initial reversion" setup.

`pm/signals/labeler.py` — fills `signal_log.outcome/pnl` with forward mid
returns `PM_LABEL_HORIZON` (default 900s) after each signal. Outcome is signed
per leg (BUY: mid_now − price; SELL: inverse) and averaged. This is what turns
signal_log into training data; analyze hit rates per kind before trusting any
research signal.

---

## What exists now (Phase 0 — code complete)

| File | Status |
|------|--------|
| `config/fees.yaml` | ✅ done |
| `config/settings.py` | ✅ done |
| `pm/core/events.py` | ✅ done |
| `pm/core/bus.py` | ✅ done |
| `pm/core/books.py` | ✅ done |
| `pm/core/db.py` | ✅ done |
| `pm/execution/fee_engine.py` | ✅ done + smoke-tested |
| `pm/ingestion/ws_polymarket.py` | ✅ done |
| `pm/signals/struct_arb.py` | ✅ done |
| `pm/ingestion/metadata_sync.py` | ✅ done — fetches Gamma `/events` (tags→category, event=NegRisk group) |
| `pm/ingestion/event_logger.py` | ✅ done |
| `pm/ingestion/rest_recon.py` | ✅ done — recon diffs verified 0.0 vs REST |
| `pm/signals/scan_task.py` | ✅ done — drives S1 + S2 + S3 from one subscription |
| `pm/signals/microstructure.py` | ✅ done — S2 research signals (OFI, liquidity, trade-through) |
| `pm/signals/relative_value.py` | ✅ done — S3 research signals (partition/complement drift) |
| `pm/signals/momentum.py` | ✅ done — S4 research signals (directional drift, boundary overshoot) |
| `pm/signals/labeler.py` | ✅ done — forward-return outcome labeler |
| `engine.py` | ✅ done — live-run verified |
| `monitor.py` | ✅ done — stale-heartbeat alert verified |
| `dashboard.py` | ✅ done — read-only web dashboard (aiohttp) |
| `tests/` | ✅ done — 23 passing (`pytest tests/ -v`) |
| `systemd/` | ✅ done — engine + monitor units, `.env.example` |

Remaining for the G0 gate: the 7-day live soak, the `iptables` reconnect drill,
and hand-checking 20 real trades against the Polymarket UI (see TASKS.md § G0).

### Field-name corrections (live Gamma API differed from TASKS.md guesses)
- NegRisk groups are **events**, keyed on the event's `negRiskMarketID`/`id`
  (the market-level `negRiskMarketId` is null in live data).
- Category/tags live on `/events`, not `/markets` — sync fetches events.
- Gamma caps `limit` at 100 and `offset` at ~10k; sync orders by liquidity desc.

---

## Invariants — never violate these

1. **No orders until Phase 1 gate is passed.** `struct_arb.py` is signal-only.
   The word `order` must not appear in any execution path until `engine.py` has
   a live-trading flag that defaults to `False`.
2. **All min-edge checks go through `fee_engine.min_edge()`** — no hardcoded numbers.
3. **Every bus event is appended to the event log** — `event_logger` subscribes
   to ALL_TOPICS and writes to disk. This is not optional; it's the replay dataset.
4. **Lossless topics (`signal`, `system`) must never be dropped.** The bus raises
   `RuntimeError` on overflow of these queues. If that fires, a consumer is wedged
   and needs a bug fix, not a larger queue.
5. **Heartbeats are cheap — use them.** Every long-running task calls `db.beat()`
   every `settings.heartbeat_interval` seconds. The monitor process reads these
   and alerts if anything goes stale.
6. **The rules_text table keeps every version it ever sees.** `db.store_rules()`
   returns `True` if the rules changed since last fetch — that's a Telegram alert.
7. **Never store secrets in code or config files** — env vars or systemd EnvironmentFile only.
