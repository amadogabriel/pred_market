# Playground

Use this to explore the blocked later phases without sending orders anywhere.
Everything is local SQLite state, tagged as `strategy='playground'`, and uses
synthetic `PLAY_*` token ids.

## Reset

```powershell
.\.venv\Scripts\python.exe scripts\playground.py reset
```

## Approved dry-run signal

Creates a synthetic complement signal, passes risk checks, records two dry-run
submitted intents, and makes them visible in the dashboard.

```powershell
.\.venv\Scripts\python.exe scripts\playground.py scenario approved
```

## Risk-blocked examples

Execution disabled:

```powershell
.\.venv\Scripts\python.exe scripts\playground.py scenario blocked-execution
```

Live mode blocked by the hard engine gate:

```powershell
.\.venv\Scripts\python.exe scripts\playground.py scenario live-blocked
```

Unverified NegRisk partition:

```powershell
.\.venv\Scripts\python.exe scripts\playground.py scenario partition-rejected
```

Per-order notional limit:

```powershell
.\.venv\Scripts\python.exe scripts\playground.py scenario order-limit
```

## Paper fill

After `scenario approved`, fill the latest submitted playground intent and
populate `execution_fills` plus `positions`.

```powershell
.\.venv\Scripts\python.exe scripts\playground.py paper-fill
```

## Fee audit template

```powershell
.\.venv\Scripts\python.exe scripts\playground.py fee-audit-template
```

The dashboard will update at:

```text
http://127.0.0.1:8787/
```

Real live execution remains blocked by G0/G1 gates and is not affected by this
playground script.
