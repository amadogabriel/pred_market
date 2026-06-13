"""Tests for Kelly position sizing."""
from __future__ import annotations

from pm.execution.sizing import (FULL, HALF, QUARTER, kelly_fraction,
                                  kelly_notional, time_to_expiry_haircut)


def test_kelly_buy_when_model_above_market():
    r = kelly_fraction(market_price=0.40, model_prob=0.60, factor=FULL)
    assert r.side == "BUY"
    assert r.raw_kelly > 0
    # (0.60 - 0.40) / (1 - 0.40) = 0.20 / 0.60 = 0.333...
    assert abs(r.raw_kelly - 1 / 3) < 1e-3
    assert r.fraction == r.raw_kelly  # full Kelly


def test_kelly_sell_when_model_below_market():
    r = kelly_fraction(market_price=0.70, model_prob=0.50, factor=FULL)
    assert r.side == "SELL"
    # (0.70 - 0.50) / 0.70 = 0.2857
    assert abs(r.raw_kelly - 2 / 7) < 1e-3


def test_half_kelly_halves_the_fraction():
    r = kelly_fraction(0.40, 0.60, factor=HALF)
    full = kelly_fraction(0.40, 0.60, factor=FULL)
    assert abs(r.fraction - full.raw_kelly * 0.5) < 1e-6


def test_quarter_kelly_quarters_the_fraction():
    r = kelly_fraction(0.40, 0.60, factor=QUARTER)
    full = kelly_fraction(0.40, 0.60, factor=FULL)
    assert abs(r.fraction - full.raw_kelly * 0.25) < 1e-6


def test_kelly_pass_when_no_edge():
    r = kelly_fraction(0.50, 0.50)
    assert r.side == "PASS"
    assert r.fraction == 0.0
    assert r.reason == "no_edge"


def test_kelly_pass_at_boundary():
    assert kelly_fraction(0.0, 0.5).side == "PASS"
    assert kelly_fraction(1.0, 0.5).side == "PASS"
    assert kelly_fraction(0.5, 0.0).side == "PASS"
    assert kelly_fraction(0.5, 1.0).side == "PASS"


def test_kelly_pass_below_min_edge():
    # very tiny edge < 1e-4 -> PASS
    r = kelly_fraction(0.50000, 0.50005)
    assert r.side == "PASS"


def test_kelly_fraction_never_exceeds_one():
    # An extreme prediction near certainty caps at 1.0
    r = kelly_fraction(0.10, 0.99, factor=FULL)
    assert 0 <= r.fraction <= 1.0


def test_kelly_notional_respects_cap():
    notional, _ = kelly_notional(0.30, 0.70, capital=1000, factor=FULL,
                                  per_trade_cap=25.0)
    assert notional == 25.0  # capped


def test_kelly_notional_no_cap():
    notional, res = kelly_notional(0.30, 0.70, capital=1000, factor=HALF)
    # (0.70 - 0.30) / (1 - 0.30) = 0.5714 raw; half = 0.2857; * 1000 = 285.71
    assert 280 <= notional <= 290
    assert res.side == "BUY"


def test_kelly_notional_pass_returns_zero():
    notional, res = kelly_notional(0.50, 0.50, capital=1000)
    assert notional == 0.0
    assert res.side == "PASS"


def test_time_to_expiry_full_scale_at_cliff():
    assert time_to_expiry_haircut(72 * 3600) == 1.0
    assert time_to_expiry_haircut(100 * 3600) == 1.0


def test_time_to_expiry_floor_at_expiry():
    h = time_to_expiry_haircut(0)
    assert h == 0.25  # default floor


def test_time_to_expiry_linear_in_between():
    # halfway through 72h window -> halfway between floor and 1.0
    h = time_to_expiry_haircut(36 * 3600)
    assert 0.6 < h < 0.65


def test_bad_factor_returns_pass():
    r = kelly_fraction(0.4, 0.6, factor=0.0)
    assert r.side == "PASS"
    r = kelly_fraction(0.4, 0.6, factor=-0.5)
    assert r.side == "PASS"
