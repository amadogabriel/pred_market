from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import dashboard
from pm.core import db


@dataclass(frozen=True)
class _Settings:
    paper_portfolio_usd: float = 50.0
    heartbeat_path: Path = Path(__file__)
    heartbeat_stale_after: int = 120
    events_dir: Path = Path(__file__).parent / "events"


def _signal(conn, *, strategy: str, kind: str, exec_sets: float,
            net_edge: float, outcome: float | None, ts: float) -> None:
    signal_id = db.log_signal(
        conn,
        strategy=strategy,
        kind=kind,
        group_id="G1",
        legs=[{"token_id": "A", "market_id": "MA", "side": "BUY", "price": 0.50, "size": 10}],
        gross_edge=net_edge,
        fees=0.0,
        net_edge=net_edge,
        exec_sets=exec_sets,
        features={},
    )
    conn.execute("UPDATE signal_log SET ts=? WHERE signal_id=?", (ts, signal_id))
    if outcome is not None:
        conn.execute(
            "UPDATE signal_log SET outcome=?, pnl=? WHERE signal_id=?",
            (outcome, outcome * exec_sets, signal_id),
        )


def test_dashboard_signal_summary_rolls_up_strategy_stats(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    _signal(conn, strategy="alpha", kind="one", exec_sets=10, net_edge=0.02, outcome=0.10, ts=1)
    _signal(conn, strategy="alpha", kind="two", exec_sets=0, net_edge=0.03, outcome=-0.05, ts=2)
    _signal(conn, strategy="beta", kind="one", exec_sets=5, net_edge=0.01, outcome=None, ts=3)

    state = dashboard.query_state(conn, _Settings())

    assert state["signal_summary"]["signals"] == 3
    assert state["signal_summary"]["labeled"] == 2
    assert state["signal_summary"]["executable"] == 2
    assert state["signal_summary"]["avg_outcome"] == pytest.approx(0.025)
    assert state["signal_summary"]["hit_rate"] == pytest.approx(0.5)
    assert state["signal_summary"]["signal_ev"] == pytest.approx(0.25)
    assert state["signal_summary"]["sim_pnl"] == pytest.approx(1.0)

    alpha = next(r for r in state["signal_by_strategy"] if r["strategy"] == "alpha")
    assert alpha["signals"] == 2
    assert alpha["labeled"] == 2
    assert alpha["executable"] == 1
    assert alpha["hit_rate"] == pytest.approx(0.5)
