"""Honest paper-trading account — simulated money, real frictions.

This is a self-contained simulation. It NEVER places an order, never touches a
broker, and is fully separate from the real execution path (execution_intents
/ execution_fills / positions and the PM_EXECUTION_* gates). It exists so you
can watch the strategies trade a fixed bankroll on paper.

"Honest" means the simulation pays the costs a real taker pays, so the P&L is
truthful rather than flattering:

  - BUY fills cross the spread at the best ASK; exits sell at the best BID.
    (A SELL view on a YES token is expressed by BUYing the NO token, the way
     you actually would on a binary CLOB — no fictional shorting.)
  - Every entry and exit pays the venue taker fee from fee_engine.
  - Open positions are marked at the mid; realised P&L uses the touch.
  - A position is closed when its market resolves (settle at the $0/$1 payout)
    or after a hold horizon (sell at the bid) so the bankroll recycles.

Expect this to lose money slowly on the no-edge signals — that is the point:
crossing a ~1c spread and paying a fee on a signal worth <0.1c is a guaranteed
bleed, and seeing it is more convincing than any table.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from pm.core.books import BookStore
from pm.core.db import beat
from pm.execution.fee_engine import FeeEngine
from pm.signals.common import mid_price

log = logging.getLogger(__name__)

PAPER_SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_account (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    bankroll      REAL,
    cash          REAL,
    realized_pnl  REAL DEFAULT 0.0,
    n_trades      INTEGER DEFAULT 0,
    last_signal_id INTEGER DEFAULT 0,
    started_at    REAL,
    updated_at    REAL
);
CREATE TABLE IF NOT EXISTS paper_trades (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id    INTEGER,
    strategy     TEXT,
    kind         TEXT,
    market_id    TEXT,
    token_id     TEXT,          -- the token actually held (complement for SELL views)
    view_side    TEXT,          -- the signal's directional view (BUY/SELL)
    entry_ts     REAL,
    entry_price  REAL,
    shares       REAL,
    cost         REAL,          -- shares*entry_price + entry_fee
    entry_fee    REAL,
    status       TEXT,          -- open | closed | settled
    exit_ts      REAL,
    exit_price   REAL,
    exit_fee     REAL,
    pnl          REAL
);
CREATE INDEX IF NOT EXISTS idx_paper_trades_status ON paper_trades(status);
"""


def ensure_schema(conn) -> None:
    conn.executescript(PAPER_SCHEMA)


def init_account(conn, bankroll: float) -> None:
    row = conn.execute("SELECT id FROM paper_account WHERE id=1").fetchone()
    if row is None:
        now = time.time()
        conn.execute(
            "INSERT INTO paper_account (id, bankroll, cash, realized_pnl, "
            "n_trades, last_signal_id, started_at, updated_at) "
            "VALUES (1, ?, ?, 0.0, 0, 0, ?, ?)", (bankroll, bankroll, now, now))


def _account(conn) -> dict:
    r = conn.execute("SELECT * FROM paper_account WHERE id=1").fetchone()
    return dict(r) if r else {}


def _market_meta(conn, market_id: str | None, token_id: str | None) -> dict:
    """Resolve category + both tokens + resolution payout for a market."""
    row = None
    if market_id:
        row = conn.execute(
            "SELECT market_id, category, token_yes, token_no, closed, "
            "outcome_prices_json FROM markets WHERE market_id=?",
            (market_id,)).fetchone()
    if row is None and token_id:
        row = conn.execute(
            "SELECT market_id, category, token_yes, token_no, closed, "
            "outcome_prices_json FROM markets "
            "WHERE token_yes=? OR token_no=?", (token_id, token_id)).fetchone()
    return dict(row) if row else {}


def _payout_for_token(meta: dict, token_id: str) -> float | None:
    """Return 0.0/1.0 if the market resolved, else None."""
    if not meta or not meta.get("closed") or not meta.get("outcome_prices_json"):
        return None
    try:
        prices = json.loads(meta["outcome_prices_json"])
        yes_p, no_p = float(prices[0]), float(prices[1])
    except (TypeError, ValueError, IndexError, json.JSONDecodeError):
        return None
    if token_id == meta.get("token_yes"):
        return yes_p
    if token_id == meta.get("token_no"):
        return no_p
    return None


class PaperTrader:
    def __init__(self, conn, books: BookStore, fees: FeeEngine, settings):
        self.conn = conn
        self.books = books
        self.fees = fees
        self.s = settings
        self.bankroll = float(getattr(settings, "paper_bankroll", 100.0))
        self.per_trade = float(getattr(settings, "paper_per_trade", 10.0))
        self.hold_s = float(getattr(settings, "paper_hold_s", 1800.0))
        self.stale_after = float(getattr(settings, "stale_book_after", 30.0))
        self.venue = "polymarket"
        self.allow = frozenset(
            x.strip() for x in str(getattr(settings, "paper_strategies",
            "struct_arb,whale_follow,news,microstructure,rel_value,momentum")).split(",")
            if x.strip())
        ensure_schema(conn)
        init_account(conn, self.bankroll)

    # ---------- open ----------
    def process_new_signals(self) -> int:
        acct = _account(self.conn)
        last_id = int(acct.get("last_signal_id", 0) or 0)
        rows = self.conn.execute(
            "SELECT signal_id, strategy, kind, legs_json FROM signal_log "
            "WHERE signal_id > ? AND strategy IN (%s) ORDER BY signal_id LIMIT 200"
            % ",".join("?" * len(self.allow)),
            (last_id, *sorted(self.allow))).fetchall() if self.allow else []
        opened = 0
        max_id = last_id
        for r in rows:
            max_id = max(max_id, int(r["signal_id"]))
            if self._try_open(r):
                opened += 1
        if max_id != last_id:
            self.conn.execute(
                "UPDATE paper_account SET last_signal_id=?, updated_at=? WHERE id=1",
                (max_id, time.time()))
        return opened

    def _try_open(self, sig) -> bool:
        cash = float(_account(self.conn).get("cash", 0.0))
        if cash < self.per_trade:
            return False
        try:
            legs = json.loads(sig["legs_json"] or "[]")
        except (TypeError, ValueError):
            return False
        if not legs:
            return False
        leg = legs[0]
        view_side = str(leg.get("side", "")).upper()
        if view_side not in ("BUY", "SELL"):
            return False
        market_id = leg.get("market_id")
        leg_token = str(leg.get("token_id"))
        meta = _market_meta(self.conn, market_id, leg_token)
        # Don't open on already-resolved markets.
        if _payout_for_token(meta, leg_token) is not None:
            return False

        # Which token do we actually buy? BUY view -> the leg token.
        # SELL view -> buy the complement (the other side of the same market).
        if view_side == "BUY":
            hold_token = leg_token
        else:
            ty, tn = meta.get("token_yes"), meta.get("token_no")
            hold_token = tn if leg_token == ty else (ty if leg_token == tn else None)
            if not hold_token:
                return False  # can't express the short cleanly; skip

        book = self.books.peek(hold_token)
        if book is None or book.is_stale(self.stale_after):
            return False
        ask = book.best_ask()
        if ask is None or not (0.0 < ask[0] < 1.0):
            return False
        # Already holding this token? one position per token at a time.
        dup = self.conn.execute(
            "SELECT 1 FROM paper_trades WHERE token_id=? AND status='open'",
            (hold_token,)).fetchone()
        if dup:
            return False

        price = ask[0]
        budget = min(self.per_trade, cash)
        fee = self.fees.taker_fee(self.venue, meta.get("category"), price, budget / price)
        shares = (budget - fee) / price
        if shares <= 0:
            return False
        cost = shares * price + fee
        now = time.time()
        self.conn.execute(
            "INSERT INTO paper_trades (signal_id, strategy, kind, market_id, "
            "token_id, view_side, entry_ts, entry_price, shares, cost, entry_fee, "
            "status) VALUES (?,?,?,?,?,?,?,?,?,?,?, 'open')",
            (sig["signal_id"], sig["strategy"], sig["kind"], market_id,
             hold_token, view_side, now, round(price, 4), round(shares, 4),
             round(cost, 4), round(fee, 4)))
        self.conn.execute(
            "UPDATE paper_account SET cash = cash - ?, n_trades = n_trades + 1, "
            "updated_at=? WHERE id=1", (cost, now))
        return True

    # ---------- mark / settle / close ----------
    def mark_and_settle(self) -> tuple[int, int]:
        now = time.time()
        settled = closed = 0
        for t in self.conn.execute(
                "SELECT * FROM paper_trades WHERE status='open'").fetchall():
            meta = _market_meta(self.conn, t["market_id"], t["token_id"])
            payout = _payout_for_token(meta, t["token_id"])
            if payout is not None:
                # market resolved -> settle at payout, no exit fee
                proceeds = t["shares"] * payout
                self._close(t, exit_price=payout, exit_fee=0.0,
                            proceeds=proceeds, status="settled", now=now)
                settled += 1
                continue
            if now - t["entry_ts"] >= self.hold_s:
                book = self.books.peek(t["token_id"])
                bid = book.best_bid() if book and not book.is_stale(self.stale_after) else None
                if bid is None:
                    continue  # can't exit yet; try next pass
                price = bid[0]
                fee = self.fees.taker_fee(self.venue, meta.get("category"),
                                          price, t["shares"])
                proceeds = t["shares"] * price - fee
                self._close(t, exit_price=round(price, 4), exit_fee=round(fee, 4),
                            proceeds=proceeds, status="closed", now=now)
                closed += 1
        return settled, closed

    def _close(self, t, *, exit_price, exit_fee, proceeds, status, now) -> None:
        pnl = proceeds - t["cost"]
        self.conn.execute(
            "UPDATE paper_trades SET status=?, exit_ts=?, exit_price=?, "
            "exit_fee=?, pnl=? WHERE id=?",
            (status, now, exit_price, exit_fee, round(pnl, 4), t["id"]))
        self.conn.execute(
            "UPDATE paper_account SET cash = cash + ?, realized_pnl = realized_pnl + ?, "
            "updated_at=? WHERE id=1", (proceeds, pnl, now))

    # ---------- reporting ----------
    def summary(self) -> dict:
        acct = _account(self.conn)
        cash = float(acct.get("cash", 0.0))
        realized = float(acct.get("realized_pnl", 0.0))
        open_value = 0.0
        open_cost = 0.0
        n_open = 0
        for t in self.conn.execute(
                "SELECT token_id, shares, cost FROM paper_trades WHERE status='open'"):
            m = mid_price(self.books.peek(t["token_id"]), self.stale_after)
            if m is not None:
                open_value += t["shares"] * m
                open_cost += t["cost"]
                n_open += 1
        equity = cash + open_value
        closed = self.conn.execute(
            "SELECT COUNT(*) n, SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins "
            "FROM paper_trades WHERE status IN ('closed','settled')").fetchone()
        n_closed = int(closed["n"] or 0)
        wins = int(closed["wins"] or 0)
        per_strat = [dict(r) for r in self.conn.execute(
            "SELECT strategy, COUNT(*) trades, "
            "COALESCE(SUM(pnl),0) pnl FROM paper_trades "
            "WHERE status IN ('closed','settled') GROUP BY strategy ORDER BY pnl")]
        return {
            "bankroll": round(self.bankroll, 2),
            "cash": round(cash, 2),
            "open_value": round(open_value, 2),
            "equity": round(equity, 2),
            "realized_pnl": round(realized, 2),
            "unrealized_pnl": round(open_value - open_cost, 2),
            "return_pct": round((equity / self.bankroll - 1.0) * 100.0, 2),
            "n_open": n_open,
            "n_closed": n_closed,
            "n_trades": int(acct.get("n_trades", 0) or 0),
            "win_rate": round(wins / n_closed, 3) if n_closed else None,
            "per_strategy": per_strat,
        }


async def paper_trader_task(conn, books: BookStore, fees: FeeEngine, settings) -> None:
    """Open paper trades from new signals; mark/settle open ones on each pass."""
    if not bool(getattr(settings, "paper_enabled", True)):
        while True:
            beat(conn, "paper_trader", "disabled")
            await asyncio.sleep(max(60, settings.heartbeat_interval))

    trader = PaperTrader(conn, books, fees, settings)
    poll_s = float(getattr(settings, "paper_poll_s", 20.0))
    log.info("paper_trader: $%.0f bankroll, $%.0f/trade, strategies=%s",
             trader.bankroll, trader.per_trade, sorted(trader.allow))
    while True:
        try:
            settled, closed = trader.mark_and_settle()
            opened = trader.process_new_signals()
            summ = trader.summary()
            beat(conn, "paper_trader",
                 f"equity=${summ['equity']:.2f} ({summ['return_pct']:+.1f}%); "
                 f"open={summ['n_open']}; trades={summ['n_trades']}")
            if opened or settled or closed:
                log.info("paper_trader: +%d opened, %d settled, %d closed; "
                         "equity=$%.2f", opened, settled, closed, summ["equity"])
            await asyncio.sleep(poll_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("paper_trader pass failed; retrying in 30s")
            await asyncio.sleep(30.0)
