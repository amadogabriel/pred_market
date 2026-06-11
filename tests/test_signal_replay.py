"""Tests for the offline research-signal replay harness."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from pm.core import db
from pm.backtest.signal_replay import replay_signals
from pm.execution.fee_engine import FeeEngine

FEES_YAML = Path(__file__).resolve().parent.parent / "config" / "fees.yaml"


def _book_event(ts: float, token: str, bid: float, ask: float) -> dict:
    return {"ts": ts, "topic": "book", "payload": {
        "event_type": "book", "asset_id": token,
        "bids": [{"price": bid, "size": 100}],
        "asks": [{"price": ask, "size": 100}]}}


def test_replay_emits_and_labels_complement_drift(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    db.upsert_market(conn, {
        "market_id": "M1", "venue": "polymarket", "question": "q", "slug": "s",
        "category": "politics", "tags_json": None, "end_date": None,
        "active": 1, "closed": 0, "neg_risk": 0, "neg_risk_id": None,
        "token_yes": "T1", "token_no": "T2", "liquidity": 5000.0, "volume_24h": 0.0})

    t0 = time.time() - 10_000
    events = [
        _book_event(t0, "T1", 0.59, 0.61),          # yes mid 0.60
        _book_event(t0 + 1, "T2", 0.49, 0.51),      # no mid 0.50 -> sum 1.10, drift fires
        _book_event(t0 + 1000, "T1", 0.49, 0.51),   # T1 reprices to 0.50 after horizon
    ]
    day_dir = tmp_path / "events" / "2026-06-11"
    day_dir.mkdir(parents=True)
    with (day_dir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    fees = FeeEngine.from_yaml(FEES_YAML)
    result = replay_signals(tmp_path / "events", conn, fees, horizon_s=900.0)

    assert result.events == 3
    kinds = [(s.strategy, s.kind) for _, s in result.signals]
    assert ("rel_value", "complement_drift") in kinds

    stats = result.stats[("rel_value", "complement_drift")]
    assert stats.n == 1
    assert stats.labeled == 1
    # T1 leg: forward mid 0.50 vs signal price 0.60 -> -0.10; T2 leg unlabelable
    assert stats.avg_outcome == pytest.approx(-0.10, abs=1e-3)
    assert stats.hit_rate == 0.0


def test_replay_quiet_log_produces_no_signals(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    db.upsert_market(conn, {
        "market_id": "M1", "venue": "polymarket", "question": "q", "slug": "s",
        "category": "politics", "tags_json": None, "end_date": None,
        "active": 1, "closed": 0, "neg_risk": 0, "neg_risk_id": None,
        "token_yes": "T1", "token_no": "T2", "liquidity": 5000.0, "volume_24h": 0.0})

    t0 = time.time() - 10_000
    events = [
        _book_event(t0, "T1", 0.59, 0.61),       # yes mid 0.60
        _book_event(t0 + 1, "T2", 0.39, 0.41),   # no mid 0.40 -> sum 1.00, no drift
    ]
    day_dir = tmp_path / "events" / "2026-06-11"
    day_dir.mkdir(parents=True)
    with (day_dir / "events.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

    fees = FeeEngine.from_yaml(FEES_YAML)
    result = replay_signals(tmp_path / "events", conn, fees)
    assert result.signals == []
