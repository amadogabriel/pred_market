from __future__ import annotations

from dataclasses import dataclass

import pytest

import dashboard
from pm.core import db


@dataclass(frozen=True)
class _Settings:
    paper_portfolio_usd: float = 50.0
    execution_strategies: frozenset[str] = frozenset({"struct_arb"})


def _signal(conn, *, kind: str, legs: list[dict], exec_sets: float,
            net_edge: float, outcome: float, ts: float) -> int:
    signal_id = db.log_signal(
        conn,
        strategy="struct_arb",
        kind=kind,
        group_id="G1",
        legs=legs,
        gross_edge=net_edge,
        fees=0.0,
        net_edge=net_edge,
        exec_sets=exec_sets,
        features={},
    )
    conn.execute(
        "UPDATE signal_log SET outcome=?, pnl=?, ts=? WHERE signal_id=?",
        (outcome, outcome * exec_sets, ts, signal_id),
    )
    return signal_id


def test_paper_portfolio_replay_sizes_against_total_bankroll(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    _signal(
        conn,
        kind="partition_buy_all",
        legs=[
            {"token_id": "A", "market_id": "MA", "side": "BUY", "price": 0.40, "size": 100},
            {"token_id": "B", "market_id": "MB", "side": "BUY", "price": 0.50, "size": 100},
        ],
        exec_sets=100.0,
        net_edge=0.05,
        outcome=0.02,
        ts=1.0,
    )
    _signal(
        conn,
        kind="partition_sell_all",
        legs=[
            {"token_id": "A", "market_id": "MA", "side": "SELL", "price": 0.50, "size": 20},
            {"token_id": "B", "market_id": "MB", "side": "SELL", "price": 0.55, "size": 20},
        ],
        exec_sets=20.0,
        net_edge=0.03,
        outcome=0.01,
        ts=2.0,
    )

    portfolio = dashboard._paper_portfolio(conn, _Settings())

    assert portfolio["bankroll"] == 50.0
    assert portfolio["selected_bets"] == 2
    assert portfolio["deployed_notional"] == pytest.approx(50.0)
    assert portfolio["sold_notional"] == pytest.approx(21.0)
    assert portfolio["cash"] == pytest.approx(21.0)
    assert portfolio["open_cost"] == pytest.approx(32.0)
    assert portfolio["total_pnl_at_cost"] == pytest.approx(3.0)
    assert portfolio["realized_pnl"] == pytest.approx(3.0)
    assert portfolio["decisions"][0]["reason"] == "cash cap"


def test_paper_portfolio_respects_strategy_allowlist(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    db.log_signal(
        conn,
        strategy="not_allowed",
        kind="test",
        group_id="G1",
        legs=[{"token_id": "A", "market_id": "MA", "side": "BUY", "price": 0.50, "size": 10}],
        gross_edge=0.02,
        fees=0.0,
        net_edge=0.02,
        exec_sets=10.0,
        features={},
    )

    portfolio = dashboard._paper_portfolio(conn, _Settings())

    assert portfolio["selected_bets"] == 0
    assert portfolio["cash"] == 50.0
    assert portfolio["decisions"][0]["reason"] == "strategy not in execution allowlist"
