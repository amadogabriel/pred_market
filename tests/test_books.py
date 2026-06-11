"""Tests for the in-memory L2 order books."""
from __future__ import annotations

import time

from pm.core.books import Book, BookStore


def _lvl(price, size):
    return {"price": price, "size": size}


def test_apply_snapshot_replaces_state():
    b = Book("X")
    b.apply_snapshot([_lvl(0.40, 100)], [_lvl(0.60, 50)])
    b.apply_snapshot([_lvl(0.30, 10)], [_lvl(0.70, 20)])
    assert b.bids == {0.30: 10.0}
    assert b.asks == {0.70: 20.0}


def test_apply_snapshot_drops_zero_size_levels():
    b = Book("X")
    b.apply_snapshot([_lvl(0.40, 100), _lvl(0.39, 0)], [_lvl(0.60, 0)])
    assert b.bids == {0.40: 100.0}
    assert b.asks == {}


def test_apply_level_size_zero_removes_level():
    b = Book("X")
    b.apply_snapshot([_lvl(0.40, 100)], [])
    b.apply_level("BUY", 0.40, 0)
    assert 0.40 not in b.bids


def test_apply_level_size_positive_updates_level():
    b = Book("X")
    b.apply_level("BUY", 0.40, 100)
    b.apply_level("BUY", 0.40, 250)
    assert b.bids[0.40] == 250.0
    b.apply_level("SELL", 0.60, 75)
    assert b.asks[0.60] == 75.0


def test_best_bid_returns_highest_price():
    b = Book("X")
    b.apply_snapshot([_lvl(0.40, 100), _lvl(0.45, 30), _lvl(0.30, 200)], [])
    assert b.best_bid() == (0.45, 30.0)


def test_best_ask_returns_lowest_price():
    b = Book("X")
    b.apply_snapshot([], [_lvl(0.60, 100), _lvl(0.55, 30), _lvl(0.70, 200)])
    assert b.best_ask() == (0.55, 30.0)


def test_best_bid_ask_none_when_empty():
    b = Book("X")
    assert b.best_bid() is None
    assert b.best_ask() is None


def test_is_stale_after_max_age():
    b = Book("X")
    b.last_update = time.time() - 100
    assert b.is_stale(30) is True
    assert b.is_stale(200) is False


def test_handle_ws_message_routes_book_to_apply_snapshot():
    store = BookStore()
    store.handle_ws_message({
        "event_type": "book", "asset_id": "X",
        "bids": [_lvl(0.40, 100)], "asks": [_lvl(0.60, 50)], "hash": "h1"})
    book = store.peek("X")
    assert book is not None
    assert book.best_bid() == (0.40, 100.0)
    assert book.best_ask() == (0.60, 50.0)
    assert book.seq_hash == "h1"


def test_handle_ws_message_routes_price_change_to_apply_level():
    store = BookStore()
    store.handle_ws_message({
        "event_type": "book", "asset_id": "X",
        "bids": [_lvl(0.40, 100)], "asks": [_lvl(0.60, 50)]})
    store.handle_ws_message({
        "event_type": "price_change", "asset_id": "X",
        "price_changes": [
            {"side": "BUY", "price": 0.40, "size": 0},      # remove
            {"side": "BUY", "price": 0.42, "size": 80},     # add
            {"side": "SELL", "price": 0.60, "size": 25},    # update
        ]})
    book = store.peek("X")
    assert 0.40 not in book.bids
    assert book.best_bid() == (0.42, 80.0)
    assert book.asks[0.60] == 25.0


def test_depth_at_or_better_sums_correctly():
    b = Book("X")
    b.apply_snapshot(
        [_lvl(0.45, 10), _lvl(0.44, 20), _lvl(0.40, 100)],
        [_lvl(0.55, 5), _lvl(0.56, 15), _lvl(0.60, 50)])
    # asks buyable at <= 0.56: 5 + 15
    assert b.depth_at_or_better("ask", 0.56) == 20.0
    # bids sellable at >= 0.44: 10 + 20
    assert b.depth_at_or_better("bid", 0.44) == 30.0
