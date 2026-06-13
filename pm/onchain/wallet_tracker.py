"""Wallet tracker: maintain calibration scores for Polymarket wallets.

Honest framing of edge: as the strategy report puts it,

    "You are not out-modelling professionals — you are reading the same
     public ledger and acting fast. The constraint is always speed."

We score each wallet by historical positions on *resolved* markets — did they
buy YES on outcomes that turned out YES? A wallet with a sustained better-than-
baseline calibration on resolved markets is interesting to follow on its open
positions.

This module is the *store*. It does not fetch chain data — that's
`ctf_listener.py`. It does not emit signals — that's `pm/signals/whale_follow.py`.

Schema (all in `data/state.db`):

    whale_wallets         track-record metrics per wallet
    whale_positions       recent observed transfers we are still scoring
    whale_resolved        labeled outcomes per wallet position

The wallet tagger has two free parameters: `min_trades_for_score` (don't tag
on noise) and `baseline_calibration` (the venue-wide hit rate; anything below
isn't useful to follow).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

WALLET_SCHEMA = """
CREATE TABLE IF NOT EXISTS whale_wallets (
    wallet        TEXT PRIMARY KEY,
    label         TEXT,                  -- optional human note ('hedge_fund_x')
    n_trades      INTEGER DEFAULT 0,
    n_resolved    INTEGER DEFAULT 0,
    n_correct     INTEGER DEFAULT 0,
    realized_pnl  REAL DEFAULT 0.0,
    calibration   REAL DEFAULT 0.0,      -- n_correct / n_resolved
    first_seen    REAL,
    last_seen     REAL,
    tracked       INTEGER DEFAULT 0      -- 1 = currently in the follow list
);
CREATE INDEX IF NOT EXISTS idx_whale_wallets_tracked
    ON whale_wallets(tracked, calibration);

CREATE TABLE IF NOT EXISTS whale_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet        TEXT NOT NULL,
    token_id      TEXT NOT NULL,
    market_id     TEXT,
    side          TEXT,                  -- BUY | SELL inferred from net flow
    value_raw     INTEGER,               -- ERC-1155 raw amount
    tx_hash       TEXT,
    block_number  INTEGER,
    ts            REAL,                  -- wallclock when ingested
    outcome       REAL,                  -- nullable, set on resolution
    pnl           REAL                   -- nullable
);
CREATE INDEX IF NOT EXISTS idx_whale_positions_wallet
    ON whale_positions(wallet, ts DESC);
CREATE INDEX IF NOT EXISTS idx_whale_positions_token
    ON whale_positions(token_id, ts DESC);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(WALLET_SCHEMA)


@dataclass
class WalletStat:
    wallet: str
    n_trades: int
    n_resolved: int
    n_correct: int
    calibration: float
    realized_pnl: float
    tracked: bool


def upsert_position(conn: sqlite3.Connection, *, wallet: str, token_id: str,
                    market_id: str | None, side: str, value_raw: int,
                    tx_hash: str, block_number: int) -> int:
    """Insert a new observed position. Returns the row id."""
    wallet = wallet.lower()
    now = time.time()
    cur = conn.execute(
        "INSERT INTO whale_positions (wallet, token_id, market_id, side, "
        " value_raw, tx_hash, block_number, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (wallet, token_id, market_id, side, value_raw, tx_hash,
         block_number, now))
    conn.execute(
        "INSERT INTO whale_wallets (wallet, n_trades, first_seen, last_seen) "
        "VALUES (?, 1, ?, ?) "
        "ON CONFLICT(wallet) DO UPDATE SET n_trades = n_trades + 1, "
        " last_seen = excluded.last_seen",
        (wallet, now, now))
    return int(cur.lastrowid or 0)


def track_wallet(conn: sqlite3.Connection, wallet: str, *,
                 label: str | None = None, tracked: bool = True) -> None:
    """Add a wallet to the follow list (or remove with tracked=False)."""
    wallet = wallet.lower()
    conn.execute(
        "INSERT INTO whale_wallets (wallet, label, tracked, first_seen, last_seen) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(wallet) DO UPDATE SET label = COALESCE(excluded.label, label), "
        " tracked = excluded.tracked",
        (wallet, label, 1 if tracked else 0, time.time(), time.time()))


def tracked_wallets(conn: sqlite3.Connection, *,
                    min_calibration: float = 0.0) -> list[str]:
    """Return wallets currently in the follow list with calibration above floor."""
    rows = conn.execute(
        "SELECT wallet FROM whale_wallets "
        "WHERE tracked = 1 AND calibration >= ? "
        "ORDER BY calibration DESC", (min_calibration,)).fetchall()
    return [r[0] for r in rows]


def record_resolution(conn: sqlite3.Connection, position_id: int,
                      outcome: float, pnl: float) -> None:
    """Fill in outcome+pnl for a resolved position and update wallet aggregate."""
    cur = conn.execute(
        "UPDATE whale_positions SET outcome = ?, pnl = ? "
        "WHERE id = ? AND outcome IS NULL", (outcome, pnl, position_id))
    if cur.rowcount == 0:
        return
    row = conn.execute(
        "SELECT wallet FROM whale_positions WHERE id = ?", (position_id,)).fetchone()
    if row is None:
        return
    wallet = row[0]
    correct = 1 if outcome > 0 else 0
    conn.execute(
        "UPDATE whale_wallets SET n_resolved = n_resolved + 1, "
        " n_correct = n_correct + ?, realized_pnl = realized_pnl + ?, "
        " calibration = CASE WHEN n_resolved + 1 > 0 "
        "                    THEN CAST(n_correct + ? AS REAL) / (n_resolved + 1) "
        "                    ELSE 0.0 END "
        "WHERE wallet = ?",
        (correct, pnl, correct, wallet))


def wallet_stat(conn: sqlite3.Connection, wallet: str) -> WalletStat | None:
    wallet = wallet.lower()
    row = conn.execute(
        "SELECT wallet, n_trades, n_resolved, n_correct, calibration, "
        " realized_pnl, tracked FROM whale_wallets WHERE wallet = ?",
        (wallet,)).fetchone()
    if not row:
        return None
    return WalletStat(*row[:6], tracked=bool(row[6]))


def recent_positions(conn: sqlite3.Connection, wallet: str, *,
                     limit: int = 20) -> list[dict]:
    rows = conn.execute(
        "SELECT id, token_id, market_id, side, value_raw, tx_hash, "
        " block_number, ts, outcome, pnl "
        "FROM whale_positions WHERE wallet = ? ORDER BY ts DESC LIMIT ?",
        (wallet, limit)).fetchall()
    return [dict(zip(
        ("id", "token_id", "market_id", "side", "value_raw", "tx_hash",
         "block_number", "ts", "outcome", "pnl"), r)) for r in rows]
