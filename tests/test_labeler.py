"""Tests for the signal outcome labeler."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from pm.core import db
from pm.core.books import BookStore
from pm.signals.labeler import label_once

SETTINGS = SimpleNamespace(
    label_horizon_s=900.0,
    label_max_age_s=86400.0,
    label_batch=200,
    stale_book_after=30.0,
)


def _set_book(store: BookStore, token: str, bid_p: float, ask_p: float) -> None:
    store.handle_ws_message({
        "event_type": "book", "asset_id": token,
        "bids": [{"price": bid_p, "size": 100}],
        "asks": [{"price": ask_p, "size": 100}]})


def _insert_signal(conn, legs, exec_sets=10.0, age_s=1000.0) -> int:
    sid = db.log_signal(conn, strategy="struct_arb", kind="complement", group_id="M1",
                        legs=legs, gross_edge=0.05, fees=0.01, net_edge=0.04,
                        exec_sets=exec_sets)
    conn.execute("UPDATE signal_log SET ts=? WHERE signal_id=?",
                 (time.time() - age_s, sid))
    return sid


def test_buy_leg_labeled_with_forward_mid_move(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    books = BookStore()
    sid = _insert_signal(conn, [{"token_id": "T1", "market_id": "M1",
                                 "side": "BUY", "price": 0.40, "size": 20}])
    _set_book(books, "T1", 0.50, 0.52)  # mid 0.51 -> forward edge +0.11

    assert label_once(conn, books, SETTINGS) == 1
    row = conn.execute("SELECT outcome, pnl FROM signal_log WHERE signal_id=?", (sid,)).fetchone()
    assert row["outcome"] == pytest.approx(0.11, abs=1e-4)
    assert row["pnl"] == pytest.approx(1.1, abs=1e-3)


def test_sell_leg_sign_is_inverted(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    books = BookStore()
    sid = _insert_signal(conn, [{"token_id": "T1", "market_id": "M1",
                                 "side": "SELL", "price": 0.40, "size": 20}])
    _set_book(books, "T1", 0.50, 0.52)  # mid rose; a SELL was wrong -> negative

    assert label_once(conn, books, SETTINGS) == 1
    row = conn.execute("SELECT outcome FROM signal_log WHERE signal_id=?", (sid,)).fetchone()
    assert row["outcome"] == pytest.approx(-0.11, abs=1e-4)


def test_immature_signal_not_labeled(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    books = BookStore()
    _insert_signal(conn, [{"token_id": "T1", "market_id": "M1",
                           "side": "BUY", "price": 0.40, "size": 20}],
                   age_s=10.0)  # younger than the 900s horizon
    _set_book(books, "T1", 0.50, 0.52)
    assert label_once(conn, books, SETTINGS) == 0


def test_signal_with_dead_book_retries_later(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    books = BookStore()  # no book for T1
    sid = _insert_signal(conn, [{"token_id": "T1", "market_id": "M1",
                                 "side": "BUY", "price": 0.40, "size": 20}])
    assert label_once(conn, books, SETTINGS) == 0
    row = conn.execute("SELECT outcome FROM signal_log WHERE signal_id=?", (sid,)).fetchone()
    assert row["outcome"] is None  # still unlabeled, will retry while in window


def test_multi_leg_average_and_half_rule(tmp_path):
    conn = db.connect(tmp_path / "state.db")
    books = BookStore()
    sid = _insert_signal(conn, [
        {"token_id": "T1", "market_id": "M1", "side": "BUY", "price": 0.40, "size": 20},
        {"token_id": "T2", "market_id": "M1", "side": "BUY", "price": 0.50, "size": 20},
    ])
    _set_book(books, "T1", 0.50, 0.52)  # +0.11
    # T2 has no book -> 1 of 2 legs labelable = exactly half -> labels with T1 only
    assert label_once(conn, books, SETTINGS) == 1
    row = conn.execute("SELECT outcome FROM signal_log WHERE signal_id=?", (sid,)).fetchone()
    assert row["outcome"] == pytest.approx(0.11, abs=1e-4)
