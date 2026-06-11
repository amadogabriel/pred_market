"""S1 structural arbitrage — Phase 0/1: SIGNAL-ONLY. No orders are placed.

Two scanners:

1. Partition arb (NegRisk groups): mutually exclusive, exhaustive outcomes.
   - BUY-ALL:  sum of best YES asks + fees < $1.00 - buffer
                -> buy every leg, guaranteed $1 payout per set.
   - SELL-ALL: sum of best YES bids - fees > $1.00 + buffer
                -> sell every leg (requires inventory or NegRisk conversion;
                   logged for completeness, flagged needs_inventory).

2. Complement check (single market): YES ask + NO ask + fees < $1.00 - buffer.

Executable size = min depth across legs at the quoted prices ("sets").
Edge accounting is per SET (one share of every leg), in dollars.

EXHAUSTIVENESS WARNING: buy-all is only riskless if the group is truly
exhaustive (some NegRisk groups carry an implicit "other/none of the above"
leg or admit late-added candidates). The scanner trusts metadata_sync's
grouping; Gate G1 review must confirm exhaustiveness per group before any
of these are ever executed.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine

log = logging.getLogger(__name__)


@dataclass
class ArbSignal:
    kind: str                 # partition_buy_all | partition_sell_all | complement
    group_id: str
    legs: list[dict]          # {token_id, market_id, side, price, size}
    gross_edge: float         # per set, before fees
    fees: float               # per set
    net_edge: float           # per set, after fees
    exec_sets: float
    features: dict

    @property
    def total_net(self) -> float:
        return self.net_edge * self.exec_sets


class StructArbScanner:
    def __init__(self, books: BookStore, fees: FeeEngine, *,
                 buffer: float = 0.01, min_sets: float = 10.0,
                 stale_after: float = 30.0, venue: str = "polymarket"):
        self.books = books
        self.fees = fees
        self.buffer = buffer
        self.min_sets = min_sets
        self.stale_after = stale_after
        self.venue = venue
        self._last_emit: dict[str, float] = {}   # debounce per group+kind

    # ---------- partition ----------
    def scan_partition(self, group_id: str,
                       legs_meta: list[dict]) -> list[ArbSignal]:
        """legs_meta: [{token_yes, market_id, category}, ...] — one per outcome."""
        out: list[ArbSignal] = []
        asks, bids = [], []
        for lm in legs_meta:
            b = self.books.peek(lm["token_yes"])
            if b is None or b.is_stale(self.stale_after):
                return out  # incomplete or stale picture -> no signal, ever
            ba, bb = b.best_ask(), b.best_bid()
            if ba is None or bb is None:
                return out
            asks.append((lm, *ba))
            bids.append((lm, *bb))

        # BUY-ALL: pay sum of asks, receive $1 at resolution.
        sum_ask = sum(p for _, p, _ in asks)
        fee_buy = sum(self.fees.taker_fee(self.venue, lm.get("category"), p, 1.0)
                      for lm, p, _ in asks)
        gross = 1.0 - sum_ask
        net = gross - fee_buy
        if net > self.buffer:
            sets = min(s for _, _, s in asks)
            if sets >= self.min_sets:
                out.append(self._mk("partition_buy_all", group_id, asks, "BUY",
                                    gross, fee_buy, net, sets))

        # SELL-ALL: receive sum of bids, owe $1 at resolution (needs inventory/conversion).
        sum_bid = sum(p for _, p, _ in bids)
        fee_sell = sum(self.fees.taker_fee(self.venue, lm.get("category"), p, 1.0)
                       for lm, p, _ in bids)
        gross_s = sum_bid - 1.0
        net_s = gross_s - fee_sell
        if net_s > self.buffer:
            sets = min(s for _, _, s in bids)
            if sets >= self.min_sets:
                sig = self._mk("partition_sell_all", group_id, bids, "SELL",
                               gross_s, fee_sell, net_s, sets)
                sig.features["needs_inventory"] = True
                out.append(sig)
        return out

    # ---------- complement ----------
    def scan_complement(self, market_id: str, token_yes: str, token_no: str,
                        category: str | None) -> list[ArbSignal]:
        by, bn = self.books.peek(token_yes), self.books.peek(token_no)
        if not by or not bn or by.is_stale(self.stale_after) or bn.is_stale(self.stale_after):
            return []
        ay, an = by.best_ask(), bn.best_ask()
        if ay is None or an is None:
            return []
        (py, sy), (pn, sn) = ay, an
        gross = 1.0 - (py + pn)
        fee = (self.fees.taker_fee(self.venue, category, py, 1.0)
               + self.fees.taker_fee(self.venue, category, pn, 1.0))
        net = gross - fee
        if net <= self.buffer:
            return []
        sets = min(sy, sn)
        if sets < self.min_sets:
            return []
        legs = [
            {"token_id": token_yes, "market_id": market_id, "side": "BUY", "price": py, "size": sy},
            {"token_id": token_no, "market_id": market_id, "side": "BUY", "price": pn, "size": sn},
        ]
        return [ArbSignal("complement", market_id, legs, gross, fee, net, sets,
                          {"category": category})]

    # ---------- helpers ----------
    def _mk(self, kind: str, group_id: str, quotes: list, side: str,
            gross: float, fee: float, net: float, sets: float) -> ArbSignal:
        legs = [{"token_id": lm["token_yes"], "market_id": lm["market_id"],
                 "side": side, "price": p, "size": s} for lm, p, s in quotes]
        return ArbSignal(kind, group_id, legs, gross, fee, net, sets,
                         {"n_legs": len(legs)})

    def should_emit(self, sig: ArbSignal, debounce_s: float = 10.0) -> bool:
        """Debounce: the same persisting opportunity shouldn't spam the log."""
        key = f"{sig.kind}:{sig.group_id}"
        now = time.time()
        if now - self._last_emit.get(key, 0.0) < debounce_s:
            return False
        self._last_emit[key] = now
        return True
