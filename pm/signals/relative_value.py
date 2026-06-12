"""S3 relative-value scanners — research signals (never executable).

1. partition_sum_drift — each NegRisk group's sum of YES mids has a stable
   baseline (NOT necessarily 1.00: groups can be persistently over/under-round
   or non-exhaustive, so we z-score against the group's OWN rolling history).
   A sudden deviation means some legs repriced and others haven't. The signal
   carries attribution: `mover` legs (largest mid change since last sample) and
   the `laggard` leg (oldest book update) — the laggard is where the stale
   quote, i.e. the candidate mispricing, lives.

2. complement_drift — within one market, YES_mid + NO_mid should pin to 1.00
   tightly (same outcome, two books). Deviation beyond round-trip taker fees is
   a relative mispricing between the two books. net_edge = |dev| − fees, so a
   persistent positive net here is a true (if probably ephemeral) edge — still
   logged as research until validated by the labeler.

Emissions are debounced per group/market and carry exec_sets=0.
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

STRATEGY = "rel_value"
STD_FLOOR = 1e-4  # quiet groups: don't let a near-zero std manufacture huge z


class RelativeValueMonitor:
    def __init__(self, books: BookStore, fees: FeeEngine, *,
                 window_s: float = 1800.0, min_samples: int = 30,
                 z_threshold: float = 3.0, min_abs_dev: float = 0.02,
                 debounce_s: float = 120.0, stale_after: float = 30.0,
                 venue: str = "polymarket", clock=time.time):
        self.books = books
        self.fees = fees
        self.clock = clock  # injectable for event-log replay (event-time)
        self.window_s = window_s
        self.min_samples = min_samples
        self.z_threshold = z_threshold
        self.min_abs_dev = min_abs_dev
        self.stale_after = stale_after
        self.venue = venue
        self.debounce = Debouncer(debounce_s)
        self._sum_hist: dict[str, deque] = {}        # group_id -> deque[(ts, sum)]
        self._prev_mids: dict[str, dict[str, float]] = {}  # group_id -> token -> mid

    # ---------- partition ----------
    def on_group_update(self, group_id: str, legs_meta: list[dict]) -> list[ResearchSignal]:
        """Called when any leg of a NegRisk group sees a book update."""
        mids: dict[str, float] = {}
        for lm in legs_meta:
            m = mid_price(self.books.peek(lm["token_yes"]), self.stale_after)
            if m is None:
                return []  # incomplete picture -> no baseline update, no signal
            mids[lm["token_yes"]] = m

        total = sum(mids.values())
        now = self.clock()
        hist = self._sum_hist.setdefault(group_id, deque())
        prev_mids = self._prev_mids.get(group_id, {})
        self._prev_mids[group_id] = mids
        hist.append((now, total))
        while hist and now - hist[0][0] > self.window_s:
            hist.popleft()
        if len(hist) < self.min_samples:
            return []

        sums = [s for _, s in hist]
        mean = fmean(sums)
        std = max(stdev(sums), STD_FLOOR)
        dev = total - mean
        z = dev / std
        if abs(z) < self.z_threshold or abs(dev) < self.min_abs_dev:
            return []
        if not self.debounce.ready(f"psum:{group_id}", now):
            return []

        # attribution: who moved, who lagged
        moves = {t: mids[t] - prev_mids.get(t, mids[t]) for t in mids}
        mover = max(moves, key=lambda t: abs(moves[t])) if moves else None
        laggard_candidates = [lm["token_yes"] for lm in legs_meta if lm["token_yes"] != mover]
        if not laggard_candidates:
            laggard_candidates = [lm["token_yes"] for lm in legs_meta]
        laggard = min(
            laggard_candidates,
            key=lambda t: self.books.peek(t).last_update if self.books.peek(t) else 0.0,
            default=None)

        fee_per_set = sum(
            self.fees.taker_fee(self.venue, lm.get("category"), mids[lm["token_yes"]], 1.0)
            for lm in legs_meta)
        legs = [{"token_id": lm["token_yes"], "market_id": lm["market_id"],
                 "side": "NA", "price": round(mids[lm["token_yes"]], 4), "size": 0.0}
                for lm in legs_meta]
        return [ResearchSignal(
            strategy=STRATEGY, kind="partition_sum_drift", group_id=group_id,
            legs=legs, gross_edge=abs(dev), fees=fee_per_set,
            net_edge=abs(dev) - fee_per_set,
            features={"sum": round(total, 4), "baseline_mean": round(mean, 4),
                      "baseline_std": round(std, 5), "z": round(z, 2),
                      "direction": "rich" if dev > 0 else "cheap",
                      "mover_token": mover,
                      "mover_change": round(moves.get(mover, 0.0), 4) if mover else None,
                      "laggard_token": laggard, "n_legs": len(legs_meta),
                      "n_samples": len(sums)},
        )]

    # ---------- complement ----------
    def on_market_update(self, meta: dict) -> list[ResearchSignal]:
        """Called on a book update for either side of a single market."""
        token_yes, token_no = meta.get("token_yes"), meta.get("token_no")
        if not token_yes or not token_no:
            return []
        ymid = mid_price(self.books.peek(token_yes), self.stale_after)
        nmid = mid_price(self.books.peek(token_no), self.stale_after)
        if ymid is None or nmid is None:
            return []
        dev = ymid + nmid - 1.0
        category = meta.get("category")
        fees = (self.fees.taker_fee(self.venue, category, ymid, 1.0)
                + self.fees.taker_fee(self.venue, category, nmid, 1.0))
        if abs(dev) < max(fees, self.min_abs_dev):
            return []
        now = self.clock()
        if not self.debounce.ready(f"comp:{meta['market_id']}", now):
            return []
        return [ResearchSignal(
            strategy=STRATEGY, kind="complement_drift", group_id=meta["market_id"],
            legs=[
                {"token_id": token_yes, "market_id": meta["market_id"],
                 "side": "NA", "price": round(ymid, 4), "size": 0.0},
                {"token_id": token_no, "market_id": meta["market_id"],
                 "side": "NA", "price": round(nmid, 4), "size": 0.0},
            ],
            gross_edge=abs(dev), fees=fees, net_edge=abs(dev) - fees,
            features={"yes_mid": round(ymid, 4), "no_mid": round(nmid, 4),
                      "deviation": round(dev, 4),
                      "direction": "rich" if dev > 0 else "cheap",
                      "category": category},
        )]
