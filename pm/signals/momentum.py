"""S4 momentum / mean-reversion scanners — research signals (never executable).

Two detectors over the same rolling mid history:

1. directional_momentum — sustained signed drift over a window, normalised by
   the window's own mid-return volatility. A high |z| means the recent move is
   large relative to this token's own short-run noise. Whether momentum
   persists or reverts in prediction markets is an empirical question; the
   labeler will answer it.

2. boundary_overshoot — when a YES mid sits beyond `boundary_low/high` for at
   least `min_samples` and the *most recent* sample reverses meaningfully back
   toward the interior. This is the classic "extreme price + initial bounce"
   setup. The signal is logged with the direction of the bounce so the labeler
   measures whether the bounce continues or fades.

Both share rolling per-token state with the microstructure tracker's idea but
are intentionally a separate module so threshold tuning, debouncing, and
labeler analysis stay independent.

All emissions are debounced per token+kind, carry exec_sets=0, and exist to
build the meta-label dataset — they are hypotheses to validate against the
labeler's forward returns, not tradable edges.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from statistics import fmean, stdev

from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine
from pm.signals.common import Debouncer, ResearchSignal, mid_price

log = logging.getLogger(__name__)

STRATEGY = "momentum"
RET_STD_FLOOR = 1e-4  # quiet tokens: keep a near-zero vol from manufacturing huge z


class MomentumTracker:
    def __init__(self, books: BookStore, fees: FeeEngine, *,
                 window_s: float = 300.0, min_samples: int = 20,
                 z_threshold: float = 2.5, min_abs_drift: float = 0.01,
                 boundary_low: float = 0.05, boundary_high: float = 0.95,
                 boundary_bounce: float = 0.01,
                 debounce_s: float = 180.0, stale_after: float = 30.0,
                 venue: str = "polymarket", clock=time.time):
        self.books = books
        self.fees = fees
        self.clock = clock  # injectable for event-log replay (event-time)
        self.window_s = window_s
        self.min_samples = min_samples
        self.z_threshold = z_threshold
        self.min_abs_drift = min_abs_drift
        self.boundary_low = boundary_low
        self.boundary_high = boundary_high
        self.boundary_bounce = boundary_bounce
        self.stale_after = stale_after
        self.venue = venue
        self.debounce = Debouncer(debounce_s)
        # token -> deque[(ts, mid)]
        self._hist: dict[str, deque] = {}

    def on_book_update(self, token_id: str, meta: dict) -> list[ResearchSignal]:
        mid = mid_price(self.books.peek(token_id), self.stale_after)
        if mid is None or not (0.0 < mid < 1.0):
            return []
        now = self.clock()
        hist = self._hist.setdefault(token_id, deque())
        hist.append((now, mid))
        while hist and now - hist[0][0] > self.window_s:
            hist.popleft()
        if len(hist) < self.min_samples:
            return []

        out: list[ResearchSignal] = []
        out.extend(self._check_momentum(token_id, meta, hist, mid, now))
        out.extend(self._check_boundary(token_id, meta, hist, mid, now))
        return out

    def _check_momentum(self, token_id: str, meta: dict, hist, mid: float,
                        now: float) -> list[ResearchSignal]:
        first_mid = hist[0][1]
        drift = mid - first_mid
        if abs(drift) < self.min_abs_drift:
            return []
        rets = [hist[i][1] - hist[i - 1][1] for i in range(1, len(hist))]
        if len(rets) < 2:
            return []
        vol = max(stdev(rets), RET_STD_FLOOR)
        # z-score the total drift against per-step vol scaled by sqrt(N) (random walk null)
        z = drift / (vol * (len(rets) ** 0.5))
        if abs(z) < self.z_threshold:
            return []
        if not self.debounce.ready(f"mom:{token_id}", now):
            return []
        side = "BUY" if drift > 0 else "SELL"  # direction of recent persistent move
        return [ResearchSignal(
            strategy=STRATEGY, kind="directional_momentum", group_id=meta["market_id"],
            legs=[{"token_id": token_id, "market_id": meta["market_id"],
                   "side": side, "price": round(mid, 4), "size": 0.0}],
            gross_edge=abs(drift), fees=0.0, net_edge=0.0,
            features={"drift": round(drift, 4), "z": round(z, 2),
                      "step_vol": round(vol, 5), "first_mid": round(first_mid, 4),
                      "mid": round(mid, 4), "n_samples": len(hist),
                      "direction": "up" if drift > 0 else "down",
                      "category": meta.get("category")},
        )]

    def _check_boundary(self, token_id: str, meta: dict, hist, mid: float,
                        now: float) -> list[ResearchSignal]:
        # need a stable boundary regime in the window followed by a bounce
        history_mids = [m for _, m in hist]
        if len(history_mids) < self.min_samples:
            return []
        prior = history_mids[:-1]  # everything before "now"
        # which boundary was the token sitting at?
        at_high = all(m >= self.boundary_high for m in prior)
        at_low = all(m <= self.boundary_low for m in prior)
        if not (at_high or at_low):
            return []
        # measure the bounce: latest mid vs the prior mean (interior direction)
        prior_mean = fmean(prior)
        bounce = mid - prior_mean
        if at_high:
            if bounce > -self.boundary_bounce:
                return []
            direction = "down"
            side = "SELL"  # YES is bouncing off the top -> SELL YES
        else:
            if bounce < self.boundary_bounce:
                return []
            direction = "up"
            side = "BUY"   # YES is bouncing off the bottom -> BUY YES
        if not self.debounce.ready(f"bnd:{token_id}", now):
            return []
        return [ResearchSignal(
            strategy=STRATEGY, kind="boundary_overshoot", group_id=meta["market_id"],
            legs=[{"token_id": token_id, "market_id": meta["market_id"],
                   "side": side, "price": round(mid, 4), "size": 0.0}],
            gross_edge=abs(bounce), fees=0.0, net_edge=0.0,
            features={"prior_mean": round(prior_mean, 4), "mid": round(mid, 4),
                      "bounce": round(bounce, 4),
                      "boundary": "high" if at_high else "low",
                      "direction": direction, "n_samples": len(hist),
                      "category": meta.get("category")},
        )]
