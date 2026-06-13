"""Bayesian update of market probability given a news event.

The update has two regime knobs:

- `prior`: the current market mid (best information before the article).
- `likelihood_ratio`: how much more likely is this article under YES than NO?

We do *not* learn likelihood ratios; we use a fixed effect-size table per
polarity. The report explicitly endorses this simplicity:

    "A simple keyword-to-contract mapping that fires in 2 seconds beats a
     sophisticated transformer that fires in 30 seconds."

Posterior, by Bayes for a binary event:

    p_post = (LR * p_prior) / (LR * p_prior + (1 - p_prior))

We return both the posterior and the edge (posterior - prior). Callers
decide whether the edge exceeds the trade threshold.
"""
from __future__ import annotations

from dataclasses import dataclass


# Likelihood ratios for our crude polarity directions. These are
# deliberately small — a single headline rarely warrants a >5pp shift.
# Negative direction inverts (LR < 1).
LR_STRONG_POSITIVE = 2.0   # explicit win/approve/raises
LR_WEAK_POSITIVE = 1.4
LR_STRONG_NEGATIVE = 0.5
LR_WEAK_NEGATIVE = 0.71    # 1/1.4

# Edge below this is just noise.
MIN_EDGE = 0.01


@dataclass(frozen=True)
class NewsUpdate:
    prior: float
    posterior: float
    edge: float
    direction: int   # +1, -1, 0
    likelihood_ratio: float
    fired: bool      # |edge| >= MIN_EDGE


def update(prior: float, direction: int, *,
           strong: bool = False) -> NewsUpdate:
    """Bayes-update a binary prior given direction + strength."""
    if not 0.0 < prior < 1.0 or direction == 0:
        return NewsUpdate(prior, prior, 0.0, direction, 1.0, False)
    if direction > 0:
        lr = LR_STRONG_POSITIVE if strong else LR_WEAK_POSITIVE
    else:
        lr = LR_STRONG_NEGATIVE if strong else LR_WEAK_NEGATIVE
    p_post = (lr * prior) / (lr * prior + (1.0 - prior))
    edge = p_post - prior
    return NewsUpdate(prior=round(prior, 4), posterior=round(p_post, 4),
                      edge=round(edge, 4), direction=direction,
                      likelihood_ratio=lr, fired=abs(edge) >= MIN_EDGE)
