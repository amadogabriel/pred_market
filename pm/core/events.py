"""Internal event envelope. Everything on the bus is one of these, and
everything on the bus gets appended to the on-disk event log verbatim.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# Topics
T_BOOK = "book"                  # full snapshot for one asset
T_PRICE_CHANGE = "price_change"  # level deltas
T_TRADE = "last_trade_price"
T_TICK = "tick_size_change"
T_MARKET_EVT = "market_event"    # new_market / market_resolved / best_bid_ask
T_SIGNAL = "signal"
T_EXECUTION = "execution"        # execution intents, risk decisions, fills
T_SYSTEM = "system"              # connects, disconnects, recon results, errors

ALL_TOPICS = (T_BOOK, T_PRICE_CHANGE, T_TRADE, T_TICK, T_MARKET_EVT, T_SIGNAL, T_EXECUTION, T_SYSTEM)


@dataclass
class Event:
    topic: str
    payload: dict[str, Any]
    ts: float = field(default_factory=time.time)

    def to_record(self) -> dict[str, Any]:
        return {"ts": self.ts, "topic": self.topic, **{"payload": self.payload}}
