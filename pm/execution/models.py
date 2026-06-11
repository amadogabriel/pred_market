"""Execution data models.

These are exchange-neutral objects used by the execution task, risk manager,
and brokers. They are safe to construct in Phase 0 because they do not submit
anything by themselves.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class OrderIntent:
    signal_id: int
    strategy: str
    kind: str
    group_id: str | None
    venue: str
    market_id: str | None
    token_id: str
    side: str
    price: float
    size: float

    @property
    def notional(self) -> float:
        return abs(self.price * self.size)

    @property
    def client_order_id(self) -> str:
        raw = f"{self.signal_id}:{self.venue}:{self.token_id}:{self.side}:{self.price}:{self.size}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

    def to_record(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "strategy": self.strategy,
            "kind": self.kind,
            "group_id": self.group_id,
            "venue": self.venue,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "side": self.side.upper(),
            "price": self.price,
            "size": self.size,
            "notional": self.notional,
            "client_order_id": self.client_order_id,
        }


@dataclass(frozen=True)
class BrokerReceipt:
    accepted: bool
    status: str
    broker_order_id: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    code: str
    detail: str


def intents_from_signal(signal: dict[str, Any], *, venue: str = "polymarket",
                        max_sets: float | None = None) -> list[OrderIntent]:
    """Translate a persisted signal row into exchange-neutral order intents."""
    size = float(signal["exec_sets"])
    if max_sets is not None:
        size = min(size, max_sets)
    if size <= 0:
        return []

    intents: list[OrderIntent] = []
    for leg in signal.get("legs", []):
        intents.append(OrderIntent(
            signal_id=int(signal["signal_id"]),
            strategy=str(signal["strategy"]),
            kind=str(signal["kind"]),
            group_id=signal.get("group_id"),
            venue=venue,
            market_id=leg.get("market_id"),
            token_id=str(leg["token_id"]),
            side=str(leg["side"]).upper(),
            price=float(leg["price"]),
            size=size,
        ))
    return intents
