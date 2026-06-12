# Self-critique — threats to validity

This is the honest list of every place this study can be wrong. It is the
hardest part of any research project and the part most often missing from
published work. None of the threats below are fully resolved — they are
documented, partially mitigated, and explicitly flagged in the results.

A reader of the final paper should read this file *first*.

## A. Threats to internal validity

### A1. The labeler half-rule introduces selection bias

**The problem.** A signal is labeled only when at least half of its legs
have a live fresh book at label time. Markets that resolved, delisted, or
went quiet between signal time and label time are excluded. These
exclusions are *not random*: a market resolving mid-horizon may have been
informationally hot at signal time (an event triggered both the signal
and the resolution).

**Magnitude.** In the preliminary sample, roughly 3–10% of signals fall
out of labeling, depending on kind. For struct_arb the rate is currently
zero because partition groups rarely resolve mid-horizon; for trade_through
on event-driven markets it has been higher (33% in the preliminary look:
n=92 emitted, n=59 labeled).

**Mitigation.** We report `n_excluded` alongside `n_lab` in every results
table. We also report a sensitivity check: what does the conclusion look
like if all excluded signals had outcome 0? Outcome +mean? Outcome -mean?
A robust finding survives all three.

**Residual risk.** Even with sensitivity bounds we cannot know the
*causal* distribution of excluded signals. This is a fundamental limit of
the design.

### A2. Survivorship bias in the active universe

**The problem.** We sample the top-150 most-liquid *currently active*
markets. Markets that were liquid yesterday but resolved today are gone
from the universe. The Polymarket Gamma `/events` endpoint does not
serve historical universes; we can only see today's.

**Effect.** The universe we are sampling drifts over the sample period.
Signals that fired on now-resolved markets are *in* the signal_log; the
markets themselves are *not* in the metadata snapshot.

**Mitigation.** We snapshot the markets table at the start and end of
the sample period and report any drift. We retain metadata about
resolved markets in the historical signal_log because the legs carry
`market_id` references.

### A3. Tick-size discretisation produces zero-mass at outcome = 0

**The problem.** Polymarket mids tick in $0.005 increments. Over a 15-min
horizon on an illiquid market, the mid often does not move at all. 85% of
ofi_pressure outcomes in the preliminary sample are exactly zero.

**Effect on metrics.** Raw hit rate `#{O > 0} / n_lab` is misleadingly
low. A signal that predicts continuation can have outcome = 0 (no move)
which counts as "wrong" under > 0 but is informationless under ≠ 0.

**Mitigation.** Primary metric for kinds with > 50% zero outcomes is the
*conditional-on-movement* hit rate `H_cond = #{O > 0 | O ≠ 0} / n_moved`.
We report both H_raw and H_cond and use the sign test on the nonzero
subset (Wilcoxon) rather than the t-test on the full sample.

**Residual risk.** Conditioning on movement is itself a selection. A
signal that *causes* the market not to move (e.g. by being a known
public quote) is excluded under conditional analysis. We do not believe
this is a strong concern here but mention it.

### A4. Look-ahead bias risk in replay (NOT in live)

**The problem.** `pm/backtest/signal_replay.py` rebuilds books from the
log but uses the *current* `markets` table for metadata (categories,
NegRisk groupings). A market that became part of a NegRisk group
*after* the events being replayed will be incorrectly grouped.

**Effect.** Only affects the offline-replay analysis used for threshold
tuning. The live signal_log (used for the primary tests) is unaffected
because metadata at signal time was correct.

**Mitigation.** We use the live signal_log as the primary data source.
Replay-derived statistics are clearly labeled as such in the report.

### A5. Look-ahead in directional_momentum's `(len(rets))^0.5` normalisation

**The problem.** The z-score normalises drift by `vol * sqrt(N)`. This
random-walk null assumes IID returns; if the returns are mean-reverting
(plausible for prediction-market mids) the null is too tight and we
will over-emit signals when there is no real drift, biasing the result
toward H_cond = 0.5.

**Effect.** Over-emission means we are testing a weaker signal than
intended. If a real result emerges it is conservative.

**Mitigation.** We have not corrected this in production. We note it in
the results. A follow-up study should use an empirical-vol estimator
that accounts for autocorrelation.

### A6. Boundary_overshoot bounce window definition

**The problem.** The scanner requires *every* sample in the window to be
beyond the boundary, then a bounce in the latest sample. With a 300-
second window at typical update rates (~1/s for a tracked token), this
is ~300 samples — most markets near a boundary will have *some* sample
that briefly slips inside the boundary even if structurally pinned. The
result is that the signal fires *rarely* and biases toward markets that
are *extremely* pinned.

**Effect.** Sample size for boundary_overshoot is going to be small. The
signal kind may be too restrictive to ever reach n ≥ 30 in our sample.

**Mitigation.** We add a robustness scan with a relaxed condition
(`> 90% of samples beyond boundary` instead of `all`) in
`pm/research/sensitivity.py` (TODO). The primary analysis uses the strict
definition as preregistered.

### A7. Multi-leg signal outcome dilution

**The problem.** For partition_sum_drift the signal carries N legs (one
per group member) but only the *laggard* leg has the predictable move
according to the hypothesis. Averaging across all N legs dilutes the
informative leg's contribution by ~1/N.

**Effect.** Underestimates true edge of partition_sum_drift.

**Mitigation.** We add a *laggard-leg specific* outcome alongside the
group-averaged outcome (specified in `HYPOTHESES.md` H8). The features
dict carries `laggard_token`; we read it back at analysis time.

### A8. Identical-direction OFI for YES and NO of same market

**The problem.** When OFI fires on a market's YES token *and* its NO
token simultaneously (both have bid-heavy books, say), both signals
predict their respective tokens go *up* — which is internally
contradictory because YES + NO = 1.

**Effect.** Some pairs of OFI signals on complementary tokens of the
same market cancel by construction. Their joint forward returns are
not independent.

**Mitigation.** We treat each token-signal as a separate sample for the
primary analysis but report the *paired* statistic when both legs of a
market fire within the same minute. Independence violations are
pre-flagged.

## B. Threats to external validity

### B1. Sample period regime dependence

**The problem.** The sample is a multi-day window. Polymarket regime
shifts dramatically across events (elections, major sports, crypto news).
Signals that work in one regime may not work in another.

**Mitigation.** We report per-category breakdowns (sports vs politics vs
crypto). We disclose the calendar window explicitly in the abstract. We
do not claim generalisation beyond the window.

### B2. Polymarket-specific tick-and-fee structure

**The problem.** Results may not transfer to Kalshi, PredictIt, or other
binary venues with different tick sizes and fee models.

**Mitigation.** Stated up front. Generalisability is an open question.

### B3. Top-150-liquid universe

**The problem.** Signals on illiquid markets (rank 151+) may behave
differently. Our findings apply only to the most-liquid tier.

**Mitigation.** Stated. A follow-up could extend the universe.

## C. Threats to construct validity

### C1. "Hit rate" is a noisy estimator of edge

**The problem.** A 51% hit rate with massively negative-skew outcomes
(big losses, small gains) is worse than a 49% hit rate with
positive-skew outcomes. Hit rate alone hides the distribution.

**Mitigation.** We report mean, median, IQR, and a quantile plot of the
outcome distribution per kind, not just hit rate.

### C2. Forward mid-return is not realised P&L

**The problem.** Mid drift ignores spread crossing and impact. A signal
with positive mid drift may have negative realised P&L for a trader who
crosses the spread.

**Mitigation.** Stated explicitly throughout. The study makes no P&L
claim. This is a *signal-detection* study, not a *strategy* study.

### C3. The 15-minute horizon is arbitrary

**The problem.** No theoretical reason 15 minutes is the right horizon
for all 10 kinds. Trade-through (information event) likely has a faster
horizon than directional_momentum.

**Mitigation.** Sensitivity analysis at 5, 15, 60 minutes is committed
in the methodology.

## D. Threats to statistical inference

### D1. Multiple-testing inflation

**The problem.** Ten kinds × two metrics each + sensitivity at three
horizons = 60 tests in the back of the napkin. Without correction,
~3 false positives expected at α = 0.05.

**Mitigation.** Benjamini–Hochberg FDR correction across the ten primary
kinds. Sensitivity-analysis horizons are reported but not used to
declare findings.

### D2. Within-token temporal dependence

**The problem.** Multiple OFI signals on the same token within the same
window are *not independent*. Bootstrap CIs that resample signals iid
will be too tight.

**Mitigation.** Per-token block bootstrap (resample tokens, then take
all signals on the resampled tokens) is committed in `pm/research/
stats.py`. Vanilla bootstrap is reported as a secondary.

### D3. The pre-look at preliminary data could have biased hypotheses

**The problem.** We *did* look at the preliminary data before writing
the hypotheses (`RESULTS_PRELIMINARY.md`). The trade-through reversal
finding is *directly* in the preliminary data; we then preregistered the
sign-test in both directions, which is *not* a directional claim but
also not blind.

**Mitigation.** We disclose this. The honest framing is: the pre-look
informed *which* hypotheses to test, not *what direction to predict*.
The trade-through finding will be replicated on out-of-sample data
post-registration to confirm.

### D4. Sample sizes for struct_arb kinds will be small

**The problem.** True structural arbitrage on a healthy venue should be
rare. We may not reach n_lab ≥ 30 for partition_buy_all in a 14-day
sample.

**Mitigation.** Small-sample exact-binomial CI rather than asymptotic.
We will state plainly that the kind is *underpowered* rather than
declare it "passed" or "failed".

## E. Threats to reproducibility

### E1. Polymarket API drift

**The problem.** Polymarket may change field names, tick sizes, or fee
schedules between our sample and a replication attempt.

**Mitigation.** We pin the metadata snapshot, the fees.yaml version, and
the engine git SHA used for the sample. The event log is the canonical
record; a replication can re-process it deterministically.

### E2. WS dropped events

**The problem.** A WS disconnect can drop events between snapshots. The
WS consumer re-subscribes on reconnect and gets a fresh snapshot, but
deltas in the gap are lost.

**Mitigation.** The recon task cross-checks WS state against REST every
5 minutes. Drift > 1 cent triggers an alert. We report total WS
disconnect time in the operational summary and quantify how many signals
fell in disconnect windows (excluded as a sensitivity check).

### E3. Code-while-running ambiguity

**The problem.** We have been editing the engine *while* it runs the
sample. The momentum scanner, for example, only joined the sample mid-
soak.

**Mitigation.** The signal_log records `ts` and we can intersect with
git history to find the engine SHA at signal time. The momentum kind's
results are reported only over the window in which it was deployed.

## F. Deviations from preregistration

(This section is empty at registration time. Any post-registration change
to hypotheses or methodology will be logged here with date and rationale.)

## G. What honest negative findings would look like

If, after the full sample, no kind survives FDR-corrected significance:

- We report the finding as a *null result on Polymarket microstructure
  signals over 15-min horizons*.
- This is **not** a failure — it is informative for the literature.
- We discuss the most likely reasons: tick-size dominance, regime,
  sample size, horizon mismatch.
- We do not retroactively add post-hoc hypotheses to manufacture a
  positive result.

A study with a clean preregistration and a clean null is more valuable
to the field than a study with motivated reasoning toward a positive
result.
