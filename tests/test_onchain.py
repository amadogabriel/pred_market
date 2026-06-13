"""Tests for the on-chain wallet tracker + CTF event decoders."""
from __future__ import annotations

import sqlite3

import pytest

from pm.onchain.ctf_events import (TOPIC_TRANSFER_BATCH, TOPIC_TRANSFER_SINGLE,
                                    decode_batch, decode_single)
from pm.onchain.polygon_rpc import (LogFilter, decode_address, decode_uint,
                                     topic_address)
from pm.onchain.wallet_tracker import (ensure_schema, record_resolution,
                                        recent_positions, track_wallet,
                                        tracked_wallets, upsert_position,
                                        wallet_stat)


# ---------- polygon_rpc utilities ----------

def test_topic_address_round_trip():
    addr = "0xAbCdEf0123456789aBcdEf0123456789ABCDEF01"
    t = topic_address(addr)
    assert len(t) == 66
    assert t.startswith("0x" + "0" * 24)
    assert decode_address(t) == addr.lower()


def test_topic_address_rejects_bad_input():
    with pytest.raises(ValueError):
        topic_address("0x1234")


def test_decode_uint_handles_prefix():
    assert decode_uint("0x10") == 16
    assert decode_uint("10") == 16
    assert decode_uint("0x") == 0


def test_log_filter_to_params():
    f = LogFilter(from_block=100, to_block=200, address="0xabc",
                  topics=["0xdeadbeef"])
    p = f.to_params()
    assert p["fromBlock"] == "0x64"
    assert p["toBlock"] == "0xc8"
    assert p["address"] == "0xabc"
    assert p["topics"] == ["0xdeadbeef"]


# ---------- CTF event decoders ----------

ZERO = "0x" + "0" * 40
ALICE = "0x" + "a" * 40
BOB = "0x" + "b" * 40
OP = "0x" + "c" * 40


def _topic_addr(a: str) -> str:
    return "0x" + "0" * 24 + a.lower().removeprefix("0x")


def _pad(x: int, n: int = 64) -> str:
    return format(x, "x").rjust(n, "0")


def test_decode_single_basic_trade():
    # TransferSingle(operator, from=alice, to=bob, id=42, value=1_000_000)
    log = {
        "topics": [TOPIC_TRANSFER_SINGLE, _topic_addr(OP), _topic_addr(ALICE),
                   _topic_addr(BOB)],
        "data": "0x" + _pad(0x2a) + _pad(0xf4240),
        "blockNumber": "0x10",
        "transactionHash": "0xdeadbeef",
    }
    t = decode_single(log)
    assert t is not None
    assert t.from_addr == ALICE
    assert t.to_addr == BOB
    assert t.token_id == 0x2a
    assert t.value == 0xf4240
    assert t.is_trade is True
    assert t.is_mint is False


def test_decode_single_detects_mint():
    log = {
        "topics": [TOPIC_TRANSFER_SINGLE, _topic_addr(OP), _topic_addr(ZERO),
                   _topic_addr(BOB)],
        "data": "0x" + _pad(1) + _pad(0xff),
        "blockNumber": "0x1",
        "transactionHash": "0xabc",
    }
    t = decode_single(log)
    assert t is not None and t.is_mint is True


def test_decode_single_wrong_topic_returns_none():
    log = {"topics": ["0xdeadbeef"], "data": "0x", "blockNumber": "0x1"}
    assert decode_single(log) is None


def test_decode_batch_two_ids():
    # operator, from, to, then data layout:
    #   offset_to_ids=64, offset_to_values=128+32=224 (NB: simplified)
    # Easier path: build the bytes deterministically.
    def pad(x: int, n: int = 64) -> str:
        return format(x, "x").rjust(n, "0")
    raw = ""
    raw += pad(0x40)            # offset to ids (in bytes from start of data) = 64
    raw += pad(0xa0)            # offset to values = 160
    raw += pad(2)               # ids length
    raw += pad(1)               # ids[0]
    raw += pad(2)               # ids[1]
    raw += pad(2)               # values length
    raw += pad(10)              # values[0]
    raw += pad(20)              # values[1]
    log = {
        "topics": [TOPIC_TRANSFER_BATCH, _topic_addr(OP), _topic_addr(ALICE),
                   _topic_addr(BOB)],
        "data": "0x" + raw,
        "blockNumber": "0x5",
        "transactionHash": "0xfeed",
    }
    items = decode_batch(log)
    assert len(items) == 2
    assert items[0].token_id == 1 and items[0].value == 10
    assert items[1].token_id == 2 and items[1].value == 20


# ---------- wallet tracker ----------

@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    ensure_schema(c)
    yield c
    c.close()


def test_track_wallet_persists(conn):
    track_wallet(conn, "0xabc", label="hf_alpha")
    assert "0xabc" in tracked_wallets(conn)


def test_upsert_position_increments_counters(conn):
    track_wallet(conn, "0xabc")
    pid = upsert_position(conn, wallet="0xabc", token_id="42",
                          market_id="M1", side="BUY", value_raw=10**8,
                          tx_hash="0xdead", block_number=100)
    assert pid > 0
    rec = recent_positions(conn, "0xabc")
    assert len(rec) == 1
    assert rec[0]["token_id"] == "42"
    stat = wallet_stat(conn, "0xabc")
    assert stat is not None and stat.n_trades == 1


def test_record_resolution_updates_calibration(conn):
    track_wallet(conn, "0xabc")
    pid = upsert_position(conn, wallet="0xabc", token_id="42",
                          market_id="M1", side="BUY", value_raw=10**8,
                          tx_hash="0xdead", block_number=100)
    record_resolution(conn, pid, outcome=0.05, pnl=5.0)
    stat = wallet_stat(conn, "0xabc")
    assert stat.n_resolved == 1 and stat.n_correct == 1
    assert abs(stat.calibration - 1.0) < 1e-6


def test_record_resolution_handles_loss(conn):
    track_wallet(conn, "0xabc")
    pid = upsert_position(conn, wallet="0xabc", token_id="42",
                          market_id="M1", side="BUY", value_raw=10**8,
                          tx_hash="0xdead", block_number=100)
    record_resolution(conn, pid, outcome=-0.03, pnl=-3.0)
    stat = wallet_stat(conn, "0xabc")
    assert stat.n_resolved == 1 and stat.n_correct == 0
    assert stat.calibration == 0.0


def test_tracked_wallets_filters_by_calibration(conn):
    track_wallet(conn, "0xabc")
    pid = upsert_position(conn, wallet="0xabc", token_id="1", market_id="M1",
                          side="BUY", value_raw=1, tx_hash="0x", block_number=1)
    record_resolution(conn, pid, outcome=0.05, pnl=5.0)
    # 1/1 = 1.0 calibration
    assert "0xabc" in tracked_wallets(conn, min_calibration=0.9)
    # tighten filter to require > 1.0 — nothing should match
    assert "0xabc" not in tracked_wallets(conn, min_calibration=1.01)
