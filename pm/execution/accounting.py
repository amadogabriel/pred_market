"""Position accounting helpers.

This module updates inventory from fills. It is intentionally venue-neutral and
does not infer settlement; realized PnL is only recognized when a SELL reduces
an existing long position.
"""
from __future__ import annotations

import sqlite3
import time


def apply_fill(conn: sqlite3.Connection, *, venue: str, token_id: str,
               market_id: str | None, side: str, price: float, size: float,
               fee: float = 0.0) -> None:
    side = side.upper()
    row = conn.execute(
        "SELECT size, avg_price, realized_pnl FROM positions WHERE venue=? AND token_id=?",
        (venue, token_id)).fetchone()
    old_size = float(row["size"]) if row else 0.0
    old_avg = float(row["avg_price"]) if row else 0.0
    realized = float(row["realized_pnl"]) if row else 0.0

    if side == "BUY":
        new_size = old_size + size
        new_avg = ((old_size * old_avg) + (size * price) + fee) / new_size if new_size else 0.0
    elif side == "SELL":
        sell_size = min(size, max(old_size, 0.0))
        realized += sell_size * (price - old_avg) - fee
        new_size = old_size - size
        new_avg = old_avg if new_size > 0 else 0.0
    else:
        raise ValueError(f"unknown side {side!r}")

    conn.execute(
        "INSERT INTO positions (venue, token_id, market_id, size, avg_price, realized_pnl, updated_at) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(venue, token_id) DO UPDATE SET "
        "market_id=excluded.market_id, size=excluded.size, avg_price=excluded.avg_price, "
        "realized_pnl=excluded.realized_pnl, updated_at=excluded.updated_at",
        (venue, token_id, market_id, new_size, new_avg, realized, time.time()))
