# Preliminary results

**As of 2026-06-12.** Live sample is still in progress; the figures below are
a snapshot for context and methodology illustration. They are *not* the final
analysis. Final results require the full sample period specified in
`HYPOTHESES.md`.

## Reproducing this section

```powershell
python scripts/research_report.py --boot 10000
```

The numbers below were produced from the same script with a smaller bootstrap
count for speed (`--boot 2000`); 10,000 resamples are committed for the final.

## Headline result — OFI

**Order-flow imbalance predicts subsequent mid drift on Polymarket.**

- n = 895 signals emitted, n_lab = 859 labeled, n_moved = 135 with nonzero
  forward outcome
- H_cond (conditional-on-movement hit rate) = **70.4%**, 95% CI [62%, 78%]
- mean forward return = +0.0004 (within 1 cent — small in magnitude)
- one-sided sign-test p < 1 × 10⁻⁶
- Benjamini–Hochberg adjusted p < 1 × 10⁻⁶
- **Verdict: supported** under the preregistered decision rule

Notes:

- The H_raw of 11% is a tick-size discretisation artefact (85% of forward
  returns are exactly zero). The conditional-on-movement metric is the
  scientifically meaningful one and is the primary metric per `STATISTICS.md`.
- The mean is small but the CI does not cross zero, so the *direction* is
  reliable even if the *magnitude* is unimpressive.
- The implied edge is on the order of one tick (0.5 cents) over 15 minutes.
  Whether this is exploitable after spread-crossing and fees is *not* a
  question this study addresses.

## Headline result — trade-through (contrarian, but FDR-borderline)

- n = 92, n_lab = 59, n_moved = 59 (no zero outcomes — these are
  information events on actively-trading markets)
- H_cond = **35.6%**, 95% CI [24%, 49%], against the encoded direction
- mean forward outcome = −0.025 (the price moves against the encoded
  direction by 2.5 cents on average)
- two-sided sign-test p = 0.036
- Benjamini–Hochberg adjusted p = **0.127** — does *not* survive FDR
  correction at q = 0.05

**Verdict: not supported** at the preregistered FDR level. The raw
p-value is consistent with a contrarian story but the FDR-corrected
p-value is not significant. We report the finding as *directionally
suggestive* and pre-commit to revisiting it with the larger sample.

This is exactly the kind of result FDR correction is designed to catch:
the raw p < 0.05 was tempting but the family of 10 tests inflates the
expected false-discovery count. The post-sample collection will resolve
whether the contrarian effect is real or sampling noise.

## Underpowered kinds (no verdict yet)

| Kind | n_lab | n_moved | Reason underpowered |
|---|---|---|---|
| struct_arb / partition_buy_all | 1 | 1 | true arbitrage is rare |
| struct_arb / partition_sell_all | 1 | 1 | needs inventory; rare |
| struct_arb / complement | 0 | 0 | none observed yet |
| rel_value / complement_drift | 3 | 3 | rare event in healthy markets |
| rel_value / partition_sum_drift | 2 | 2 | needs 30-sample baseline + group activity |
| momentum / directional_momentum | 0 | 0 | scanner deployed mid-soak; needs more time |
| momentum / boundary_overshoot | 0 | 0 | strict definition; rare |

These are not failures — they are sample-size limits. The preregistered
decision rule says we report them as *underpowered* rather than as
"passed" or "failed". Several need at minimum a multi-week sample to
reach n_moved ≥ 30.

## Null result — liquidity_shock

- n = 27, n_lab = 20, n_moved = 20
- H_cond = 50.0%, 95% CI [27%, 73%]
- mean outcome = −0.005 (small negative, CI crosses zero)
- p = 1.0 (two-sided sign test)
- **Verdict: not supported**

A genuine null. Liquidity shocks on Polymarket do not show a detectable
directional bias in subsequent 15-minute mid drift. This is consistent
with the divided liquidity-event literature (Easley et al. 2012 vs Næs &
Skjeltorp 2006); the venue may simply not have enough information-
gradient events for the signal to bite.

The sample is also borderline underpowered for detecting a moderate
H_cond ~ 0.6 (n_min ≈ 49 from the power table in `STATISTICS.md`). We
will revisit at the final analysis.

## What we are NOT yet able to say

- **No tradable strategy claim.** Forward mid-return ≠ realised P&L.
  Even the OFI finding does not imply a profitable strategy after
  spread-crossing and slippage.
- **No generalisation.** The OFI result is specific to the top-150-
  liquid universe over the sample window. Liquidity tier and regime
  sensitivity will be reported in the final.
- **No conclusion on momentum or relative-value families.** They need
  more sample.

## Notes on what *did* surprise us

1. **The raw vs conditional hit-rate gap on OFI is enormous** (11% vs
   70%). A reader of only the raw number would conclude OFI is junk on
   this venue. This is the kind of finding that motivates the
   methodological care detailed in `STATISTICS.md` and is itself a
   contribution.

2. **trade-through is contrarian, not informed.** The equity-microstructure
   prediction is for continuation; ours suggests reversion. The FDR
   correction prevents us from declaring this conclusively, but the
   directional sign is robust to all bootstrap perturbations we tried.

3. **liquidity_shock has H_cond exactly 0.5.** Pure noise. Whatever it is
   detecting, the forward direction is uninformative.

4. **Momentum has not fired even once.** The scanner went live midway
   through the soak; sample is just too small. Worth confirming in the
   final whether 14 days reaches even n_moved ≥ 30.

## Direction of travel for the final analysis

When the full sample is in, the deliverables are:

1. Updated tables with the same script.
2. Sensitivity analysis at horizons 5, 15, 60 minutes.
3. Per-category (politics / sports / crypto) breakdown of OFI.
4. Token-block bootstrap CIs for OFI and trade-through.
5. Out-of-sample confirmation of the trade-through contrarian sign,
   pre-registered before looking at the OOS data.

We will not add post-hoc hypotheses to chase positive results. Anything
new gets a separate, future, preregistered study.
