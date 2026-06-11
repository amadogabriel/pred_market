"""In-memory L2 order books, one per asset (CLOB token).

Semantics match the Polymarket market channel:
  - `book` events are full snapshots (emitted on subscribe and after trades
    that affect the book) -> replace state.
  - `price_change` events carry level updates where `size` is the NEW TOTAL
    at that price; size 0 removes the level. side BUY -> bids, SELL -> asks.

Every asset tracks last_update so consumers can refuse to act on stale books
(stale-data kill in the risk gate later; the scanner checks it now).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class Book:
    asset_id: str
    bids: dict[float, float] = field(default_factory=dict)   # price -> size
    asks: dict[float, float] = field(default_factory=dict)
    last_update: float = 0.0
    seq_hash: str = ""

    # ---- mutation ----
    def apply_snapshot(self, bids: list[dict], asks: list[dict], h: str = "") -> None:
        self.bids = {float(l["price"]): float(l["size"]) for l in bids if float(l["size"]) > 0}
        self.asks = {float(l["price"]): float(l["size"]) for l in asks if float(l["size"]) > 0}
        self.seq_hash = h
        self.last_update = time.time()

    def apply_level(self, side: str, price: float, size: float) -> None:
        levels = self.bids if side.upper() == "BUY" else self.asks
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size
        self.last_update = time.time()

    # ---- queries ----
    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]

    def is_stale(self, max_age: float) -> bool:
        return (time.time() - self.last_update) > max_age

    def depth_at_or_better(self, side: str, limit_price: float) -> float:
        """Total size buyable at <= limit_price (side='ask') or sellable at >= limit_price (side='bid')."""
        if side == "ask":
            return sum(s for p, s in self.asks.items() if p <= limit_price)
        return sum(s for p, s in self.bids.items() if p >= limit_price)


class BookStore:
    def __init__(self) -> None:
        self._books: dict[str, Book] = {}

    def get(self, asset_id: str) -> Book:
        if asset_id not in self._books:
            self._books[asset_id] = Book(asset_id)
        return self._books[asset_id]

    def peek(self, asset_id: str) -> Book | None:
        return self._books.get(asset_id)

    def handle_ws_message(self, msg: dict) -> None:
        et = msg.get("event_type")
        if et == "book":
            self.get(msg["asset_id"]).apply_snapshot(
                msg.get("bids", []), msg.get("asks", []), msg.get("hash", ""))
        elif et == "price_change":
            top_level_asset = msg.get("asset_id")
            for pc in msg.get("price_changes", []) or msg.get("changes", []):
                aid = pc.get("asset_id") or top_level_asset
                if not aid:
                    continue
                self.get(aid).apply_level(
                    pc.get("side", "BUY"), float(pc["price"]), float(pc["size"]))

    def __len__(self) -> int:
        return len(self._books)
