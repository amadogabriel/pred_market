"""Tests for the S4 momentum / boundary-overshoot research scanner."""
from __future__ import annotations

from pathlib import Path

import pytest

from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine
from pm.signals.momentum import MomentumTracker

FEES_YAML = Path(__file__).resolve().parent.parent / "config" / "fees.yaml"

META = {"market_id": "M1", "category": "politics", "neg_risk_id": None,
        "token_yes": "T1", "token_no": "T2"}


@pytest.fixture
def fees() -> FeeEngine:
    return FeeEngine.from_yaml(FEES_YAML)


def _set_book(store: BookStore, token: str, mid: float, half_spread: float = 0.005) -> None:
    store.handle_ws_message({
        "event_type": "book", "asset_id": token,
        "bids": [{"price": round(mid - half_spread, 4), "size": 100}],
        "asks": [{"price": round(mid + half_spread, 4), "size": 100}]})


class FakeClock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ---------- directional_momentum ----------

def test_directional_momentum_fires_on_persistent_drift(fees):
    store = BookStore()
    clock = FakeClock()
    mom = MomentumTracker(store, fees, window_s=600, min_samples=10,
                          z_threshold=2.0, min_abs_drift=0.02,
                          debounce_s=60, clock=clock)
    # Drift from 0.40 up to 0.50 in 10 steps — sustained one-way move
    sigs = []
    for i in range(11):
        _set_book(store, "T1", 0.40 + i * 0.01)
        sigs.extend(mom.on_book_update("T1", META))
        clock.advance(5.0)
    momentum = [s for s in sigs if s.kind == "directional_momentum"]
    assert len(momentum) >= 1
    sig = momentum[0]
    assert sig.exec_sets == 0.0
    assert sig.legs[0]["side"] == "BUY"  # upward drift -> BUY YES
    assert sig.features["direction"] == "up"
    assert sig.features["drift"] > 0
    assert sig.features["z"] >= 2.0


def test_directional_momentum_quiet_on_choppy_walk(fees):
    store = BookStore()
    clock = FakeClock()
    mom = MomentumTracker(store, fees, window_s=600, min_samples=10,
                          z_threshold=2.5, min_abs_drift=0.02,
                          debounce_s=60, clock=clock)
    # Oscillate around 0.50 — no net drift
    pattern = [0.49, 0.51, 0.495, 0.505, 0.498, 0.502, 0.49, 0.51, 0.495, 0.505,
               0.498, 0.502]
    sigs = []
    for mid in pattern:
        _set_book(store, "T1", mid)
        sigs.extend(mom.on_book_update("T1", META))
        clock.advance(5.0)
    momentum = [s for s in sigs if s.kind == "directional_momentum"]
    assert momentum == []


def test_directional_momentum_quiet_below_min_drift(fees):
    store = BookStore()
    clock = FakeClock()
    mom = MomentumTracker(store, fees, window_s=600, min_samples=10,
                          z_threshold=1.0, min_abs_drift=0.05,  # demand big drift
                          debounce_s=60, clock=clock)
    # Tiny drift 0.50 -> 0.51 — below min_abs_drift
    for i in range(15):
        _set_book(store, "T1", 0.50 + i * 0.0005)
        sigs = mom.on_book_update("T1", META)
        clock.advance(5.0)
    momentum = [s for s in sigs if s.kind == "directional_momentum"]
    assert momentum == []


# ---------- boundary_overshoot ----------

def test_boundary_overshoot_fires_on_high_bounce(fees):
    store = BookStore()
    clock = FakeClock()
    mom = MomentumTracker(store, fees, window_s=600, min_samples=10,
                          z_threshold=99.0,    # disable momentum branch
                          min_abs_drift=99.0,
                          boundary_high=0.95, boundary_bounce=0.02,
                          debounce_s=60, clock=clock)
    sigs = []
    # 10 samples at 0.97 (above boundary_high) then a bounce to 0.92
    for _ in range(10):
        _set_book(store, "T1", 0.97)
        sigs.extend(mom.on_book_update("T1", META))
        clock.advance(5.0)
    _set_book(store, "T1", 0.92)  # bounce down by 0.05
    sigs.extend(mom.on_book_update("T1", META))
    overshoots = [s for s in sigs if s.kind == "boundary_overshoot"]
    assert len(overshoots) == 1
    sig = overshoots[0]
    assert sig.exec_sets == 0.0
    assert sig.features["boundary"] == "high"
    assert sig.features["direction"] == "down"
    assert sig.legs[0]["side"] == "SELL"  # bouncing off the top -> SELL YES


def test_boundary_overshoot_fires_on_low_bounce(fees):
    store = BookStore()
    clock = FakeClock()
    mom = MomentumTracker(store, fees, window_s=600, min_samples=10,
                          z_threshold=99.0, min_abs_drift=99.0,
                          boundary_low=0.05, boundary_bounce=0.02,
                          debounce_s=60, clock=clock)
    sigs = []
    for _ in range(10):
        _set_book(store, "T1", 0.03)
        sigs.extend(mom.on_book_update("T1", META))
        clock.advance(5.0)
    _set_book(store, "T1", 0.08)  # bounce up by 0.05
    sigs.extend(mom.on_book_update("T1", META))
    overshoots = [s for s in sigs if s.kind == "boundary_overshoot"]
    assert len(overshoots) == 1
    sig = overshoots[0]
    assert sig.features["boundary"] == "low"
    assert sig.features["direction"] == "up"
    assert sig.legs[0]["side"] == "BUY"


def test_boundary_overshoot_quiet_without_bounce(fees):
    store = BookStore()
    clock = FakeClock()
    mom = MomentumTracker(store, fees, window_s=600, min_samples=10,
                          z_threshold=99.0, min_abs_drift=99.0,
                          boundary_high=0.95, boundary_bounce=0.02,
                          debounce_s=60, clock=clock)
    sigs = []
    # Stays pinned above the boundary — no bounce
    for _ in range(12):
        _set_book(store, "T1", 0.97)
        sigs.extend(mom.on_book_update("T1", META))
        clock.advance(5.0)
    overshoots = [s for s in sigs if s.kind == "boundary_overshoot"]
    assert overshoots == []


def test_boundary_overshoot_quiet_when_history_not_at_boundary(fees):
    store = BookStore()
    clock = FakeClock()
    mom = MomentumTracker(store, fees, window_s=600, min_samples=10,
                          z_threshold=99.0, min_abs_drift=99.0,
                          boundary_high=0.95, boundary_bounce=0.02,
                          debounce_s=60, clock=clock)
    sigs = []
    # Mid-book the whole time, never touches the boundary
    for _ in range(12):
        _set_book(store, "T1", 0.60)
        sigs.extend(mom.on_book_update("T1", META))
        clock.advance(5.0)
    _set_book(store, "T1", 0.55)
    sigs.extend(mom.on_book_update("T1", META))
    overshoots = [s for s in sigs if s.kind == "boundary_overshoot"]
    assert overshoots == []


def test_no_signals_before_min_samples(fees):
    store = BookStore()
    clock = FakeClock()
    mom = MomentumTracker(store, fees, window_s=600, min_samples=10,
                          z_threshold=0.5, min_abs_drift=0.001,
                          debounce_s=60, clock=clock)
    # Big drift but not enough samples yet
    sigs = []
    for i in range(5):
        _set_book(store, "T1", 0.30 + i * 0.05)
        sigs.extend(mom.on_book_update("T1", META))
        clock.advance(5.0)
    assert sigs == []
