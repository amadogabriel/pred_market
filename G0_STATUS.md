# G0 Status

Updated: 2026-06-12 Asia/Manila.

Phase 0 build items in `TASKS.md` sections 1-8 are code complete. The remaining
items are the G0 gate checks.

## Automated checks

Run:

```powershell
.\.venv\Scripts\python.exe scripts\g0_gate.py
```

Use strict mode when the final gate should fail on any blocked item:

```powershell
.\.venv\Scripts\python.exe scripts\g0_gate.py --strict
```

The script validates:

- engine heartbeat freshness
- state DB population and component heartbeats
- `recon_log` max absolute diff under 0.01
- event-log JSONL validity, with `book` and `system` events present
- monitor stale-heartbeat alert behavior using a temporary DB/heartbeat
- optional 7-day soak marker
- optional 20-trade manual fee audit CSV

## Still required for full G0

- 7 consecutive days of engine uptime. Start the local marker with:

```powershell
.\.venv\Scripts\python.exe scripts\g0_gate.py --start-soak
```

- WS reconnect drill on the Linux VPS. The requested `iptables` drop test is a
  production-network drill and should not be run from this Windows desktop.
- 20 hand-checked fee examples from the Polymarket UI. Put them in
  `data/g0_fee_audit.csv` with these columns:

```csv
venue,category,price,shares,is_taker,expected_fee,tolerance,source
polymarket,politics,0.50,100,true,1.00,0.0001,manual UI check URL or note
```

`data/` is intentionally ignored by git, so the fee-audit evidence remains local
unless you choose to publish it separately.
