"""S2 microstructure scanners — research signals (never executable).

Three detectors over data the engine already ingests:

1. ofi_pressure — sustained order-flow imbalance at the touch. Rolling window
   of (bid_depth − ask_depth)/(bid_depth + ask_depth) samples; when the mean
   imbalance over enough samples crosses a threshold while the spread is tight,
   one side is absorbing flow. Direction = the heavy side. Dimensionless score
   in gross_edge (net_edge stays 0 — this is not a priced edge).

2. liquidity_shock — spread blowout + touch-depth evaporation vs the window's
   own baseline. Usually precedes news / repricing; useful as a do-not-execute
   filter and as a research label.

3. trade_through — a trade printing far from the prevailing mid (beyond fees).
   Prints outside the touch mean someone paid up: informed-flow proxy. Side of
   the print's deviation gives the direction. net_edge = |dev| − per-share fee
   via fee_engine.min_edge (invariant: no hardcoded fee numbers).

All emissions are debounced per token+kind, carry exec_sets=0, and exist to
build the meta-label dataset — they are hypotheses to validate against the
labeler's forward returns, not tradable edges.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from statistics import median

from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine
from pm.signals.common import Debouncer, ResearchSignal, mid_price, touch_state

log = logging.getLogger(__name__)

STRATEGY = "microstructure"


class MicrostructureTracker:
    def __init__(self, books: BookStore, fees: FeeEngine, *,
                 window_s: float = 300.0, min_samples: int = 20,
                 ofi_threshold: float = 0.6, max_spread: float = 0.03,
                 liq_spread_mult: float = 3.0, liq_depth_drop: float = 0.5,
                 trade_abs_floor: float = 0.01, debounce_s: float = 120.0,
                 stale_after: float = 30.0, venue: str = "polymarket",
                 clock=time.time):
        self.books = books
        self.fees = fees
        self.clock = clock  # injectable for event-log replay (event-time)
        self.window_s = window_s
        self.min_samples = min_samples
        self.ofi_threshold = ofi_threshold
        self.max_spread = max_spread
        self.liq_spread_mult = liq_spread_mult
        self.liq_depth_drop = liq_depth_drop
        self.trade_abs_floor = trade_abs_floor
        self.stale_after = stale_after
        self.venue = venue
        self.debounce = Debouncer(debounce_s)
        # token -> deque[(ts, mid, spread, bid_depth, ask_depth)]
        self._hist: dict[str, deque] = {}

    # ---------- book-driven ----------
    def on_book_update(self, token_id: str, meta: dict) -> list[ResearchSignal]:
        state = touch_state(self.books.peek(token_id), self.stale_after)
        if state is None:
            return []
        mid, spread, bd, ad = state
        now = self.clock()
        hist = self._hist.setdefault(token_id, deque())
        hist.append((now, mid, spread, bd, ad))
        while hist and now - hist[0][0] > self.window_s:
            hist.popleft()
        if len(hist) < self.min_samples:
            return []

        out: list[ResearchSignal] = []
        out.extend(self._check_ofi(token_id, meta, hist, mid, spread, now))
        out.extend(self._check_liquidity(token_id, meta, hist, mid, spread, bd, ad, now))
        return out

    def _check_ofi(self, token_id: str, meta: dict, hist, mid: float,
                   spread: float, now: float) -> list[ResearchSignal]:
        if spread > self.max_spread:
            return []
        imbs = [(b - a) / (b + a) for _, _, _, b, a in hist if (b + a) > 0]
        if len(imbs) < self.min_samples:
            return []
        avg_imb = sum(imbs) / len(imbs)
        if abs(avg_imb) < self.ofi_threshold:
            return []
        if not self.debounce.ready(f"ofi:{token_id}", now):
            return []
        side = "BUY" if avg_imb > 0 else "SELL"  # heavy bids -> upward pressure
        return [ResearchSignal(
            strategy=STRATEGY, kind="ofi_pressure", group_id=meta["market_id"],
            legs=[{"token_id": token_id, "market_id": meta["market_id"],
                   "side": side, "price": mid, "size": 0.0}],
            gross_edge=avg_imb, fees=0.0, net_edge=0.0,
            features={"imbalance": round(avg_imb, 4), "spread": round(spread, 4),
                      "mid": round(mid, 4), "n_samples": len(imbs),
                      "category": meta.get("category")},
        )]

    def _check_liquidity(self, token_id: str, meta: dict, hist, mid: float,
                         spread: float, bd: float, ad: float,
                         now: float) -> list[ResearchSignal]:
        spreads = [s for _, _, s, _, _ in hist]
        depths = [b + a for _, _, _, b, a in hist]
        base_spread = median(spreads)
        base_depth = median(depths)
        if base_spread <= 0 or base_depth <= 0:
            return []
        spread_ratio = spread / base_spread
        depth_ratio = (bd + ad) / base_depth
        if spread_ratio < self.liq_spread_mult or depth_ratio > (1.0 - self.liq_depth_drop):
            return []
        if not self.debounce.ready(f"liq:{token_id}", now):
            return []
        return [ResearchSignal(
            strategy=STRATEGY, kind="liquidity_shock", group_id=meta["market_id"],
            legs=[{"token_id": token_id, "market_id": meta["market_id"],
                   "side": "NA", "price": mid, "size": 0.0}],
            gross_edge=spread_ratio, fees=0.0, net_edge=0.0,
            features={"spread": round(spread, 4), "base_spread": round(base_spread, 4),
                      "spread_ratio": round(spread_ratio, 2),
                      "depth_ratio": round(depth_ratio, 3), "mid": round(mid, 4),
                      "category": meta.get("category")},
        )]

    # ---------- trade-driven ----------
    def on_trade(self, payload: dict, meta: dict) -> list[ResearchSignal]:
        token_id = payload.get("asset_id")
        if not token_id:
            return []
        try:
            price = float(payload["price"])
        except (KeyError, TypeError, ValueError):
            return []
        mid = mid_price(self.books.peek(token_id), self.stale_after)
        if mid is None or not (0.0 < mid < 1.0):
            return []
        dev = price - mid
        per_share_fee = self.fees.min_edge(self.venue, meta.get("category"), mid)
        threshold = max(per_share_fee, self.trade_abs_floor)
        if abs(dev) <= threshold:
            return []
        now = self.clock()
        if not self.debounce.ready(f"trade:{token_id}", now):
            return []
        size = 0.0
        try:
            size = float(payload.get("size") or 0.0)
        except (TypeError, ValueError):
            pass
        side = "BUY" if dev > 0 else "SELL"  # paid above mid -> aggressive buyer
        return [ResearchSignal(
            strategy=STRATEGY, kind="trade_through", group_id=meta["market_id"],
            legs=[{"token_id": token_id, "market_id": meta["market_id"],
                   "side": side, "price": price, "size": size}],
            gross_edge=abs(dev), fees=per_share_fee, net_edge=abs(dev) - per_share_fee,
            features={"trade_price": round(price, 4), "mid": round(mid, 4),
                      "deviation": round(dev, 4), "trade_size": size,
                      "category": meta.get("category")},
        )]
