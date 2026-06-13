# pm-system tutorial

This walks through the system end-to-end: install, first run, what to look at,
how to read the signals, how to tune them offline, and how to keep things safe.
No order is ever placed by following these steps — the system is fail-closed by
design.

If something here disagrees with `CLAUDE.md` or `PHASES.md`, those files are
authoritative for invariants.

---

## 1. What this is

A Polymarket observation + research engine. One Python process reads live order
books, writes them to disk, and runs a stack of signal scanners. A second
process watches it. A third serves a dashboard. Execution is scaffolded but
defaults off behind three independent gates.

```
ws_polymarket  ─books──▶ bus ─▶ event_logger (3.6 GB/day)
metadata_sync ─events──▶                      ▼
rest_recon    ─prices──▶          ┌──▶ struct_arb     (S1, executable, gated)
                                  ├──▶ microstructure (S2, research only)
                                  ├──▶ relative_value (S3, research only)
                                  └──▶ momentum       (S4, research only)
                                         ▼
                                    signal_log ─▶ labeler (forward returns)
                                         ▼
                                    dashboard + monitor
```

---

## 2. First-time setup

### Install

```powershell
cd "C:\Users\juljo\Downloads\New folder (2)\extracted\pm-system"
pip install pyyaml websockets aiohttp pytest
```

That's all the runtime deps. There is no framework, no ORM, no Postgres.

### Verify

```powershell
python -m pytest tests/ -q
```

You should see all tests pass (the `test_partition_sum_drift` test occasionally
flakes on Windows because of `time.time()` resolution — re-run if it fails
once, file a bug if it fails twice).

### Config

Defaults work out of the box. To customise, copy `.env.example` and `source` it
(WSL) or set the variables in your shell. The settings worth knowing:

| Var | Default | What it controls |
|---|---|---|
| `PM_TRACK_TOP_N` | 150 | how many of the most-liquid markets to subscribe to |
| `PM_WS_CHUNK` | 100 | tokens per WS connection (300 tokens = 3 sockets) |
| `PM_LABEL_HORIZON` | 900 | seconds after a signal to label its forward outcome |
| `PM_EXECUTION_ENABLED` | false | gate 1 — must be `true` to even build intents |
| `PM_EXECUTION_MODE` | dry_run | gate 2 — `live` requires explicit review |
| `PM_LIVE_TRADING` | false | gate 3 — engine.py constant, defaults False |

There are no API keys to set unless you want Telegram alerts (`PM_TG_TOKEN` +
`PM_TG_CHAT`) or are wiring up the Polymarket broker, which is out of scope
until Phase 1 is gated open.

---

## 3. Daily flow

### Start everything

Three processes, three terminals (or use the systemd units in `systemd/`):

```powershell
python engine.py       # WS + scanners + labeler + execution stub
python monitor.py      # heartbeat watcher + Telegram alerter
python dashboard.py    # aiohttp web UI on :8787
```

Within ~15s the engine should log:

```
ws: tracking 300 assets across 3 connections
metadata_sync: tracking top 150 of N eligible markets
event_logger: writing to data/events/YYYY-MM-DD/events.jsonl
```

If WS reconnects keep firing, you are probably starting with `PM_TRACK_TOP_N`
set too high — the initial metadata fetch blocks the event loop. Reduce it.

### Open the dashboard

http://127.0.0.1:8787 — refreshes every 3 seconds. The header strip shows
component health, the cards below show market counts and signal performance.

### Check the inspector

Once a day, run:

```powershell
python scripts/inspect_state.py
python scripts/daily_report.py
```

Both pull from the same `state.db`. `inspect_state` is the quick liveness
check. `daily_report` gives the operational summary plus the signal-performance
table (hit rate and avg forward edge per strategy/kind).

### Run the replay

After a few hours of soaking, run:

```powershell
python scripts/replay_signals.py
python scripts/replay_signals.py --ofi 0.5 --mom-z 3.0 --horizon 600
```

This re-runs the scanners over the event log using *event time* instead of
wall clock, so a 24-hour log replays in a couple of minutes. Sweep thresholds
to see how hit rates and avg outcomes change before touching live settings.

---

## 4. Reading the signals

Open `data/state.db` with any SQLite client. The interesting tables:

- **`signal_log`** — every signal ever emitted, with `outcome` filled in by the
  labeler after `PM_LABEL_HORIZON` seconds. This is your training data.
- **`heartbeats`** — what was alive when.
- **`recon_log`** — WS-vs-REST price diffs. Anything > 0.01 deserves a look.

The four strategies and their kinds:

| strategy | kind | what it means | executable? |
|---|---|---|---|
| `struct_arb` | `partition_buy_all` | NegRisk group's YES asks sum to < $1 net of fees — riskless if group is exhaustive | yes, when G1 is open |
| `struct_arb` | `partition_sell_all` | YES bids sum to > $1 — needs inventory | yes, when G1 is open |
| `struct_arb` | `complement` | single market YES+NO asks < $1 | yes, when G1 is open |
| `microstructure` | `ofi_pressure` | sustained order-flow imbalance at the touch | research only |
| `microstructure` | `liquidity_shock` | spread blowout + depth evaporation | research only |
| `microstructure` | `trade_through` | trade printed beyond fees from mid | research only |
| `rel_value` | `partition_sum_drift` | group YES-mid sum z-scored vs its OWN baseline | research only |
| `rel_value` | `complement_drift` | YES_mid + NO_mid drifted from 1.0 beyond fees | research only |
| `momentum` | `directional_momentum` | sustained signed mid drift, z-scored vs own vol | research only |
| `momentum` | `boundary_overshoot` | YES pinned beyond 0.95 / 0.05 then bounced inward | research only |
| `whale_follow` | `tracked_wallet_position` | tracked Polymarket wallet (calibration above baseline) takes new on-chain position | research only |
| `news` | `headline_match` | RSS headline matches a tracked market and Bayesian update exceeds the edge threshold | research only |
| `calibration` | `model_divergence` | market mid diverges from internal-base-rate + Metaculus blended model by ≥ threshold | research only |

### Optional integrations (not required for the core engine)

The whale-follow, news, and calibration strategies all *idle* if their
external dependency is not configured:

- **Whale-follow** needs `PM_POLYGON_RPC_URL` (any Polygon JSON-RPC
  endpoint — Alchemy, Infura free tier, public node). Empty URL → the
  `ctf_listener` task logs and idles; everything else keeps running.
  You also need to mark wallets you care about with
  `python scripts/inspect_state.py` or by direct SQL on the `whale_wallets`
  table; the listener watches transfers involving those wallets only.

- **News** needs `config/news_feeds.yaml` (copy from `news_feeds.yaml.example`).
  Empty file → the `rss_poller` task idles.

- **Calibration** needs `config/base_rates.yaml` (an example is shipped).
  Edit base rates as you gather resolved samples. Set
  `PM_CALIB_METACULUS=true` to additionally blend in Metaculus public-API
  forecasts (no auth required).

### What we intentionally did NOT build

The strategy report (`STRATEGY_BRIEF.md`) describes a four-strategy framework
that included **cross-platform arbitrage between Kalshi and Polymarket**.
That strategy is intentionally not built — adding Kalshi would more than
double the integration surface and the user explicitly opted out. The other
three strategies (whale-follow, news, calibration) are built and structurally
fail-closed.

Each signal in `signal_log` carries a `legs_json` (which tokens, at what price,
which side) and a `features_json` (the scanner's own diagnostics — z-score,
imbalance, mover/laggard, etc).

### Outcome convention

The labeler fills `signal_log.outcome` (signed forward return). For a BUY leg,
`outcome = mid_after_horizon − price_at_signal`. For SELL, the sign is
inverted. Multi-leg signals average across legs that have live books at the
horizon (half-rule: at least half the legs must label).

A *positive* outcome means the signal was directionally right. A *negative*
outcome means the price moved against the signal.

### What the data has shown so far

After 489 signals (403 labeled) on a real-time soak:

- `ofi_pressure`: 12.7% hit rate, +0.0017 avg — near noise floor. Plausibly a
  tiny but real edge.
- `trade_through`: 25.8% hit rate, −0.0371 avg — *negative*. Trades printing
  through the mid are informed flow against the direction the prior mid
  implied. Probably valuable as a "do not execute here" filter.
- `liquidity_shock`: 46.7% hit rate, −0.0103 avg — also slightly negative.
  Again, useful as an execution filter.
- `struct_arb` / `partition_*` / `complement_drift`: too few samples yet.

Run `python scripts/daily_report.py` to see the current numbers. Anything with
< 50 labeled outcomes is not a verdict; it is a sample.

---

## 5. Tuning thresholds

You should never edit live thresholds from speculation. Use the replay loop:

```powershell
# baseline
python scripts/replay_signals.py

# crank OFI threshold up; fewer signals, hopefully better quality
python scripts/replay_signals.py --ofi 0.75 --min-samples 30

# longer horizon (10 min -> 15 min)
python scripts/replay_signals.py --horizon 900

# momentum: require bigger z, only fire on really persistent moves
python scripts/replay_signals.py --mom-z 3.0 --mom-min-drift 0.02
```

The output is a per-kind table:

```
strategy         kind                       n  labeled    hit       avg    median
---------------- ---------------------- -----  -------  -----  --------  --------
microstructure   ofi_pressure             411      403    13%   +0.0017   +0.0000
momentum         directional_momentum      37       31    52%   +0.0042   +0.0021
```

Once a kind shows a stable positive avg outcome over many hundreds of labeled
samples *and* the hit rate is materially above 50%, it is a candidate for
promotion. Promotion does **not** mean enabling execution. It means:

1. Open an issue / commit-message documenting the replay parameters and the
   labeled-sample stats supporting the move.
2. Adjust the live threshold for that kind in `.env`.
3. Continue logging — the live data is the only data that counts.

---

## 6. Promoting a research signal to executable

Before this is even on the table, **Phase 1 G0 has to be passed** — see
`G0_STATUS.md`. The G0 gate covers operational readiness, not signal quality.

Once G0 is open, to make a research kind executable:

1. Add its `strategy` to `PM_EXECUTION_STRATEGIES` (default
   `struct_arb`). The execution task filters by strategy *before* the risk
   pipeline runs.
2. Implement an `intents_from_signal` mapping for that kind in
   `pm/execution/models.py`. Research signals carry `exec_sets=0` by
   construction, so today they would produce empty plans even if the
   allowlist let them through.
3. Set `PM_EXECUTION_ENABLED=true` and confirm `PM_EXECUTION_MODE=dry_run`.
4. Soak in dry-run mode for at least a week, then review the
   `execution_intents` and `risk_events` tables.
5. Only then consider `PM_EXECUTION_MODE=live`. The `LIVE_TRADING` constant
   in `engine.py` is a separate hard gate.

Don't skip steps. The whole point of three independent gates is that one slip
isn't enough.

---

## 7. Operations

### Dashboard URL

`http://127.0.0.1:8787` — pure HTTP, no auth. Don't expose to the internet.

### Monitor alerts

The monitor watches:
- Heartbeats older than `PM_HB_STALE` (default 120s)
- Engine heartbeat file `data/heartbeat` (process-level liveness)
- `recon_log` rows with `abs_diff > 0.02`
- New `rules_text` rows (resolution wording changed)

If `PM_TG_TOKEN` and `PM_TG_CHAT` are set, alerts go to Telegram. Otherwise the
monitor prints them with `flush=True` so they survive a crash.

### Event log

Append-only JSONL, dated directories under `data/events/`. About 3.6 GB per
24-hour soak with the default 150-market universe. Rotate / archive yourself —
the system does not delete anything.

### Kill switch

Create the file `data/KILL_SWITCH` (any content). The risk manager checks for
it on every plan and refuses if present. Use this for emergency stops without
killing the engine process.

### Recovering from a crash

Just restart the engine. The state DB is WAL-mode SQLite and the event log is
append-only; both are recoverable. The labeler will catch up on its next pass.
There is no orphaned-order recovery yet because there are no orders yet.

---

## 8. Things that can go wrong

| symptom | likely cause | fix |
|---|---|---|
| WS keeps reconnecting at startup | metadata sync blocking the loop | reduce `PM_TRACK_TOP_N` |
| `heartbeat stale` alert for a long-cadence task | the task only beats at end-of-cycle | idle-beat is already in place; check for an exception |
| `recon drift > 0.10` repeatedly on one token | tick size change, dead market | `recon_log` has the asset_id; remove from tracking if dead |
| `partition_sum_drift` never fires | groups need 30 samples over 1800s baseline | normal — give it time |
| dashboard `engine_heartbeat.stale=true` | engine.py crashed | check `data/engine.log`, restart |
| `outcome` stays NULL on old signals | labeler can't find live books for the legs | normal — half-rule requires at least half legs labelable |

---

## 9. The shape of a healthy day

- Engine running for 24h, no reconnect storms
- ~1 GB of new event-log data
- 100–500 new signals across all strategies
- Labeler caught up: `outcome IS NOT NULL` for everything older than
  `PM_LABEL_HORIZON`
- Monitor sent 0 alerts (or 1 informational rules-changed alert)
- Dashboard: all components green, recon drift < 0.02

A healthy week is the same, seven times.

---

## 10. Where to look next

- `CLAUDE.md` — architectural overview and invariants
- `PHASES.md` — the gated roadmap (Phase 0 → Phase 5)
- `G0_STATUS.md` — checklist for opening Phase 1
- `PLAYGROUND.md` — scratch space for experiments
- `pm/signals/*.py` — the scanners themselves; each module's docstring
  describes the contract
- `pm/backtest/signal_replay.py` — the offline replay harness
