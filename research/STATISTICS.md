# Statistical analysis plan

The companion implementation lives in `pm/research/stats.py` and is invoked
by `scripts/research_report.py`. This document describes *what* the code
does and *why*, so a reader can audit either against the other.

## 1. Primary point estimates

For each kind we compute:

| Statistic | Definition | Why this one |
|---|---|---|
| `n` | Total emitted signals | descriptive |
| `n_lab` | Labeled signals (outcome ≠ NULL) | denominator for outcome stats |
| `n_moved` | `#{O ≠ 0}` (forward mid moved at all) | needed because tick-size discretisation puts mass at 0 |
| `H_raw` | `#{O > 0} / n_lab` | the naive metric; reported for completeness |
| `H_cond` | `#{O > 0} / n_moved` | primary metric when zero-mass is large |
| `μ` | `mean(O)` over labeled signals | edge magnitude |
| `q50` | `median(O)` | robust to heavy tails |
| `q25, q75` | quartiles | distribution shape |
| `σ` | `stdev(O)` | dispersion |

## 2. Confidence intervals

We report 95% bootstrap CIs computed with 10,000 resamples. For metrics
that depend on a count (`H_raw`, `H_cond`), the resampling is over signals;
for `μ` and quantiles, the same.

**Vanilla bootstrap** — resample with replacement over the n_lab observed
outcomes. Reported as the primary CI.

**Block bootstrap by token** — resample tokens with replacement, then take
all signals on each resampled token. Wider than vanilla because within-
token outcomes are not iid. Reported as a secondary; whichever is wider
is the conservative claim.

For sample sizes below 30 we additionally report the *exact-binomial*
Clopper–Pearson CI for hit-rate metrics, which is conservative and
asymptotically equivalent for large n.

## 3. Hypothesis tests

Per `HYPOTHESES.md`, each kind has a primary test:

### Sign test for `H_cond ≠ 0.5`

Exact binomial test. Two-sided for kinds with no directional pre-prediction
(H5 liquidity_shock, H9 directional_momentum). One-sided for kinds with a
specific direction (H1–H3 struct_arb expect H_cond > 0.5; H4 OFI; H7, H8
relative-value; H10 boundary-overshoot). H6 trade_through tests against
H_cond = 0.5 but reports both directions because the preregistered prediction
is *directional sign-flip* — we expect H_cond < 0.5 against the encoded side.

```
p_value = 2 * binom.cdf(min(k, n-k), n, 0.5)        # two-sided
p_value =     binom.cdf(n-k, n, 0.5)                # H_cond < 0.5
p_value =     binom.sf(k-1, n, 0.5)                 # H_cond > 0.5
```

### Wilcoxon signed-rank test for `μ`

Non-parametric, no normality assumption. Robust to outliers and the heavy
right tail seen in the preliminary OFI outcomes (max +0.18, two orders of
magnitude beyond the median).

### Mann–Whitney U for baseline comparison

For kinds where the directional convention is ambiguous (H5
liquidity_shock), we compare the realised outcome distribution against the
same-token same-time random-direction baseline (`pm/research/baseline.py`).
Mann–Whitney U is the natural two-sample test that handles ties.

## 4. Multiple-testing correction

We have 10 hypotheses in the primary family. The Benjamini–Hochberg
procedure controls the *false discovery rate* — the expected proportion of
declared findings that are actually null.

```
Sort p-values p(1) ≤ p(2) ≤ ... ≤ p(10).
Find the largest k such that p(k) ≤ (k/10) * q   where q = 0.05.
Reject H0 for tests 1..k.
```

We report both raw and BH-adjusted p-values. A kind is declared
*supported* only if the BH-adjusted p < 0.05.

We do **not** use Bonferroni (q = 0.005 per test). Bonferroni is for
strict family-wise error control which is overkill for a screening study.
FDR is the standard in financial-econometrics signal screening (e.g.
Harvey, Liu & Zhu 2016, "And the cross-section of expected returns").

## 5. Time-matched random baseline

For each real signal at time `t` on token `T`, we draw a synthetic signal
at the same `(t, T)` with a *random* direction (BUY or SELL with equal
probability). We compute its forward outcome using the same labeling
protocol. The resulting baseline distribution is the null we test against
for Mann–Whitney U.

We can also reverse-direction the random baseline to a *strict* contrarian
baseline (flip the real signal's encoded side) and test how the realised
signal compares to both pure-random and pure-contrarian. The triple
distribution makes the directional-prediction story explicit.

Implemented in `pm/research/baseline.py`. Seed = 42.

## 6. Power analysis

For a sign test against H_cond = 0.5 with two-sided α = 0.05, the
sample size to detect a true H_cond at given power 1−β is:

```
n_min = ((z_{α/2} + z_β) / (2 * (H_cond - 0.5)))^2
```

Substituting standard values:

| True H_cond | Power 0.8 | Power 0.9 |
|---|---|---|
| 0.55 | 196 | 263 |
| 0.60 | 49 | 66 |
| 0.65 | 22 | 30 |
| 0.70 | 13 | 17 |
| 0.80 | 5 | 7 |

For the preliminary OFI finding (H_cond = 0.725, n_moved = 131), the
current sample is well above the n_min for power 0.9. For other kinds —
especially struct_arb where n is single digits — we are *severely
underpowered* and report that explicitly.

For continuous outcomes (Wilcoxon), we use the Lehr (1992) approximation:

```
n ≈ 16 * σ² / Δ²
```

where Δ is the minimum mean shift we care to detect. For typical
prediction-market mid-drift σ ≈ 0.015 and a target Δ = 0.005, n ≈ 144.

## 7. Effect sizes

In addition to p-values, we report:

- Hit rate: deviation from 0.5
- Mean outcome: μ in price units
- Cohen's *d* analogue: μ / σ
- Total dollars implied (illustrative only): μ × (avg shares per signal)

Effect sizes are what matter for whether a signal is *practically*
meaningful, separate from whether it is *statistically* significant. A
detectable +0.0001 mid-drift on a busy enough kind can be statistically
significant and practically useless.

## 8. Sensitivity analyses

We will run the entire analysis under four perturbations:

1. **Horizon.** 5, 15, 60 minutes.
2. **Half-rule.** Strict (≥ half legs labeled) vs lenient (≥ 1 leg) vs
   strict-strict (all legs).
3. **Subsample by category.** Politics, sports, crypto, other.
4. **Subsample by liquidity tier.** Top-50 vs 51–150.

A finding is *robust* if it is significant under at least 3 of 4
perturbations at the same direction. Robustness is reported alongside the
primary table.

## 9. Reporting

The final table in the paper has, for each kind, one row with:

```
kind | n | n_lab | n_moved | H_cond [95% CI] | μ [95% CI] | p (raw) | p (BH) | verdict
```

`verdict` is one of: `supported`, `not supported`, `inverted` (for
trade-through if the contrarian sign is confirmed), or `underpowered`.

Negative results are reported with the same prominence as positive. The
honest picture is a *family of hypotheses*, some of which the data
supports and some of which it does not.
