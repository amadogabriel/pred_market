"""Tests for the S2 microstructure and S3 relative-value research scanners."""
from __future__ import annotations

from pathlib import Path

import pytest

from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine
from pm.signals.microstructure import MicrostructureTracker
from pm.signals.relative_value import RelativeValueMonitor

FEES_YAML = Path(__file__).resolve().parent.parent / "config" / "fees.yaml"

META = {"market_id": "M1", "category": "politics", "neg_risk_id": None,
        "token_yes": "T1", "token_no": "T2"}


@pytest.fixture
def fees() -> FeeEngine:
    return FeeEngine.from_yaml(FEES_YAML)


def _set_book(store: BookStore, token: str, bid_p: float, bid_s: float,
              ask_p: float, ask_s: float) -> None:
    store.handle_ws_message({
        "event_type": "book", "asset_id": token,
        "bids": [{"price": bid_p, "size": bid_s}],
        "asks": [{"price": ask_p, "size": ask_s}]})


# ---------- microstructure: OFI ----------

def test_ofi_pressure_fires_on_sustained_bid_heavy_book(fees):
    store = BookStore()
    micro = MicrostructureTracker(store, fees, window_s=3600, min_samples=5,
                                  ofi_threshold=0.6, debounce_s=60)
    _set_book(store, "T1", 0.50, 1000, 0.51, 10)  # imbalance ~0.98

    sigs = []
    for _ in range(6):
        sigs.extend(micro.on_book_update("T1", META))
    assert len(sigs) == 1  # fires once min_samples is reached, then debounced
    sig = sigs[0]
    assert sig.kind == "ofi_pressure"
    assert sig.exec_sets == 0.0
    assert sig.legs[0]["side"] == "BUY"  # heavy bids -> upward pressure
    assert sig.features["imbalance"] > 0.9
    # debounced: immediately after, no repeat
    assert micro.on_book_update("T1", META) == []


def test_ofi_quiet_on_balanced_book(fees):
    store = BookStore()
    micro = MicrostructureTracker(store, fees, window_s=3600, min_samples=5,
                                  ofi_threshold=0.6, debounce_s=60)
    _set_book(store, "T1", 0.50, 100, 0.51, 100)
    for _ in range(10):
        assert micro.on_book_update("T1", META) == []


# ---------- microstructure: liquidity shock ----------

def test_liquidity_shock_on_spread_blowout_and_depth_drop(fees):
    store = BookStore()
    micro = MicrostructureTracker(store, fees, window_s=3600, min_samples=5,
                                  ofi_threshold=2.0,  # disable OFI in this test
                                  liq_spread_mult=3.0, liq_depth_drop=0.5,
                                  debounce_s=60)
    _set_book(store, "T1", 0.50, 500, 0.51, 500)  # tight + deep baseline
    for _ in range(10):
        assert micro.on_book_update("T1", META) == []

    _set_book(store, "T1", 0.40, 5, 0.60, 5)  # spread x20, depth /100
    sigs = micro.on_book_update("T1", META)
    assert len(sigs) == 1
    assert sigs[0].kind == "liquidity_shock"
    assert sigs[0].features["spread_ratio"] >= 3.0
    assert sigs[0].features["depth_ratio"] <= 0.5


# ---------- microstructure: trade-through ----------

def test_trade_through_fires_beyond_fee_threshold(fees):
    store = BookStore()
    micro = MicrostructureTracker(store, fees, debounce_s=60)
    _set_book(store, "T1", 0.50, 100, 0.51, 100)  # mid 0.505

    sigs = micro.on_trade({"asset_id": "T1", "price": 0.58, "size": 40}, META)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.kind == "trade_through"
    assert sig.legs[0]["side"] == "BUY"  # printed above mid
    assert sig.net_edge > 0
    assert sig.features["trade_size"] == 40


def test_trade_near_mid_is_ignored(fees):
    store = BookStore()
    micro = MicrostructureTracker(store, fees, debounce_s=60)
    _set_book(store, "T1", 0.50, 100, 0.51, 100)
    assert micro.on_trade({"asset_id": "T1", "price": 0.507}, META) == []


def test_trade_without_book_is_ignored(fees):
    store = BookStore()
    micro = MicrostructureTracker(store, fees)
    assert micro.on_trade({"asset_id": "T9", "price": 0.60}, META) == []


# ---------- relative value: partition sum drift ----------

LEGS_META = [
    {"token_yes": "A", "market_id": "MA", "category": "politics"},
    {"token_yes": "B", "market_id": "MB", "category": "politics"},
]


def test_partition_sum_drift_fires_with_mover_and_laggard(fees):
    store = BookStore()
    rv = RelativeValueMonitor(store, fees, window_s=3600, min_samples=5,
                              z_threshold=3.0, min_abs_dev=0.02, debounce_s=60)
    _set_book(store, "A", 0.29, 100, 0.31, 100)  # mid 0.30
    _set_book(store, "B", 0.49, 100, 0.51, 100)  # mid 0.50

    for _ in range(20):  # stable baseline, sum = 0.80
        assert rv.on_group_update("G1", LEGS_META) == []

    _set_book(store, "A", 0.59, 100, 0.61, 100)  # A reprices to mid 0.60
    sigs = rv.on_group_update("G1", LEGS_META)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.kind == "partition_sum_drift"
    assert sig.exec_sets == 0.0
    assert sig.features["direction"] == "rich"
    assert sig.features["mover_token"] == "A"
    assert sig.features["laggard_token"] == "B"  # B's book is the older one
    assert sig.features["z"] >= 3.0
    # debounced
    assert rv.on_group_update("G1", LEGS_META) == []


def test_partition_skips_when_a_leg_book_is_missing(fees):
    store = BookStore()
    rv = RelativeValueMonitor(store, fees, min_samples=2)
    _set_book(store, "A", 0.29, 100, 0.31, 100)  # B missing
    assert rv.on_group_update("G1", LEGS_META) == []


# ---------- relative value: complement drift ----------

def test_complement_drift_fires_when_mid_sum_departs_from_one(fees):
    store = BookStore()
    rv = RelativeValueMonitor(store, fees, min_abs_dev=0.02, debounce_s=60)
    _set_book(store, "T1", 0.59, 100, 0.61, 100)  # yes mid 0.60
    _set_book(store, "T2", 0.49, 100, 0.51, 100)  # no mid 0.50 -> sum 1.10
    sigs = rv.on_market_update(META)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.kind == "complement_drift"
    assert sig.features["direction"] == "rich"
    assert sig.net_edge > 0


def test_complement_quiet_when_mids_pin_to_one(fees):
    store = BookStore()
    rv = RelativeValueMonitor(store, fees, min_abs_dev=0.02)
    _set_book(store, "T1", 0.59, 100, 0.61, 100)  # yes mid 0.60
    _set_book(store, "T2", 0.39, 100, 0.41, 100)  # no mid 0.40 -> sum 1.00
    assert rv.on_market_update(META) == []
