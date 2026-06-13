"""Kelly position sizing for binary-payoff prediction-market contracts.

Standard Kelly for a binary contract paying $1 on outcome at market price p with
true probability q:

    f* = (q - p) / (1 - p)        for BUY YES at price p < q
    f* = (p - q) / p              for SELL YES at price p > q

This is the per-side fraction of *risk capital* committed. Full Kelly maximises
expected log-growth but is famously variance-heavy on binary outcomes (single
losing turn = ~62% drawdown at full Kelly when the bet is favoured); fractional
Kelly (half, quarter) captures most of the growth with much less variance.

The strategy report (PRED-MKT-001) recommends half-Kelly as the default and
quarter-Kelly when the edge estimate is noisy. We expose both and a
configurable factor.

This module is pure logic: it returns a fraction in [0, 1]. The caller is
responsible for combining it with the live capital base, the per-order and
per-signal notional caps in pm.execution.risk, and the time-to-expiry haircut.
Kelly sizing does NOT bypass any risk cap — the smaller of (kelly_size, cap) is
the binding limit.
"""
from __future__ import annotations

from dataclasses import dataclass

FULL = 1.0
HALF = 0.5
QUARTER = 0.25

# Hard floor on edge required to take a position. Below this, even full Kelly
# returns a fraction so small the trade is not worth the risk of model error.
MIN_EDGE_ABS = 1e-4


@dataclass(frozen=True)
class KellyResult:
    fraction: float       # fraction of risk capital, in [0, 1]
    side: str             # 'BUY' | 'SELL' | 'PASS'
    raw_kelly: float      # the full-Kelly value before fractional scaling
    edge: float           # signed (q - p)
    reason: str = ""      # diagnostic ('ok', 'no_edge', 'p_at_boundary', ...)


def kelly_fraction(market_price: float, model_prob: float, *,
                   factor: float = HALF) -> KellyResult:
    """Return the fractional-Kelly position sizing for a binary contract.

    Args:
        market_price: the current YES mid in [0, 1]
        model_prob: the model's estimate of true probability of YES
        factor: scaling factor for fractional Kelly (1.0=full, 0.5=half, ...)

    Returns:
        KellyResult with fraction, side, and diagnostic reason.
    """
    if not 0.0 < market_price < 1.0:
        return KellyResult(0.0, "PASS", 0.0, 0.0, "p_at_boundary")
    if not 0.0 < model_prob < 1.0:
        return KellyResult(0.0, "PASS", 0.0, 0.0, "q_at_boundary")
    if not 0.0 < factor <= 1.0:
        return KellyResult(0.0, "PASS", 0.0, 0.0, "bad_factor")

    edge = model_prob - market_price
    if abs(edge) < MIN_EDGE_ABS:
        return KellyResult(0.0, "PASS", 0.0, edge, "no_edge")

    if edge > 0:
        # BUY YES at market_price, expected payoff $1 with prob q
        # Kelly: f* = (q*(1-p) - (1-q)*p) / ((1-p)*p) reduces to (q-p)/(1-p)
        raw = edge / (1.0 - market_price)
        side = "BUY"
    else:
        # SELL YES at market_price (equivalent to BUY NO at 1-p)
        raw = (-edge) / market_price
        side = "SELL"

    raw = max(0.0, min(1.0, raw))
    return KellyResult(round(raw * factor, 6), side, round(raw, 6), edge, "ok")


def kelly_notional(market_price: float, model_prob: float, capital: float, *,
                   factor: float = HALF,
                   per_trade_cap: float | None = None) -> tuple[float, KellyResult]:
    """Convert fractional Kelly into a dollar notional bounded by capital and cap.

    Returns (notional, KellyResult). The KellyResult is unchanged from
    kelly_fraction; the notional is min(fraction * capital, per_trade_cap).
    """
    res = kelly_fraction(market_price, model_prob, factor=factor)
    if res.side == "PASS" or capital <= 0:
        return (0.0, res)
    notional = res.fraction * capital
    if per_trade_cap is not None and per_trade_cap > 0:
        notional = min(notional, per_trade_cap)
    return (round(notional, 2), res)


def time_to_expiry_haircut(seconds_to_expiry: float, *,
                           cliff_seconds: float = 72 * 3600,
                           floor: float = 0.25) -> float:
    """Within 72h of expiry, scale size linearly down to `floor` at t=0.

    Per the strategy report: "Reduce size within 72 hours of expiry — time
    decay amplifies adverse moves." Returns a multiplier in [floor, 1.0].
    """
    if seconds_to_expiry <= 0:
        return floor
    if seconds_to_expiry >= cliff_seconds:
        return 1.0
    # linear scale from floor at t=0 to 1.0 at t=cliff
    return floor + (1.0 - floor) * (seconds_to_expiry / cliff_seconds)
