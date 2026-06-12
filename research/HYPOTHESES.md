# Preregistered hypotheses

**Registration date:** 2026-06-12. Git SHA: see `git log --grep=HYPOTHESES` for
the registration commit. Changes after that commit are versioned in git and
flagged in `CRITIQUE.md` § Deviations.

The hypotheses below are stated *before* the formal analysis. The preliminary
look documented in `RESULTS_PRELIMINARY.md` was used only to choose which
hypotheses to test, not to fit them — we did not pick directions based on the
preview data.

## Notation

- `n` — total signals of this kind in the sample
- `n_lab` — labeled signals (forward-return computable)
- `n_moved` — labeled signals where forward return ≠ 0 (mid actually moved)
- `O_i` — labeled forward-return outcome of signal `i`, sign-adjusted so
  positive means the encoded direction was correct
- `H_raw = #{O_i > 0} / n_lab` — raw hit rate
- `H_cond = #{O_i > 0 | O_i ≠ 0} / n_moved` — conditional-on-movement hit rate
- `μ = mean(O_i)` over labeled signals (uncondtional)
- Significance level: `α = 0.05`, FDR-corrected across the family of 10
  kinds (Benjamini–Hochberg)

The conditional-on-movement hit rate `H_cond` is the primary metric for kinds
with > 50% zero-outcomes (OFI, partition_sum_drift). For kinds where most
forward returns are nonzero (trade_through, liquidity_shock), `H_raw` and
`H_cond` coincide and the primary metric is `H_raw`.

## H1. Structural arbitrage — partition_buy_all

**Null H0_1.** Forward outcomes are zero-mean.
**Alt H1_1.** μ > 0.
**Test.** One-sample t / Wilcoxon signed-rank against 0, one-sided.
**Pre-prediction.** *Strong* — this is the no-arbitrage prediction itself.
If μ ≤ 0, either the group was not exhaustive, the buffer was too small, or
something is genuinely broken in the fee model. We expect μ ≥ buffer with
very high probability.
**Decision rule.** Reject H0 at adjusted p < 0.05 *and* require n_lab ≥ 30
for a meaningful claim. Smaller samples are reported but do not "pass."

## H2. Structural arbitrage — partition_sell_all

**Null H0_2.** Same.
**Alt H1_2.** μ > 0.
**Pre-prediction.** Same as H1 in principle. In practice we expect very few
sell_all opportunities because they require inventory that the system does
not hold — this kind is *flagged as needs_inventory* and rarely fires.

## H3. Structural arbitrage — complement (single-market YES + NO < 1 − buffer)

**Null H0_3.** μ = 0.
**Alt H1_3.** μ > 0.
**Pre-prediction.** Strong, same as H1. Single-market complement violations
are the *minimal* arbitrage check; if these don't pay off, the no-arbitrage
hypothesis is in deep trouble.

## H4. Microstructure — ofi_pressure (RQ1)

**Null H0_4a.** H_cond = 0.5 (movements are random given a signal fired).
**Alt H1_4a.** H_cond > 0.5.
**Null H0_4b.** μ ≤ 0.
**Alt H1_4b.** μ > 0.

**Tests.**
- H_cond: one-sided binomial test on (#positives among moved) vs n_moved
- μ: one-sample Wilcoxon signed-rank against 0, one-sided

**Pre-prediction.** *Moderate.* The literature on equities supports OFI as a
short-horizon predictor. On a binary tick-discretised CLOB the effect may
be weaker. We pre-predict H_cond in [0.55, 0.70].

**Decision rule.** Reject H0_4a if adjusted p < 0.05 *and* n_moved ≥ 50. The
μ test is secondary.

## H5. Microstructure — liquidity_shock

**Null H0_5.** H_raw is symmetric around 0.5; the absolute forward return is
not different from the unconditional absolute return.
**Alt H1_5.** Two-sided departure from null in either direction.

**Pre-prediction.** *Weak.* The literature is divided on whether liquidity
shocks predict continuation or reversion. We do not pre-predict a sign; we
test only that liquidity_shock samples differ from a same-token same-time
random baseline (see `baseline.py`).

**Test.** Mann–Whitney U vs baseline outcomes, two-sided.

## H6. Microstructure — trade_through (RQ2)

**Null H0_6.** Trade-through prints predict continuation in the encoded
direction. Operationally: H_cond > 0.5 against the current encoding.
**Alt H1_6.** H_cond < 0.5 — i.e. the prints are *contrarian*, and the
direction convention should be inverted.

**Pre-prediction.** *Specifically directional.* On equities the theory
predicts H_cond > 0.5. Our preliminary look suggests the opposite on
Polymarket; we preregister the directional test in both directions and let
the data decide.

**Test.** Two-sided sign test against H_cond = 0.5.

**Decision rule.** If H_cond is statistically significantly below 0.5, we
conclude trade-through is a contrarian indicator and recommend flipping the
sign of the encoded direction in production. (This is a *configuration*
change, not a code change; the scanner still emits the signal.)

## H7. Relative value — complement_drift (RQ3a)

**Null H0_7.** YES_mid + NO_mid stays within ±fees of $1.00 in expectation;
deviations are pure noise.
**Alt H1_7.** When deviations occur, they revert: μ > 0 in the encoded
direction (the encoded side is the rich/cheap leg pair).

**Pre-prediction.** *Moderate.* This is essentially a single-market
arbitrage; we expect μ > 0 weakly. Frequency of this signal is the
limiting factor; we may not reach n_lab ≥ 30.

## H8. Relative value — partition_sum_drift (RQ3b)

**Null H0_8.** Group YES-mid sum stays within its rolling baseline ±2σ;
sustained z > 3 events are pure noise.
**Alt H1_8.** Sustained z > 3 events revert: the laggard token moves toward
the mover's direction within horizon.

**Pre-prediction.** *Weak.* The construction is novel; we have no strong
prior. Sample size is likely the binding constraint.

**Special metric.** For this kind, we additionally tabulate the
*per-laggard-leg* forward return (the leg with the oldest book update at
signal time, identified in the features) rather than just the group-averaged
outcome. We expect the laggard to be the leg with the predictable move.

## H9. Momentum — directional_momentum (RQ4)

**Null H0_9.** Sustained drift over the window is uninformative about the
next 15 minutes; H_cond = 0.5.
**Alt H1_9.** Either continuation (H_cond > 0.5, encoded direction
correct) or reversion (H_cond < 0.5).

**Pre-prediction.** *Two-sided, uncommitted.* Equity momentum is well
documented; prediction-market momentum is *not* obviously the same. Bounded
[0,1] prices, approaching-resolution effects, and event-driven repricing
plausibly make momentum a reverting signal here. We pre-register the
two-sided test.

## H10. Momentum — boundary_overshoot (RQ5)

**Null H0_10.** Bounces back from > 0.95 / < 0.05 do not predict further
interior movement: μ = 0.
**Alt H1_10.** Bounces continue: μ > 0 in the encoded direction (which is
the interior-direction the bounce is moving).

**Pre-prediction.** *Moderate-positive.* Near-boundary prices are
near-certainty regimes; a bounce often reflects a probability update toward
contestability. Frequency is likely low.

## Family of tests and FDR correction

We have **10 hypotheses** in the primary family (H1–H10). We use the
Benjamini–Hochberg procedure at FDR q = 0.05 to control the expected
proportion of false discoveries across the family.

For two-sided tests (H6, H9) we use the symmetric p-value and apply
FDR-correction at the kind level (not the direction level).

## Reporting commitments

For every hypothesis, regardless of significance, we report:

- n, n_lab, n_moved
- H_raw and H_cond with 95% bootstrap CIs (10,000 resamples)
- μ with 95% bootstrap CI
- raw p-value
- BH-corrected p-value
- preregistered direction vs realised direction
- a one-sentence verdict: *supported* / *not supported* / *insufficient
  power*

Negative results are not buried. If a signal fails to reject the null we
say so plainly. If a signal yields a directional result *opposite* the
preregistered prediction (the trade-through case), we report both the
finding *and* the prediction it falsifies.

## Power and stopping

The sample is collected as a continuous live soak; we are not stopping
based on interim results. We will run the FDR-corrected analysis at a
single point of time — when n_lab ≥ 1,500 across all kinds *or* the soak
reaches 14 days, whichever comes first.

The exception is the structural-arbitrage kinds (H1, H2, H3) where n is
constrained by opportunity frequency and 14 days may produce n_lab < 30.
For these we will report the small-sample exact-binomial CI and state
explicitly that the sample is underpowered.
