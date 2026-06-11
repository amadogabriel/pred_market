"""Fee engine: the single source of truth for "what does this execution cost?"

Every threshold in the system queries this. Schedules live in config/fees.yaml,
versioned by effective_date, so replays over historical data use the fees that
applied at the time.

Polymarket (2026 structure):
    taker_fee = shares * category_rate * p * (1 - p), rounded; makers free.
Kalshi:
    per-side fee = ceil_to_cent(shares * rate * p * (1 - p)).

VERIFY against live published schedules before trading; re-verify monthly.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Schedule:
    venue: str
    effective_date: date
    model: str
    maker_rate: float
    default_rate: float
    category_rates: dict[str, float]
    min_fee: float = 0.0
    round_decimals: int = 4


class FeeEngine:
    def __init__(self, schedules: list[Schedule]):
        self._by_venue: dict[str, list[Schedule]] = {}
        for s in schedules:
            self._by_venue.setdefault(s.venue, []).append(s)
        for v in self._by_venue:
            self._by_venue[v].sort(key=lambda s: s.effective_date)

    # ---------- construction ----------
    @classmethod
    def from_yaml(cls, path: Path) -> "FeeEngine":
        raw = yaml.safe_load(path.read_text())
        scheds = []
        for s in raw["schedules"]:
            scheds.append(Schedule(
                venue=s["venue"],
                effective_date=date.fromisoformat(s["effective_date"]),
                model=s["model"],
                maker_rate=float(s.get("maker_rate", 0.0)),
                default_rate=float(s["default_rate"]),
                category_rates={k.lower(): float(v) for k, v in (s.get("category_rates") or {}).items()},
                min_fee=float(s.get("min_fee", 0.0)),
                round_decimals=int(s.get("round_decimals", 4)),
            ))
        return cls(scheds)

    def schedule(self, venue: str, on: date | None = None) -> Schedule:
        scheds = self._by_venue.get(venue)
        if not scheds:
            raise KeyError(f"no fee schedule for venue {venue!r}")
        on = on or date.today()
        applicable = [s for s in scheds if s.effective_date <= on]
        if not applicable:
            raise KeyError(f"no fee schedule for {venue!r} effective on {on}")
        return applicable[-1]

    # ---------- rates ----------
    def rate(self, venue: str, category: str | None, on: date | None = None) -> float:
        s = self.schedule(venue, on)
        if category and category.lower() in s.category_rates:
            return s.category_rates[category.lower()]
        return s.default_rate

    # ---------- fees ----------
    def taker_fee(self, venue: str, category: str | None, price: float,
                  shares: float, on: date | None = None) -> float:
        """Fee in dollars for a taker execution of `shares` at `price`."""
        if not (0.0 < price < 1.0):
            return 0.0
        s = self.schedule(venue, on)
        r = self.rate(venue, category, on)
        raw = shares * r * price * (1.0 - price)
        if s.model == "rate_p_one_minus_p":
            fee = round(raw, s.round_decimals)
            return fee if fee >= s.min_fee else 0.0
        if s.model == "rate_p_one_minus_p_per_side_ceil_cent":
            return math.ceil(raw * 100.0) / 100.0
        raise ValueError(f"unknown fee model {s.model!r}")

    def maker_fee(self, venue: str, category: str | None, price: float,
                  shares: float, on: date | None = None) -> float:
        if not (0.0 < price < 1.0):
            return 0.0
        s = self.schedule(venue, on)
        raw = shares * s.maker_rate * price * (1.0 - price)
        return round(raw, s.round_decimals)

    def fee(self, venue: str, category: str | None, price: float, shares: float,
            is_taker: bool = True, on: date | None = None) -> float:
        f = self.taker_fee if is_taker else self.maker_fee
        return f(venue, category, price, shares, on)

    # ---------- decision helpers ----------
    def min_edge(self, venue: str, category: str | None, price: float,
                 is_taker: bool = True, statistical_buffer: float = 0.0,
                 on: date | None = None) -> float:
        """Minimum model-vs-market edge (in probability points, per share)
        required to clear entry fee + buffer. Per-share fee = fee(1 share)."""
        per_share = self.fee(venue, category, price, 1.0, is_taker, on)
        return per_share + statistical_buffer
