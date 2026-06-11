"""Operational state store: SQLite in WAL mode.

Holds slowly-changing state (markets, rules, groups, signals, heartbeats).
High-rate market data does NOT go here — it goes to the append-only event
log on disk. SQLite is for things you query; the log is for things you replay.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    market_id     TEXT PRIMARY KEY,   -- condition_id
    venue         TEXT NOT NULL DEFAULT 'polymarket',
    question      TEXT,
    slug          TEXT,
    category      TEXT,               -- normalized for the fee engine
    tags_json     TEXT,
    end_date      TEXT,
    active        INTEGER,
    closed        INTEGER,
    neg_risk      INTEGER DEFAULT 0,
    neg_risk_id   TEXT,               -- groups mutually exclusive outcomes
    token_yes     TEXT,
    token_no      TEXT,
    liquidity     REAL,
    volume_24h    REAL,
    updated_at    REAL
);
CREATE INDEX IF NOT EXISTS idx_markets_negrisk ON markets(neg_risk_id);
CREATE INDEX IF NOT EXISTS idx_markets_active  ON markets(active, closed);

CREATE TABLE IF NOT EXISTS rules_text (
    market_id   TEXT NOT NULL,
    venue       TEXT NOT NULL,
    rules_md    TEXT,
    hash        TEXT,
    fetched_at  REAL,
    PRIMARY KEY (market_id, venue, hash)        -- keep every version we ever saw
);

CREATE TABLE IF NOT EXISTS signal_log (
    signal_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy    TEXT NOT NULL,
    kind        TEXT NOT NULL,        -- e.g. partition_buy_all / complement / ...
    group_id    TEXT,                 -- neg_risk_id or market_id
    legs_json   TEXT NOT NULL,        -- [{token_id, market_id, side, price, size}, ...]
    gross_edge  REAL NOT NULL,        -- per $1 set, before fees
    fees        REAL NOT NULL,        -- per set
    net_edge    REAL NOT NULL,        -- per set, after fees and buffer check
    exec_sets   REAL NOT NULL,        -- executable sets at quoted depth
    features_json TEXT,               -- meta-label training features
    ts          REAL NOT NULL,
    acted       INTEGER DEFAULT 0,
    outcome     REAL,                 -- filled in at resolution / unwind
    pnl         REAL
);
CREATE INDEX IF NOT EXISTS idx_signal_strategy_ts ON signal_log(strategy, ts);

CREATE TABLE IF NOT EXISTS heartbeats (
    component   TEXT PRIMARY KEY,
    ts          REAL NOT NULL,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS recon_log (
    ts          REAL NOT NULL,
    token_id    TEXT NOT NULL,
    field       TEXT NOT NULL,        -- best_bid / best_ask
    ws_value    REAL,
    rest_value  REAL,
    diff        REAL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.executescript(SCHEMA)
    return conn


# ---------- helpers ----------

def upsert_market(conn: sqlite3.Connection, m: dict[str, Any]) -> None:
    cols = ("market_id venue question slug category tags_json end_date active closed "
            "neg_risk neg_risk_id token_yes token_no liquidity volume_24h updated_at").split()
    m = {**m, "updated_at": time.time()}
    placeholders = ",".join(":" + c for c in cols)
    conn.execute(
        f"INSERT INTO markets ({','.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(market_id) DO UPDATE SET " +
        ",".join(f"{c}=excluded.{c}" for c in cols if c != "market_id"),
        {c: m.get(c) for c in cols},
    )


def store_rules(conn: sqlite3.Connection, market_id: str, venue: str, rules_md: str) -> bool:
    """Returns True if this is a NEW version of the rules (changed text -> alert-worthy)."""
    h = hashlib.sha256((rules_md or "").encode()).hexdigest()
    cur = conn.execute(
        "SELECT 1 FROM rules_text WHERE market_id=? AND venue=? AND hash=?",
        (market_id, venue, h))
    if cur.fetchone():
        return False
    prior = conn.execute(
        "SELECT COUNT(*) c FROM rules_text WHERE market_id=? AND venue=?",
        (market_id, venue)).fetchone()["c"]
    conn.execute(
        "INSERT INTO rules_text (market_id, venue, rules_md, hash, fetched_at) VALUES (?,?,?,?,?)",
        (market_id, venue, rules_md, h, time.time()))
    return prior > 0  # changed (not first sighting)


def log_signal(conn: sqlite3.Connection, *, strategy: str, kind: str, group_id: str,
               legs: Iterable[dict], gross_edge: float, fees: float, net_edge: float,
               exec_sets: float, features: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO signal_log (strategy, kind, group_id, legs_json, gross_edge, fees, "
        "net_edge, exec_sets, features_json, ts) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (strategy, kind, group_id, json.dumps(list(legs)), gross_edge, fees,
         net_edge, exec_sets, json.dumps(features or {}), time.time()))
    return int(cur.lastrowid)


def beat(conn: sqlite3.Connection, component: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO heartbeats (component, ts, detail) VALUES (?,?,?) "
        "ON CONFLICT(component) DO UPDATE SET ts=excluded.ts, detail=excluded.detail",
        (component, time.time(), detail))


def stale_components(conn: sqlite3.Connection, max_age: float) -> list[tuple[str, float]]:
    now = time.time()
    rows = conn.execute("SELECT component, ts FROM heartbeats").fetchall()
    return [(r["component"], now - r["ts"]) for r in rows if now - r["ts"] > max_age]
