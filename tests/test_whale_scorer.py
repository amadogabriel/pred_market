"""Tests for whale position scoring + auto-promotion."""
from __future__ import annotations

import sqlite3

import pytest

from pm.core import db
from pm.onchain.wallet_tracker import (ensure_schema, tracked_wallets,
                                        upsert_position, wallet_stat)
from pm.onchain.whale_scorer import (demote_wallets, promote_wallets,
                                      score_unresolved)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.db")
    ensure_schema(c)
    yield c
    c.close()


def _resolved_market(c, *, market_id, token_yes, token_no, yes_payout):
    """Insert a closed market with outcome prices [yes, no]."""
    no_payout = 1 - yes_payout
    db.upsert_market(c, {
        "market_id": market_id, "venue": "polymarket",
        "question": "q", "slug": "s", "category": "politics",
        "tags_json": None, "end_date": None, "active": 0, "closed": 1,
        "neg_risk": 0, "neg_risk_id": None,
        "token_yes": token_yes, "token_no": token_no,
        "liquidity": 0.0, "volume_24h": 0.0})
    c.execute("UPDATE markets SET outcome_prices_json=? WHERE market_id=?",
              (f'["{yes_payout}", "{no_payout}"]', market_id))


def test_score_buy_winner_is_correct(conn):
    _resolved_market(conn, market_id="M1", token_yes="TY", token_no="TN",
                     yes_payout=1)
    pid = upsert_position(conn, wallet="0xw", token_id="TY", market_id="M1",
                          side="BUY", value_raw=10**9, tx_hash="0x",
                          block_number=1)
    assert score_unresolved(conn) == 1
    stat = wallet_stat(conn, "0xw")
    assert stat.n_resolved == 1 and stat.n_correct == 1


def test_score_buy_loser_is_incorrect(conn):
    _resolved_market(conn, market_id="M1", token_yes="TY", token_no="TN",
                     yes_payout=0)
    pid = upsert_position(conn, wallet="0xw", token_id="TY", market_id="M1",
                          side="BUY", value_raw=10**9, tx_hash="0x",
                          block_number=1)
    score_unresolved(conn)
    stat = wallet_stat(conn, "0xw")
    assert stat.n_resolved == 1 and stat.n_correct == 0


def test_score_sell_inverts(conn):
    # SELL a token that won => incorrect; SELL a token that lost => correct
    _resolved_market(conn, market_id="M1", token_yes="TY", token_no="TN",
                     yes_payout=1)
    upsert_position(conn, wallet="0xseller", token_id="TY", market_id="M1",
                    side="SELL", value_raw=10**9, tx_hash="0x", block_number=1)
    score_unresolved(conn)
    stat = wallet_stat(conn, "0xseller")
    assert stat.n_resolved == 1 and stat.n_correct == 0


def test_unresolved_market_not_scored(conn):
    # market not closed -> no outcome prices -> not scorable
    db.upsert_market(conn, {
        "market_id": "M2", "venue": "polymarket", "question": "q", "slug": "s",
        "category": "politics", "tags_json": None, "end_date": None,
        "active": 1, "closed": 0, "neg_risk": 0, "neg_risk_id": None,
        "token_yes": "TY2", "token_no": "TN2", "liquidity": 0.0, "volume_24h": 0.0})
    upsert_position(conn, wallet="0xw", token_id="TY2", market_id="M2",
                    side="BUY", value_raw=10**9, tx_hash="0x", block_number=1)
    assert score_unresolved(conn) == 0
    stat = wallet_stat(conn, "0xw")
    assert stat.n_resolved == 0


def test_score_is_idempotent(conn):
    _resolved_market(conn, market_id="M1", token_yes="TY", token_no="TN",
                     yes_payout=1)
    upsert_position(conn, wallet="0xw", token_id="TY", market_id="M1",
                    side="BUY", value_raw=10**9, tx_hash="0x", block_number=1)
    assert score_unresolved(conn) == 1
    assert score_unresolved(conn) == 0  # already scored, not re-counted
    assert wallet_stat(conn, "0xw").n_resolved == 1


def test_promote_wallet_above_bar(conn):
    _resolved_market(conn, market_id="M1", token_yes="TY", token_no="TN",
                     yes_payout=1)
    # 3 correct BUY-winner positions on the same resolved token
    for i in range(3):
        upsert_position(conn, wallet="0xgood", token_id="TY", market_id="M1",
                        side="BUY", value_raw=10**9, tx_hash=f"0x{i}",
                        block_number=i)
    score_unresolved(conn)
    assert "0xgood" not in tracked_wallets(conn)
    n = promote_wallets(conn, min_calibration=0.6, min_resolved=3)
    assert n == 1
    assert "0xgood" in tracked_wallets(conn)


def test_promote_skips_below_bar(conn):
    _resolved_market(conn, market_id="M1", token_yes="TY", token_no="TN",
                     yes_payout=0)  # YES loses
    for i in range(3):
        upsert_position(conn, wallet="0xbad", token_id="TY", market_id="M1",
                        side="BUY", value_raw=10**9, tx_hash=f"0x{i}",
                        block_number=i)
    score_unresolved(conn)  # all incorrect -> calibration 0
    n = promote_wallets(conn, min_calibration=0.6, min_resolved=3)
    assert n == 0
    assert "0xbad" not in tracked_wallets(conn)


def test_promote_requires_min_resolved(conn):
    _resolved_market(conn, market_id="M1", token_yes="TY", token_no="TN",
                     yes_payout=1)
    upsert_position(conn, wallet="0xnew", token_id="TY", market_id="M1",
                    side="BUY", value_raw=10**9, tx_hash="0x", block_number=1)
    score_unresolved(conn)  # 1 correct, calibration 1.0 but only 1 resolved
    n = promote_wallets(conn, min_calibration=0.6, min_resolved=8)
    assert n == 0  # not enough resolved bets yet


def test_demote_fallen_wallet(conn):
    from pm.onchain.wallet_tracker import track_wallet
    _resolved_market(conn, market_id="M1", token_yes="TY", token_no="TN",
                     yes_payout=0)
    track_wallet(conn, "0xfallen", tracked=True)
    for i in range(10):
        upsert_position(conn, wallet="0xfallen", token_id="TY", market_id="M1",
                        side="BUY", value_raw=10**9, tx_hash=f"0x{i}",
                        block_number=i)
    score_unresolved(conn)  # all wrong -> calibration 0
    n = demote_wallets(conn, min_calibration=0.6, min_resolved=8)
    assert n == 1
    assert "0xfallen" not in tracked_wallets(conn)
