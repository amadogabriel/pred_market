# Literature review

This is the academic context that motivates the study. Citations are by
author-year + venue rather than full bibliographic entries; the canonical
list lives in the final paper.

## 1. Prediction-market efficiency

The classical question on prediction markets is whether their prices are
calibrated forecasts. Wolfers & Zitzewitz (JEP 2004, "Prediction Markets")
laid out the canonical case in favour of calibration on the Iowa Electronic
Markets and early Tradesports/Intrade data. Berg, Forsythe, Nelson & Rietz
(Handbook of Experimental Economic Results, 2008) demonstrated that
political prediction-market prices were closer to actual vote share than
contemporaneous polls.

The picture is more nuanced when one looks intraday. Page & Clemen
(Management Science 2013) studied intraday Intrade data and found
*non-trivial intraday inefficiencies* — prices wandered and returned —
even when daily-average prices were well calibrated. This is the gap that
intraday signal research occupies: even on a venue with calibrated daily
prices, transient mispricings within the day are an empirical question.

For Polymarket specifically, peer-reviewed empirical work is thin. There are
working papers (e.g. on 2024 US election forecasting) and industry reports,
but very little on intraday CLOB dynamics. **This is the gap our work
addresses.**

## 2. Market microstructure foundations

### Order-flow imbalance

The theoretical case for OFI as a price-impact signal comes from Cont,
Kukanov & Stoikov (J. Financial Econometrics 2014, "The price impact of
order book events"). They show, on equity LOB data, that signed volume at
the touch (additions minus cancellations on the bid versus the ask) has
near-linear predictive content for short-horizon mid drift. Subsequent
work (Lipton et al., Eisler et al., Kolm & Westray) refined the
multi-level extension and the timescale dependency.

Translating OFI to prediction markets has not been done in published work.
The mechanical reason it might fail: prediction-market mids are bounded
in [0, 1] and tick-discretised in $0.01 increments. The Cont et al.
linear-impact result depends on a continuous price approximation that
breaks at small tick counts. We expect OFI to be *weaker* on Polymarket
than on equity LOBs but not necessarily zero.

### Trade direction and informed flow

The Lee & Ready (J. Finance 1991) algorithm for classifying trades as
buyer- or seller-initiated, and the price-impact literature that followed
(Glosten-Milgrom, Kyle), give us the canonical reason aggressive trades
should *contain information*: informed traders demand liquidity, so trades
that print outside the mid are more likely to be informed. Hasbrouck
(Empirical Market Microstructure, 2007) reviews the empirical evidence on
equities.

The trade-through signal we test asks the question this literature would
predict: prints far from mid should predict continuation. Our preliminary
finding is the *opposite* (see RESULTS_PRELIMINARY.md), which is consistent
with a uninformed-noise-trader story on a venue with high retail
participation — but our sample is small and we make no strong claim yet.

### Liquidity events

Easley, López de Prado & O'Hara (Review of Financial Studies 2012, "Flow
toxicity and liquidity in a high-frequency world") give us the modern
treatment of liquidity withdrawal as a leading indicator of adverse
selection. The VPIN measure they introduce is volume-bucketed; ours is
time-bucketed (depth ratio against rolling baseline), which is closer to
the Næs & Skjeltorp (J. Financial Markets 2006) approach. The expected
forward outcome is *ambiguous* in this literature: a liquidity shock can
be either followed by adverse selection (the smart money was right; price
moves further in their direction) or by reversion (the shock was noise).
We do not preregister a directional prediction; we test only that
liquidity_shock signals are non-random in some direction.

## 3. Relative-value and arbitrage in prediction markets

### Sum-to-one arbitrage on NegRisk groups

The structural-arbitrage idea — that the YES prices in a mutually
exclusive and exhaustive partition should sum to one — is a direct
analogue of the put-call parity / book-arbitrage relationships studied
on options exchanges (Battalio & Schultz 2006, J. Finance). On
prediction markets specifically, the partition-arbitrage in Polymarket
NegRisk groups is mentioned in industry reports (e.g. by Domer 2024 on
the 2024 election markets) but has not been formally studied for
frequency, magnitude, or persistence.

The complement check (single-market YES + NO ≈ 1) is the *minimal*
case of this and is the easiest to verify empirically. We use it as a
baseline arbitrage measure: any genuine market efficiency should pin
this very tightly.

### Cross-market relative value

The partition-sum-drift signal we test is a *novel* construction (to
our knowledge): rather than asserting that the YES sum should equal 1.00,
we observe that it equals some venue-specific baseline that may itself
have a stable distribution, and we test for *deviations from the
baseline*. The closest published analogue is cross-asset basket
deviations in equities (Madhavan & Sobczyk 2016), but the
prediction-market application is new.

## 4. Momentum and mean-reversion

The momentum literature is vast in equities (Jegadeesh & Titman 1993,
Asness 1994, Moskowitz et al. 2012) and crypto (Tsang & Vechkayanavada
2022). The translation to prediction markets is not obvious: a momentum
in an equity reflects information arriving over time; a momentum in a
prediction market may reflect either information *or* approaching
resolution time. We test both `directional_momentum` and
`boundary_overshoot` and expect they may behave differently from each
other.

## 5. Tick-size and microstructure noise

Hasbrouck & Saar (J. Financial Markets 2013) and others document that
*tick-size discretisation* materially affects observed return-series
properties: zero returns over a short horizon are common, autocorrelation
structures are induced by quote-clustering. Polymarket's $0.01 tick is
*large* relative to typical mid moves over 15 minutes on illiquid
markets — we expect a substantial fraction of forward returns to be
*exactly zero* and explicitly handle this in the analysis (conditional
hit rate, sign test rather than t-test).

This is, as far as we know, the first study to flag tick-size
discretisation as a first-order methodological concern on prediction-
market CLOB data.

## 6. The literature gap our project occupies

Putting it together, the gap is:

1. Polymarket has been studied at the *forecast-quality* level (does the
   price predict the outcome?) but not at the *intraday-signal* level
   (do microstructure signals predict the next 15-minute mid drift?).
2. Microstructure literature has rich theory for OFI, trade-through, and
   liquidity events, all of which require empirical validation on a
   *binary tick-discretised CLOB* that the equity literature does not
   cover.
3. The structural-arbitrage opportunity in NegRisk groups is widely
   referenced informally but not formally measured for frequency,
   magnitude, or persistence.
4. Prediction-market empirical work has under-emphasised tick-size
   discretisation as a confounder of forward-return-based analyses.

Our work targets all four gaps with a single instrumented data-collection
framework and a preregistered analysis plan.

## 7. Methodological precedents

For our statistical approach we lean on:

- **Bootstrap CIs for hit rates and average outcomes:** Efron & Tibshirani
  (Introduction to the Bootstrap, 1993). Standard and robust to
  heavy-tailed distributions.
- **Sign test on zero/nonzero outcomes:** Wilcoxon (Biometrics 1945). The
  natural test when 85% of outcomes are exactly zero.
- **Multiple-testing correction:** Benjamini & Hochberg (J. Royal Stat
  Soc B 1995). FDR control is appropriate for a screening study with
  ten candidate signals; family-wise Bonferroni would be too conservative.
- **Power analysis with binary outcomes:** Lehr (Stat in Med 1992) for the
  back-of-envelope, exact binomial otherwise.
- **Preregistration:** the AsPredicted.org / OSF tradition. We do not
  use those services (the work is too venue-specific) but we adopt the
  spirit: write the hypotheses before looking at the data.
