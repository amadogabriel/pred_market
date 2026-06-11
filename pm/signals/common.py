"""Shared primitives for signal scanners.

ResearchSignal is the generic envelope for non-executable signals: anything a
scanner wants persisted to `signal_log` for the meta-label dataset but that no
execution path should ever act on. The contract is `exec_sets == 0.0`, which
makes `intents_from_signal()` return an empty plan, and the execution task
additionally filters by strategy allowlist — research strategies never reach
the risk pipeline at all.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from pm.core.books import Book


@dataclass
class ResearchSignal:
    strategy: str             # e.g. "microstructure" | "rel_value"
    kind: str                 # e.g. ofi_pressure | trade_through | partition_sum_drift
    group_id: str             # market_id or neg_risk group id
    legs: list[dict]          # [{token_id, market_id, side, price, size}, ...]
    gross_edge: float         # measured deviation, price units where meaningful
    fees: float               # applicable per-set taker fees (0 if not priced)
    net_edge: float           # gross - fees where meaningful, else 0
    features: dict = field(default_factory=dict)
    exec_sets: float = 0.0    # ALWAYS 0 — research signals are never executable


class Debouncer:
    """Per-key rate limiter so a persisting condition doesn't spam the log."""

    def __init__(self, interval_s: float):
        self.interval_s = interval_s
        self._last: dict[str, float] = {}

    def ready(self, key: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if now - self._last.get(key, 0.0) < self.interval_s:
            return False
        self._last[key] = now
        return True


def mid_price(book: Book | None, max_age: float) -> float | None:
    """Mid of best bid/ask, or None if the book is missing, stale, or one-sided."""
    if book is None or book.is_stale(max_age):
        return None
    bb, ba = book.best_bid(), book.best_ask()
    if bb is None or ba is None:
        return None
    return (bb[0] + ba[0]) / 2.0


def touch_state(book: Book | None, max_age: float) -> tuple[float, float, float, float] | None:
    """(mid, spread, bid_depth, ask_depth) at the touch, or None if unusable."""
    if book is None or book.is_stale(max_age):
        return None
    bb, ba = book.best_bid(), book.best_ask()
    if bb is None or ba is None:
        return None
    (bp, bs), (ap, as_) = bb, ba
    return (bp + ap) / 2.0, ap - bp, bs, as_
