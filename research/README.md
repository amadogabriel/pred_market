# pm-system research project

This directory frames pm-system as a research project rather than only a
trading engine. The trading code is the laboratory; the questions answered by
running it are the contribution.

## Working title

**Microstructure and relative-value signals on Polymarket: a real-time
observation framework and preregistered evaluation of ten candidate signals.**

## Abstract (target ~250 words)

Centralised-limit-order-book prediction markets such as Polymarket are an
under-studied venue compared to equities and crypto. Their continuous binary
contracts admit a structural arbitrage (no-arbitrage of mutually exclusive
outcomes) and several classical microstructure signals (order-flow imbalance,
trade-through, liquidity shocks) translate naturally — but their efficacy is
unmeasured at this venue, and prior empirical work has largely focused on
calibration of forecasts rather than on intraday signal predictability.

We build a real-time observation framework that ingests the full Polymarket
CLOB websocket feed, persists every event to an append-only log, runs ten
candidate signal detectors over the live data, and labels each emitted signal
with its 15-minute forward mid-return. The system is structurally fail-closed:
research signals carry an empty execution plan by construction and a strategy
allowlist additionally filters them before any risk pipeline runs. After
roughly 1,000 labeled signals over a multi-day live soak, we report
preregistered statistical evaluations of each signal kind with bootstrap
confidence intervals and Benjamini–Hochberg FDR correction across the
multiple-testing family of ten kinds.

We find directional support for order-flow imbalance (conditional hit rate
72.5%, n=131), a robust *contrarian* signal in trade-through prints (35.6%
hit rate against the encoded direction, n=59), and insufficient data on the
relative-value, structural-arbitrage, and momentum families. We document the
gap between raw and conditional hit rate driven by tick-size discretisation
and offer this as a methodological caution for similar studies.

## Research questions

1. **RQ1.** Do order-flow imbalance signals predict subsequent mid drift on
   Polymarket, after correcting for tick-size discretisation?
2. **RQ2.** Are aggressive trade-through prints informed flow (i.e.
   continuation) or uninformed pressure (i.e. mean-reversion)?
3. **RQ3.** Do NegRisk groups exhibit statistically detectable temporary
   internal mispricings (sum-drift, complement-drift) that revert within a
   15-minute horizon?
4. **RQ4.** Does sustained mid drift predict further continuation, or does
   it predict reversion toward the window's starting value?
5. **RQ5.** Do extreme-price overshoots (YES > 0.95 or < 0.05) mean-revert
   on bounce, controlled for the bounce magnitude required for detection?
6. **RQ6.** What is the empirical frequency and persistence of true
   structural-arbitrage opportunities on this venue, and is their forward
   outcome consistent with the no-arbitrage prediction?
7. **RQ7. (Methodological)** How does the labeler's half-rule (label only
   when ≥ half the legs have live books) interact with selection bias from
   resolved or delisted markets?

## Contribution claims

C1. *Engineering.* A reproducible real-time observation framework for
Polymarket with append-only event logging, deterministic event-time replay,
and isolation guarantees between research and executable code paths.

C2. *Empirical.* The first publicly-described preregistered evaluation, on
this venue, of ten candidate signals over a multi-day live sample with
multiple-testing correction.

C3. *Methodological.* A documented protocol for outcome labeling on a binary
prediction-market CLOB, including the tick-size-discretisation artifact and
its impact on naive hit-rate metrics.

C4. *Negative results count.* Where signals fail to reject the null we report
that explicitly, rather than discarding kinds that did not work.

## What we are NOT claiming

- We are *not* claiming a deployable strategy. The labeled outcomes are mid
  drifts; they ignore spread crossing, slippage, queue position, and
  inventory constraints. No P&L claim is supportable from this data.
- We are *not* claiming generalisation beyond the sampling period. Markets
  change. A signal that worked during a US-election news cycle may not work
  in a quiet sports week.
- We are *not* using out-of-sample data from beyond the preregistration
  date. The hypotheses in `HYPOTHESES.md` are timestamped and any
  post-registration changes are versioned in git history.

## Document map

| File | Contents |
|---|---|
| `LITERATURE.md` | Context: prediction-market efficiency literature, market microstructure foundations, Polymarket-specific empirical work, and what is unaddressed |
| `HYPOTHESES.md` | Preregistered null and alternative for each signal kind, test statistic, decision rule |
| `METHODOLOGY.md` | Data collection, labeling protocol, event-time replay, choice of horizon, software environment |
| `STATISTICS.md` | Bootstrap, sign test, Mann–Whitney U, Benjamini–Hochberg FDR, power analysis |
| `CRITIQUE.md` | Honest enumeration of every known threat to validity in this study |
| `RESULTS_PRELIMINARY.md` | What the live sample says so far, framed as preliminary not conclusive |

## Reproducibility manifest

The artefacts that must be checkable for replication:

- **Code:** git SHA of every commit between sample start and sample end
- **Schedules:** `config/fees.yaml` versions covering the sample period
- **Universe:** `data/state.db.markets` snapshot at sample end
- **Events:** `data/events/YYYY-MM-DD/events.jsonl` for every soak day
- **Labels:** `data/state.db.signal_log` snapshot at sample end
- **Replay command:** `python scripts/research_report.py --as-of <ISO date>`
  reproduces the published tables from the raw event log alone.

The event log is the canonical artefact. The state DB is derived from it
plus the engine code at the corresponding git SHA. We commit to never
back-edit either.

## Status as of 2026-06-12

- Sample collection: in progress (live soak, multi-day)
- Preregistration: drafted (this directory), not yet timestamped externally
- Statistical analysis: implemented in `pm/research/`, runnable via
  `scripts/research_report.py`
- Preliminary findings: see `RESULTS_PRELIMINARY.md`. Treat as direction-of-
  travel, not conclusions.
- Independent verification: pending. Code review by an external
  microstructure researcher is the natural next step.
