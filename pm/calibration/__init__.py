"""Calibration model: market-vs-model probability divergence.

The most durable edge in the strategy report. Build a probability model from
historical base rates per contract category and bet when the market price
diverges materially from the model.

Three sources of model probability, blended:

1. **Internal base rates.** A YAML file `config/base_rates.yaml` lists
   (category, question_pattern) → empirical base rate from historical
   resolutions. Curated, not auto-learned. Hand-edited as new categories
   accumulate enough resolved samples in `signal_log` to be informative.

2. **Metaculus aggregate.** When a market question resolves to a Metaculus
   question, we fetch their crowd forecast. Their community is well
   calibrated on long-horizon political/scientific questions
   (Brier ~0.15 on closed questions per public benchmarks).

3. **External indicators.** Where applicable: CME FedWatch for Fed-action
   markets, polling aggregators for elections, prediction-market consensus
   for cross-checks. Each indicator is a separate plugin.

The combined model probability is a weighted geometric mean of the available
signals, falling back to the internal base rate if no external source
matches.

The scanner (`pm/signals/calibration_div.py`) trades only when:
- |market_mid - model_p| ≥ edge_threshold (default 0.10)
- Time-to-expiry > min_time_to_expiry_s (don't trade dying markets)
- Model has at least one informative source (not just naive 0.5)
"""
