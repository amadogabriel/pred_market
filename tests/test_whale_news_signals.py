"""Tests for whale-follow and news signal scanners."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from pm.core import db
from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine
from pm.onchain.wallet_tracker import (ensure_schema, record_resolution,
                                        track_wallet, upsert_position)
from pm.signals.news_signal import NewsSignalScanner
from pm.signals.whale_follow import WhaleFollowTracker

FEES_YAML = Path(__file__).resolve().parent.parent / "config" / "fees.yaml"


@pytest.fixture
def fees() -> FeeEngine:
    return FeeEngine.from_yaml(FEES_YAML)


def _seed_market(conn: sqlite3.Connection, *, market_id="M1",
                  question="Fed raises rates in March?",
                  category="finance", token_yes="T1", token_no="T2") -> None:
    db.upsert_market(conn, {
        "market_id": market_id, "venue": "polymarket",
        "question": question, "slug": "fed",
        "category": category, "tags_json": None, "end_date": None,
        "active": 1, "closed": 0, "neg_risk": 0, "neg_risk_id": None,
        "token_yes": token_yes, "token_no": token_no,
        "liquidity": 5000.0, "volume_24h": 0.0})


def _seed_book(books: BookStore, token: str, mid: float) -> None:
    half = 0.005
    books.handle_ws_message({
        "event_type": "book", "asset_id": token,
        "bids": [{"price": round(mid - half, 4), "size": 100}],
        "asks": [{"price": round(mid + half, 4), "size": 100}]})


# ---------- whale-follow ----------

def test_whale_signal_fires_on_high_calibration_tracked_wallet(fees, tmp_path):
    conn = db.connect(tmp_path / "state.db")
    ensure_schema(conn)
    _seed_market(conn)
    books = BookStore()
    _seed_book(books, "T1", 0.55)
    track_wallet(conn, "0xWhale", label="whale")
    # Resolve 10 positions all correct -> calibration 1.0
    for i in range(10):
        pid = upsert_position(conn, wallet="0xWhale", token_id="t_old",
                              market_id="M0", side="BUY", value_raw=10**8,
                              tx_hash=f"0x{i}", block_number=i)
        record_resolution(conn, pid, outcome=0.05, pnl=5.0)

    tracker = WhaleFollowTracker(books=books, fees=fees, conn=conn,
                                  debounce_s=0.001, min_calibration=0.55,
                                  min_resolved=5, min_value_raw=10**8)
    payload = {"wallet": "0xwhale", "token_id": "T1", "market_id": "M1",
               "side": "BUY", "value_raw": 5 * 10**8,
               "tx_hash": "0xabc", "block": 999}
    sigs = tracker.on_whale_transfer(payload)
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.strategy == "whale_follow"
    assert sig.kind == "tracked_wallet_position"
    assert sig.exec_sets == 0.0
    assert sig.legs[0]["side"] == "BUY"
    assert sig.features["calibration"] == 1.0


def test_whale_signal_skips_untracked_wallet(fees, tmp_path):
    conn = db.connect(tmp_path / "state.db")
    ensure_schema(conn)
    _seed_market(conn)
    books = BookStore()
    _seed_book(books, "T1", 0.55)
    tracker = WhaleFollowTracker(books=books, fees=fees, conn=conn,
                                  min_calibration=0.55, min_resolved=5,
                                  min_value_raw=10**8)
    sigs = tracker.on_whale_transfer({
        "wallet": "0xUnknown", "token_id": "T1", "market_id": "M1",
        "side": "BUY", "value_raw": 10**9, "tx_hash": "0xa", "block": 1})
    assert sigs == []


def test_whale_signal_skips_below_calibration(fees, tmp_path):
    conn = db.connect(tmp_path / "state.db")
    ensure_schema(conn)
    _seed_market(conn)
    books = BookStore()
    _seed_book(books, "T1", 0.55)
    track_wallet(conn, "0xwhale")
    # 5 resolved, 0 correct -> calibration 0
    for i in range(5):
        pid = upsert_position(conn, wallet="0xwhale", token_id="t",
                              market_id="M0", side="BUY", value_raw=10**8,
                              tx_hash=f"0x{i}", block_number=i)
        record_resolution(conn, pid, outcome=-0.01, pnl=-1.0)
    tracker = WhaleFollowTracker(books=books, fees=fees, conn=conn,
                                  min_calibration=0.55, min_resolved=5,
                                  min_value_raw=10**8)
    sigs = tracker.on_whale_transfer({
        "wallet": "0xwhale", "token_id": "T1", "market_id": "M1",
        "side": "BUY", "value_raw": 10**9, "tx_hash": "0xa", "block": 1})
    assert sigs == []


def test_whale_signal_skips_small_value(fees, tmp_path):
    conn = db.connect(tmp_path / "state.db")
    ensure_schema(conn)
    _seed_market(conn)
    books = BookStore()
    _seed_book(books, "T1", 0.55)
    track_wallet(conn, "0xwhale")
    pid = upsert_position(conn, wallet="0xwhale", token_id="t", market_id="M0",
                          side="BUY", value_raw=10**8, tx_hash="0xa",
                          block_number=1)
    record_resolution(conn, pid, outcome=0.05, pnl=5.0)
    tracker = WhaleFollowTracker(books=books, fees=fees, conn=conn,
                                  min_calibration=0.0, min_resolved=1,
                                  min_value_raw=10**9)  # require huge size
    sigs = tracker.on_whale_transfer({
        "wallet": "0xwhale", "token_id": "T1", "market_id": "M1",
        "side": "BUY", "value_raw": 10**8, "tx_hash": "0xa", "block": 1})
    assert sigs == []


# ---------- news ----------

def test_news_signal_matches_and_fires(fees, tmp_path):
    conn = db.connect(tmp_path / "state.db")
    _seed_market(conn, market_id="M1",
                  question="Will the Fed raise rates in March?")
    books = BookStore()
    _seed_book(books, "T1", 0.50)
    scanner = NewsSignalScanner(books=books, fees=fees, conn=conn,
                                 debounce_s=0.001, min_overlap=2)
    sigs = scanner.on_article({
        "feed": "test", "title": "Fed approves rate increase",
        "summary": "The central bank raised rates today.",
        "guid": "g1"})
    assert len(sigs) == 1
    sig = sigs[0]
    assert sig.strategy == "news"
    assert sig.kind == "headline_match"
    assert sig.exec_sets == 0.0
    assert sig.legs[0]["side"] == "BUY"
    assert sig.features["polarity"] == 1


def test_news_signal_skips_unrelated_headline(fees, tmp_path):
    conn = db.connect(tmp_path / "state.db")
    _seed_market(conn, market_id="M1",
                  question="Will the Fed raise rates in March?")
    books = BookStore()
    _seed_book(books, "T1", 0.50)
    scanner = NewsSignalScanner(books=books, fees=fees, conn=conn,
                                 min_overlap=2)
    sigs = scanner.on_article({
        "feed": "test", "title": "Bitcoin rallies to new highs",
        "summary": "Crypto markets jumped.", "guid": "g1"})
    assert sigs == []


def test_news_signal_neutral_polarity_no_fire(fees, tmp_path):
    conn = db.connect(tmp_path / "state.db")
    _seed_market(conn, market_id="M1",
                  question="Will the Fed raise rates in March?")
    books = BookStore()
    _seed_book(books, "T1", 0.50)
    scanner = NewsSignalScanner(books=books, fees=fees, conn=conn,
                                 min_overlap=2)
    sigs = scanner.on_article({
        "feed": "test", "title": "Fed officials hold rate meeting",
        "summary": "Discussion is ongoing.", "guid": "g1"})
    assert sigs == []


def test_news_signal_no_book_no_fire(fees, tmp_path):
    conn = db.connect(tmp_path / "state.db")
    _seed_market(conn, market_id="M1",
                  question="Will the Fed raise rates in March?")
    books = BookStore()  # no book seeded
    scanner = NewsSignalScanner(books=books, fees=fees, conn=conn,
                                 min_overlap=2)
    sigs = scanner.on_article({
        "feed": "test", "title": "Fed approves rate increase",
        "summary": "", "guid": "g1"})
    assert sigs == []
