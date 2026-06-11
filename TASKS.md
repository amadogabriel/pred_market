# TASKS.md — Phase 0 build checklist

Current state: foundations done (bus, books, db, fee engine, WS consumer, struct_arb scanner).
Goal: Phase 0 gate — 7 consecutive days clean uptime, recon passing, event log running.

Work through these in order. Each task has acceptance criteria; don't move on until they pass.

---

## 1. `pm/ingestion/metadata_sync.py`

Pull the active market universe from Polymarket's Gamma API, persist to `markets` table,
extract NegRisk groups, push token IDs to the WS consumer.

**What to build:**
- Async function `sync_markets(conn, books, ws, settings)` that runs on startup
  and then every `settings.metadata_sync_interval` seconds (default 1h).
- Fetches active markets from `https://gamma-api.polymarket.com/markets`
  with params `?active=true&closed=false&limit=500` (paginate with `next_cursor`).
- For each market, normalize the category tag (lowercase, strip, map to fee engine categories).
- Call `db.upsert_market()` for each market.
- Group markets by `neg_risk_market_id` (field in the Gamma API response) where non-null
  and `neg_risk=true` — these are the NegRisk partition groups. Store as `neg_risk_id`.
- Fetch and store resolution rules text via `db.store_rules()`. If it returns `True`
  (rules changed), log a warning — this will become a Telegram alert later.
- Extract all `clob_token_ids` (yes + no tokens) for markets above `settings.min_liquidity_usd`.
- Call `ws.set_assets(token_ids)` to update the WS subscription universe.
- Call `db.beat(conn, "metadata_sync")` after each successful run.

**Acceptance criteria:**
- Runs without error; `markets` table populated with > 50 rows.
- `neg_risk_id` is non-null for at least some markets (NegRisk groups exist on Polymarket).
- WS consumer receives `book` events after first sync (check `books.__len__() > 0`).
- Re-run is idempotent (upsert, not duplicate insert).

**Notes:**
- Gamma API field names to check: `conditionId` (→ market_id), `question`, `slug`,
  `category` (or `tags`), `endDate`, `active`, `closed`, `negRisk`, `negRiskMarketId`,
  `clobTokenIds` (list, index 0 = yes token, 1 = no), `liquidity`, `volume24hr`.
  Field names may differ — inspect a live response first and adjust accordingly.
- The fee engine needs a normalized category string. Mapping examples:
  `"Politics" → "politics"`, `"Crypto" → "crypto"`, `"Sports" → "sports"`.
  Unknown categories → `None` (fee engine uses default_rate).
- Don't store `clob_token_ids` as a JSON blob — split into `token_yes` and `token_no`.

---

## 2. `pm/ingestion/event_logger.py`

Append every event on the bus to dated JSONL files on disk.
**This must start before anything else runs.** It is the replay dataset.

**What to build:**
- Async task `event_logger_task(bus, events_dir)` that subscribes to `ALL_TOPICS`
  and writes every event to `events_dir/YYYY-MM-DD/events.jsonl`.
- One file per day, append-only, one JSON line per event using `event.to_record()`.
- Rotate cleanly at UTC midnight (just open a new file; old one stays).
- Flush after every write (or use `line_buffering=True`) — don't buffer across crashes.
- No size limit; disk is cheap vs losing the dataset.

**Acceptance criteria:**
- After 60 seconds of the engine running, `data/events/YYYY-MM-DD/events.jsonl` exists
  and contains at least one `book` and one `system` line.
- Each line is valid JSON with keys `ts`, `topic`, `payload`.
- Killing and restarting the engine appends to the existing file, not truncates.

**Notes:**
- `events_dir` comes from `settings.events_dir`. Create the date subdirectory if needed.
- Import `ALL_TOPICS` from `pm.core.events`.

---

## 3. `pm/ingestion/rest_recon.py`

Periodically check WS book state against REST prices. Flags drift; doesn't auto-correct.

**What to build:**
- Async task `recon_task(conn, bus, books, settings)` that runs every
  `settings.recon_interval` seconds (default 300s = 5 min).
- For a sample of tracked markets (up to 20, random subset), fetch the current
  best bid/ask from `https://clob.polymarket.com/midpoints?token_id=TOKEN_ID`
  or `https://clob.polymarket.com/book?token_id=TOKEN_ID` (check which is cheaper/faster).
- Compare REST best_bid/best_ask to `books.peek(token_id).best_bid()` / `best_ask()`.
- Write each diff to `recon_log` table: `(ts, token_id, field, ws_value, rest_value, diff)`.
- If `abs(diff) > 0.02` (2 cents), publish a `system` event with `what="recon_drift"`.
- Call `db.beat(conn, "rest_recon")` after each run.

**Acceptance criteria:**
- After first run, `recon_log` has rows.
- Diffs are small (< 0.01) under normal conditions — if they're large, the WS parsing
  has a bug (fix `ws_polymarket._handle_raw` or `books.handle_ws_message`).

---

## 4. `pm/signals/scan_task.py`

Async task that runs the struct_arb scanner on every book update and logs signals.

**What to build:**
- Async task `scan_task(bus, conn, books, fee_engine, settings, neg_risk_groups)`
  where `neg_risk_groups: dict[str, list[dict]]` is populated by metadata_sync.
- Subscribes to `T_BOOK` and `T_PRICE_CHANGE` topics on the bus.
- On each event, identify which markets were updated (from `msg["asset_id"]`).
- For each updated market:
  - Run `scanner.scan_complement(market_id, token_yes, token_no, category)`.
  - If the market belongs to a NegRisk group, run `scanner.scan_partition(group_id, legs_meta)`.
- For each signal where `scanner.should_emit(sig)` returns True:
  - Call `db.log_signal(conn, ...)` to persist it.
  - Publish `Event(T_SIGNAL, {"strategy": "struct_arb", "signal_id": sid, ...})` on the bus.
- Call `db.beat(conn, "scan_task")` every 60 seconds regardless of signal activity.

**Acceptance criteria:**
- Task runs without error for 10+ minutes.
- `signal_log` table accumulates rows over time (or stays empty — that's fine,
  it means no arb exists right now, which is expected).
- No `RuntimeError` from lossless queue overflow (signals are consumed by event_logger).

---

## 5. `engine.py` (root of repo)

Asyncio supervisor that wires everything together. Entry point.

**What to build:**
```python
# engine.py — pseudocode structure
async def main():
    settings = Settings()
    conn = db.connect(settings.db_path)
    bus = Bus()
    books = BookStore()
    fee_engine = FeeEngine.from_yaml(settings.fees_yaml)
    ws = PolymarketWS(settings.pm_ws_url, bus, books, settings.ws_assets_per_conn)

    async with asyncio.TaskGroup() as tg:
        tg.create_task(event_logger_task(bus, settings.events_dir), name="event_logger")
        tg.create_task(metadata_sync_loop(conn, books, ws, settings), name="metadata_sync")
        tg.create_task(recon_task(conn, bus, books, settings), name="rest_recon")
        tg.create_task(scan_task(bus, conn, books, fee_engine, settings, neg_risk_groups), name="scan")
        tg.create_task(heartbeat_task(conn, settings), name="heartbeat")

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(main())
```

- `neg_risk_groups` should be a shared mutable dict that `metadata_sync` populates
  and `scan_task` reads — a plain `dict` is fine since asyncio is single-threaded.
- `heartbeat_task` just calls `db.beat(conn, "engine")` every `settings.heartbeat_interval` seconds
  and writes the timestamp to `settings.heartbeat_path` (a file the monitor process reads).
- On `KeyboardInterrupt`, log "shutting down" and exit cleanly.
- Set `PYTHONASYNCIODEBUG=1` in dev to catch unawaited coroutines.

**Acceptance criteria:**
- `python engine.py` starts, logs WS connections, and runs without crashing.
- `data/state.db` exists and has populated tables.
- `data/events/YYYY-MM-DD/events.jsonl` is growing.
- `data/heartbeat` file is updated every ~15 seconds.

---

## 6. `monitor.py` (root of repo)

Separate process. Reads heartbeat file + DB; sends Telegram alerts.
Survives engine crashes (that's the point).

**What to build:**
- Reads `settings.heartbeat_path` every 30 seconds.
- If the file is missing or its mtime is > `settings.heartbeat_stale_after` seconds old (default 120s):
  alert "⚠️ engine heartbeat stale — may be down".
- Also calls `db.stale_components(conn, max_age=120)` and alerts on any stale component.
- Telegram send function: `POST https://api.telegram.org/bot{TOKEN}/sendMessage`
  with `{"chat_id": CHAT_ID, "text": msg}` via `aiohttp`. If `PM_TG_TOKEN` is empty,
  just `print()` instead (so it works without Telegram configured).
- Alert on: engine stale, WS disconnected (from `system` events in DB... or just heartbeat),
  `recon_drift` (query `recon_log` for recent large diffs), rules changed (`rules_text` inserts).
- Call its own `db.beat(conn, "monitor")` so the monitor is also in the heartbeat table.

**Acceptance criteria:**
- `python monitor.py` runs in a second terminal alongside `engine.py`.
- Kill the engine — within 2 minutes, monitor prints/sends "engine heartbeat stale".
- Restart engine — alert clears on next check.

---

## 7. Tests — `tests/test_fee_engine.py` and `tests/test_books.py`

These two components are load-bearing; they need exhaustive tests.

**`tests/test_fee_engine.py`:**
```python
# Cover:
# - geopolitics fee is always 0
# - peak fee at p=0.50 matches published rates per category
# - fee at p=0.01 and p=0.99 is near zero
# - maker fee is always 0 for polymarket
# - kalshi rounds up to cent
# - min_edge = fee(1 share) + buffer
# - wrong venue raises KeyError
# - schedule versioning: date before earliest schedule raises KeyError
# - schedule versioning: picks the latest schedule on or before the query date
```

**`tests/test_books.py`:**
```python
# Cover:
# - apply_snapshot replaces state
# - apply_level size=0 removes level
# - apply_level size>0 updates level
# - best_bid returns highest bid price
# - best_ask returns lowest ask price
# - is_stale returns True after max_age seconds (use time.sleep or mock time)
# - handle_ws_message routes book events to apply_snapshot
# - handle_ws_message routes price_change events to apply_level
# - depth_at_or_better sums correctly
```

Run with: `python -m pytest tests/ -v`

---

## 8. `systemd/pm-engine.service` and `systemd/pm-monitor.service`

For production deployment on the VPS.

**`pm-engine.service`:**
```ini
[Unit]
Description=pm-system engine
After=network.target
Wants=pm-monitor.service

[Service]
Type=simple
User=pm
WorkingDirectory=/home/pm/pm-system
EnvironmentFile=/home/pm/pm-system/.env
ExecStart=/home/pm/.venv/bin/python engine.py
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

**`pm-monitor.service`:** same structure but `ExecStart` runs `monitor.py`,
`Restart=always` (monitor should never stay down), no `Wants=`.

**`.env.example`:**
```
PM_DB_PATH=/home/pm/pm-system/data/state.db
PM_EVENTS_DIR=/home/pm/pm-system/data/events
PM_TG_TOKEN=your-bot-token-here
PM_TG_CHAT=your-chat-id-here
PM_TRACK_TOP_N=150
```

---

## Phase 0 gate — G0

All of the above done. Then run for 7 consecutive days and verify:

- [ ] Engine runs 7 days without manual restart
- [ ] WS auto-reconnects after simulated drop (`sudo iptables -A OUTPUT -p tcp --dport 443 -j DROP` for 60s then remove)
- [ ] Fee engine matches 20 hand-computed real trades from the Polymarket UI
- [ ] `recon_log` shows diffs < 0.01 consistently (or WS parsing bug is fixed first)
- [ ] Event log growing daily; each day's file is valid JSONL
- [ ] Monitor correctly alerts on stale heartbeat

Once G0 passes → move to Phase 1 (struct_arb live trading, small cap).
Do NOT start Phase 1 without passing G0.

---

## Coding conventions

- All async tasks: `while True` loop with `except asyncio.CancelledError: raise` and
  exponential backoff on other exceptions. Never let a task silently die.
- Every task heartbeats via `db.beat(conn, task_name)`. No exceptions.
- Logging: `log = logging.getLogger(__name__)` in every module. Use `log.info` for
  normal events, `log.warning` for recoverable problems, `log.error` for things that
  need human attention. No bare `print()` except in monitor's Telegram fallback.
- Type hints on all public functions. `from __future__ import annotations` at top of each file.
- No global mutable state except the `neg_risk_groups` dict shared between metadata_sync
  and scan_task (acceptable since asyncio is single-threaded).
- Never `import *`.
- If a function needs the fee engine, pass it as a parameter — don't instantiate it inside.
