"""Blend internal base rates + external probabilities into a model probability.

The blend is a weighted *logit* average — natural for probabilities and
symmetric around 0.5. Weights:

- internal base rate weight = `min(1.0, n_samples / 30)` (Saturates at n=30)
- external source weight = whatever the fetcher returned (typically 0.1–1.0)
- floor weight if both are zero/missing → returns None

If no source contributed, we return None and the divergence scanner skips
this market. We do not blend an uninformed 0.5 in by default — that would
make the model output indistinguishable from "we have no idea."
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from pm.calibration.base_rates import BaseRate
from pm.calibration.sources import ExternalProb


@dataclass(frozen=True)
class ModelProb:
    p: float
    sources: list[str]
    weight_total: float


def _logit(p: float) -> float:
    p = min(max(p, 1e-6), 1 - 1e-6)
    return math.log(p / (1 - p))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def blend(internal: BaseRate | None,
          externals: list[ExternalProb] | None = None) -> ModelProb | None:
    parts: list[tuple[float, float, str]] = []  # (weight, logit_p, source)
    if internal is not None:
        w = min(1.0, internal.n_samples / 30.0)
        if w > 0:
            parts.append((w, _logit(internal.p),
                          f"internal:{internal.name}"))
        else:
            # n_samples = 0 marks a placeholder rate — informative as a prior
            # nudge only, with a small fixed weight
            parts.append((0.1, _logit(internal.p),
                          f"internal_prior:{internal.name}"))
    if externals:
        for e in externals:
            if e is None or e.weight <= 0:
                continue
            parts.append((e.weight, _logit(e.p), e.source))
    if not parts:
        return None
    total_w = sum(w for w, _, _ in parts)
    if total_w <= 0:
        return None
    logit_avg = sum(w * lp for w, lp, _ in parts) / total_w
    return ModelProb(p=round(_sigmoid(logit_avg), 4),
                     sources=[s for _, _, s in parts],
                     weight_total=round(total_w, 3))
