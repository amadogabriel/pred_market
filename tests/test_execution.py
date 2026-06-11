"""Tests for execution planning, risk gates, and accounting."""
from __future__ import annotations

from pathlib import Path

import pytest

from pm.core import db
from pm.execution.accounting import apply_fill
from pm.execution.models import intents_from_signal
from pm.execution.risk import RiskLimits, RiskManager


def _limits(tmp_path: Path, **overrides) -> RiskLimits:
    base = {
        "execution_enabled": True,
        "execution_mode": "dry_run",
        "live_trading": False,
        "hard_live_gate": False,
        "max_order_notional": 50.0,
        "max_signal_notional": 100.0,
        "max_open_notional": 250.0,
        "max_daily_loss": 25.0,
        "max_recon_diff_for_execution": 0.01,
        "allow_unverified_negrisk": False,
        "verified_groups_path": tmp_path / "verified.txt",
        "kill_switch_path": tmp_path / "KILL_SWITCH",
    }
    base.update(overrides)
    return RiskLimits(**base)


def _signal(kind: str = "complement") -> dict:
    return {
        "signal_id": 7,
        "strategy": "struct_arb",
        "kind": kind,
        "group_id": "G1",
        "exec_sets": 10.0,
        "legs": [
            {"token_id": "YES", "market_id": "M1", "side": "BUY", "price": 0.41, "size": 20},
            {"token_id": "NO", "market_id": "M1", "side": "BUY", "price": 0.55, "size": 20},
        ],
    }


def test_intents_from_signal_preserves_legs_and_caps_size():
    intents = intents_from_signal(_signal(), max_sets=5)
    assert len(intents) == 2
    assert intents[0].signal_id == 7
    assert intents[0].side == "BUY"
    assert intents[0].size == 5
    assert intents[0].notional == 0.41 * 5
    assert intents[0].client_order_id


def test_risk_rejects_when_execution_disabled(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    signal = _signal()
    intents = intents_from_signal(signal)
    risk = RiskManager(_limits(tmp_path, execution_enabled=False))
    decision = risk.check_plan(conn, signal, intents)
    assert decision.approved is False
    assert decision.code == "execution_disabled"


def test_risk_allows_complement_dry_run_when_limits_pass(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    signal = _signal()
    intents = intents_from_signal(signal)
    risk = RiskManager(_limits(tmp_path))
    plan = risk.check_plan(conn, signal, intents)
    assert plan.approved is True
    assert [risk.check_intent(conn, i).approved for i in intents] == [True, True]


def test_risk_requires_verified_negrisk_group(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    signal = _signal("partition_buy_all")
    intents = intents_from_signal(signal)
    risk = RiskManager(_limits(tmp_path))
    decision = risk.check_plan(conn, signal, intents)
    assert decision.approved is False
    assert decision.code == "unverified_negrisk_group"

    (tmp_path / "verified.txt").write_text("G1\n", encoding="utf-8")
    risk = RiskManager(_limits(tmp_path))
    assert risk.check_plan(conn, signal, intents).approved is True


def test_risk_rejects_live_mode_without_hard_gate(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    signal = _signal()
    intents = intents_from_signal(signal)
    risk = RiskManager(_limits(tmp_path, execution_mode="live", live_trading=True, hard_live_gate=False))
    decision = risk.check_plan(conn, signal, intents)
    assert decision.approved is False
    assert decision.code == "live_gate_closed"


def test_risk_rejects_sell_without_inventory(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    signal = _signal()
    signal["legs"][0]["side"] = "SELL"
    intent = intents_from_signal(signal)[0]
    risk = RiskManager(_limits(tmp_path))
    decision = risk.check_intent(conn, intent)
    assert decision.approved is False
    assert decision.code == "insufficient_inventory"


def test_apply_fill_updates_position_and_realized_pnl(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    apply_fill(conn, venue="polymarket", market_id="M1", token_id="YES",
               side="BUY", price=0.40, size=10, fee=0)
    row = conn.execute("SELECT * FROM positions WHERE token_id='YES'").fetchone()
    assert row["size"] == 10
    assert row["avg_price"] == 0.40

    apply_fill(conn, venue="polymarket", market_id="M1", token_id="YES",
               side="SELL", price=0.50, size=4, fee=0.01)
    row = conn.execute("SELECT * FROM positions WHERE token_id='YES'").fetchone()
    assert row["size"] == 6
    assert row["avg_price"] == 0.40
    assert row["realized_pnl"] == pytest.approx(0.39)
