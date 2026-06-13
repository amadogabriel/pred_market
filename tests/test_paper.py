"""Tests for the honest paper-trading simulator."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pm.core import db
from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine
from pm.execution.paper import PaperTrader, ensure_schema, init_account

FEES_YAML = Path(__file__).resolve().parent.parent / "config" / "fees.yaml"


class Settings:
    paper_bankroll = 100.0
    paper_per_trade = 10.0
    paper_hold_s = 1800.0
    stale_book_after = 30.0
    paper_strategies = "struct_arb,microstructure"


@pytest.fixture
def fees():
    return FeeEngine.from_yaml(FEES_YAML)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "state.db")
    yield c
    c.close()


def _market(c, *, market_id="M1", token_yes="TY", token_no="TN",
            category="politics", closed=0, prices=None):
    db.upsert_market(c, {
        "market_id": market_id, "venue": "polymarket", "question": "q",
        "slug": "s", "category": category, "tags_json": None, "end_date": None,
        "active": 0 if closed else 1, "closed": closed, "neg_risk": 0,
        "neg_risk_id": None, "token_yes": token_yes, "token_no": token_no,
        "liquidity": 5000.0, "volume_24h": 0.0})
    if prices is not None:
        c.execute("UPDATE markets SET outcome_prices_json=? WHERE market_id=?",
                  (json.dumps(prices), market_id))


def _book(books, token, bid, ask):
    books.handle_ws_message({
        "event_type": "book", "asset_id": token,
        "bids": [{"price": bid, "size": 1000}],
        "asks": [{"price": ask, "size": 1000}]})


def _signal(c, *, strategy="microstructure", kind="ofi_pressure",
            market_id="M1", token="TY", side="BUY"):
    return db.log_signal(
        c, strategy=strategy, kind=kind, group_id=market_id,
        legs=[{"token_id": token, "market_id": market_id, "side": side,
               "price": 0.5, "size": 0.0}],
        gross_edge=0.0, fees=0.0, net_edge=0.0, exec_sets=0.0, features={})


def test_account_initializes_at_bankroll(conn, fees):
    pt = PaperTrader(conn, BookStore(), fees, Settings())
    s = pt.summary()
    assert s["bankroll"] == 100.0
    assert s["cash"] == 100.0
    assert s["equity"] == 100.0
    assert s["n_trades"] == 0


def test_buy_signal_opens_position_and_spends_cash(conn, fees):
    books = BookStore()
    _market(conn)
    _book(books, "TY", 0.49, 0.51)
    _signal(conn, side="BUY", token="TY")
    pt = PaperTrader(conn, books, fees, Settings())
    assert pt.process_new_signals() == 1
    s = pt.summary()
    assert s["n_open"] == 1
    assert s["n_trades"] == 1
    assert s["cash"] < 100.0           # cash was spent
    # bought at the ASK (0.51), not the mid
    row = conn.execute("SELECT entry_price, token_id FROM paper_trades").fetchone()
    assert row["entry_price"] == 0.51
    assert row["token_id"] == "TY"


def test_sell_signal_buys_complement(conn, fees):
    books = BookStore()
    _market(conn, token_yes="TY", token_no="TN")
    _book(books, "TN", 0.48, 0.52)      # we'll buy the NO token
    _signal(conn, side="SELL", token="TY")
    pt = PaperTrader(conn, books, fees, Settings())
    assert pt.process_new_signals() == 1
    row = conn.execute("SELECT token_id, view_side FROM paper_trades").fetchone()
    assert row["token_id"] == "TN"      # complement held
    assert row["view_side"] == "SELL"


def test_no_duplicate_position_same_token(conn, fees):
    books = BookStore()
    _market(conn)
    _book(books, "TY", 0.49, 0.51)
    _signal(conn, token="TY")
    _signal(conn, token="TY")
    pt = PaperTrader(conn, books, fees, Settings())
    opened = pt.process_new_signals()
    assert opened == 1                  # second signal on same token skipped


def test_skips_when_cash_exhausted(conn, fees):
    books = BookStore()
    # 12 distinct markets/tokens, $10/trade on $100 -> ~9-10 fundable
    for i in range(12):
        _market(conn, market_id=f"M{i}", token_yes=f"T{i}", token_no=f"N{i}")
        _book(books, f"T{i}", 0.49, 0.51)
        _signal(conn, market_id=f"M{i}", token=f"T{i}")
    pt = PaperTrader(conn, books, fees, Settings())
    opened = pt.process_new_signals()
    assert opened <= 10
    assert pt.summary()["cash"] < pt.per_trade  # ran out


def test_settle_at_resolution_win(conn, fees):
    books = BookStore()
    _market(conn, token_yes="TY")
    _book(books, "TY", 0.49, 0.51)
    _signal(conn, token="TY", side="BUY")
    pt = PaperTrader(conn, books, fees, Settings())
    pt.process_new_signals()
    # market resolves YES (token_yes pays 1)
    conn.execute("UPDATE markets SET closed=1, outcome_prices_json=? WHERE market_id=?",
                 (json.dumps(["1", "0"]), "M1"))
    settled, closed = pt.mark_and_settle()
    assert settled == 1
    row = conn.execute("SELECT status, pnl, exit_price FROM paper_trades").fetchone()
    assert row["status"] == "settled"
    assert row["exit_price"] == 1.0
    assert row["pnl"] > 0               # bought ~0.51, paid out 1.0


def test_settle_at_resolution_loss(conn, fees):
    books = BookStore()
    _market(conn, token_yes="TY")
    _book(books, "TY", 0.49, 0.51)
    _signal(conn, token="TY", side="BUY")
    pt = PaperTrader(conn, books, fees, Settings())
    pt.process_new_signals()
    conn.execute("UPDATE markets SET closed=1, outcome_prices_json=? WHERE market_id=?",
                 (json.dumps(["0", "1"]), "M1"))   # YES loses
    pt.mark_and_settle()
    row = conn.execute("SELECT status, pnl FROM paper_trades").fetchone()
    assert row["status"] == "settled"
    assert row["pnl"] < 0


def test_horizon_close_sells_at_bid(conn, fees):
    books = BookStore()
    _market(conn)
    _book(books, "TY", 0.49, 0.51)
    _signal(conn, token="TY")
    s = Settings(); s.paper_hold_s = 0.0   # close immediately
    pt = PaperTrader(conn, books, s, fees) if False else PaperTrader(conn, books, fees, s)
    pt.process_new_signals()
    settled, closed = pt.mark_and_settle()
    assert closed == 1
    row = conn.execute("SELECT status, exit_price FROM paper_trades").fetchone()
    assert row["status"] == "closed"
    assert row["exit_price"] == 0.49        # sold at the BID


def test_round_trip_at_flat_price_loses_to_costs(conn, fees):
    # Buy at ask 0.51, immediately sell at bid 0.49 -> loses spread + 2 fees.
    books = BookStore()
    _market(conn)
    _book(books, "TY", 0.49, 0.51)
    _signal(conn, token="TY")
    s = Settings(); s.paper_hold_s = 0.0
    pt = PaperTrader(conn, books, fees, s)
    pt.process_new_signals()
    pt.mark_and_settle()
    summ = pt.summary()
    assert summ["realized_pnl"] < 0          # honest: costs guarantee a loss
    assert summ["equity"] < 100.0
