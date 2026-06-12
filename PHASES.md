# Phase Roadmap

This repo remains fail-closed. Phase 0 signal capture can run now; later phases
are implemented as disabled-by-default infrastructure until their gates pass.

## Phase 0: Observe

Status: implemented.

- Sync Polymarket metadata
- Maintain live books from WS
- Reconcile WS against REST
- Log all bus events to JSONL
- Detect and persist structural-arb signals
- Run dashboard and monitor

Gate G0 still requires the items in `G0_STATUS.md`: seven-day soak, VPS
reconnect drill, fee audit, clean recon, event-log validation, and monitor
stale-alert verification.

## Phase 0.5: Research Signals + Outcome Labeling

Status: implemented (signal capture only; never executable).

- S2 microstructure (`pm/signals/microstructure.py`): ofi_pressure,
  liquidity_shock, trade_through
- S3 relative value (`pm/signals/relative_value.py`): partition_sum_drift
  (with mover/laggard attribution), complement_drift
- S4 momentum (`pm/signals/momentum.py`): directional_momentum (z-scored
  drift against own per-step vol), boundary_overshoot (extreme price +
  interior-direction bounce)
- Outcome labeler (`pm/signals/labeler.py`): fills signal_log.outcome/pnl
  with forward mid returns after PM_LABEL_HORIZON
- Offline replay harness (`scripts/replay_signals.py`): re-runs the scanners
  over the JSONL event log with event-time clocks and evaluates forward
  returns from the log itself — threshold tuning without live soak

Isolation guarantees: research signals carry exec_sets=0 (empty execution
plan by construction) AND their strategies are absent from
PM_EXECUTION_STRATEGIES, so the execution task drops them before any risk
machinery runs. Promotion path: sustained positive labeled outcomes →
review → add strategy to the allowlist (Phase 1 gates still apply).

## Phase 1: Controlled Execution

Status: scaffolding implemented, disabled by default.

- `pm.execution.task` subscribes to `signal` events
- `pm.execution.models` converts signals into order intents
- `pm.execution.risk` blocks unsafe plans
- `pm.execution.broker.DryRunBroker` records no exchange-side activity
- Live broker intentionally fails closed

Activation gates:

- G0 passed
- `config/verified_negrisk_groups.txt` populated for any partition execution
- `PM_EXECUTION_ENABLED=true`
- dry run validated first with `PM_EXECUTION_MODE=dry_run`
- live mode reviewed separately; engine hard gate is still `LIVE_TRADING=False`

## Phase 2: Execution Hardening

Status: primitives implemented.

- Intent/fill/position/risk-event tables
- Client order ids
- Dry-run broker receipts
- Position accounting from fills
- Dashboard execution-intent and risk-event visibility

Still required before live mode:

- Real Polymarket broker adapter
- Cancel/replace handling
- Partial-fill polling
- Exchange balance reconciliation
- Idempotent restart recovery for open orders

## Phase 3: Risk and Sizing

Status: first-pass risk gates implemented.

- Per-order notional cap
- Per-signal notional cap
- Open-notional cap
- Daily realized-loss cap
- Recent recon-drift block
- Kill switch file
- No shorting without inventory
- Verified NegRisk allowlist

Still required before live mode:

- Portfolio-level exposure by category and market group
- Settlement/unwind PnL attribution
- Balance-aware sizing
- Operator approval flow for new groups

## Phase 4: Research and Backtesting

Status: replay/reporting substrate implemented.

- `pm.backtest.replay` iterates and summarizes event logs
- `scripts/replay_events.py` reports replay coverage
- `scripts/daily_report.py` prints operational state

Still required:

- Deterministic order-book reconstruction from event logs
- Strategy replay with latency/slippage assumptions
- Meta-label training from `signal_log`

## Phase 5: Production Operations

Status: operating docs and systemd units exist.

- Engine, monitor, and dashboard systemd units
- `.env.example` includes later-phase risk gates
- `scripts/g0_gate.py` automates G0 evidence checks

Still required:

- VPS deployment
- Telegram credentials
- Backup/retention policy for `data/events`
- Daily report automation
- Monthly fee/rules verification calendar

## Current Safety Posture

No live orders can be placed from the current engine:

- `PM_EXECUTION_ENABLED` defaults to `false`
- `PM_EXECUTION_MODE` defaults to `dry_run`
- `PM_LIVE_TRADING` defaults to `false`
- `engine.py` passes `hard_live_gate=False` through `LIVE_TRADING = False`
- live broker implementation returns a failure receipt instead of submitting
