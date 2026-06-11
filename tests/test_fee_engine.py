"""Tests for the fee engine — the load-bearing component.

Covers the published Polymarket/Kalshi fee models, min-edge, venue/date
errors, and schedule versioning.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from pm.execution.fee_engine import FeeEngine, Schedule

FEES_YAML = Path(__file__).resolve().parent.parent / "config" / "fees.yaml"
PM_DATE = date(2026, 5, 1)  # after the 2026-04-01 polymarket schedule


@pytest.fixture
def fe() -> FeeEngine:
    return FeeEngine.from_yaml(FEES_YAML)


def test_geopolitics_fee_is_always_zero(fe):
    for p in (0.01, 0.25, 0.50, 0.75, 0.99):
        assert fe.taker_fee("polymarket", "geopolitics", p, 100, on=PM_DATE) == 0.0


@pytest.mark.parametrize("category,rate", [
    ("politics", 0.04), ("sports", 0.03), ("crypto", 0.072), ("economics", 0.06),
])
def test_peak_fee_at_p_half_matches_published_rate(fe, category, rate):
    # peak fee is at p=0.50: shares * rate * 0.25
    fee = fe.taker_fee("polymarket", category, 0.50, 100, on=PM_DATE)
    assert fee == pytest.approx(100 * rate * 0.25, abs=1e-9)


def test_fee_near_zero_at_extreme_prices(fe):
    lo = fe.taker_fee("polymarket", "politics", 0.01, 100, on=PM_DATE)
    hi = fe.taker_fee("polymarket", "politics", 0.99, 100, on=PM_DATE)
    assert lo < 0.05 and hi < 0.05
    assert lo == pytest.approx(hi, abs=1e-6)  # symmetric in p


def test_maker_fee_always_zero_polymarket(fe):
    for p in (0.1, 0.5, 0.9):
        assert fe.maker_fee("polymarket", "politics", p, 100, on=PM_DATE) == 0.0


def test_kalshi_rounds_up_to_cent(fe):
    # 1 share at p=0.5, rate 0.07 -> raw 0.0175 -> ceil to 0.02
    fee = fe.taker_fee("kalshi", None, 0.50, 1, on=date(2026, 5, 1))
    assert fee == 0.02
    # always a whole number of cents
    fee2 = fe.taker_fee("kalshi", None, 0.37, 13, on=date(2026, 5, 1))
    assert round(fee2 * 100) == pytest.approx(fee2 * 100, abs=1e-9)


def test_min_edge_is_per_share_fee_plus_buffer(fe):
    per_share = fe.taker_fee("polymarket", "politics", 0.50, 1.0, on=PM_DATE)
    me = fe.min_edge("polymarket", "politics", 0.50, statistical_buffer=0.04, on=PM_DATE)
    assert me == pytest.approx(per_share + 0.04, abs=1e-9)


def test_wrong_venue_raises_keyerror(fe):
    with pytest.raises(KeyError):
        fe.taker_fee("nasdaq", "politics", 0.5, 100)


def test_date_before_earliest_schedule_raises_keyerror(fe):
    with pytest.raises(KeyError):
        fe.schedule("polymarket", on=date(2020, 1, 1))


def test_schedule_versioning_picks_latest_on_or_before():
    old = Schedule("v", date(2026, 1, 1), "rate_p_one_minus_p", 0.0, 0.05, {})
    new = Schedule("v", date(2026, 6, 1), "rate_p_one_minus_p", 0.0, 0.09, {})
    fe = FeeEngine([new, old])  # deliberately out of order
    assert fe.schedule("v", on=date(2026, 5, 31)).effective_date == date(2026, 1, 1)
    assert fe.schedule("v", on=date(2026, 6, 1)).effective_date == date(2026, 6, 1)
    assert fe.schedule("v", on=date(2026, 7, 1)).effective_date == date(2026, 6, 1)
