"""S5 whale-follow scanner — research signal (never executable).

When a wallet on the *tracked* list (calibration above the venue baseline,
sufficient sample) takes a position that exceeds the per-wallet baseline
volume multiplier, emit a signal. The scanner reads `system` events of kind
`whale_transfer` published by the CTF listener — same bus, same envelope.

The signal records:
- wallet that took the position
- side (BUY/SELL inferred from net flow into/out of wallet)
- token_id and market_id (if resolvable)
- the wallet's calibration score and recent activity baseline
- the *current* mid at signal time (filled by scan_task before persistence)

Fail-closed: exec_sets=0 by construction; strategy 'whale_follow' must be
explicitly added to PM_EXECUTION_STRATEGIES to ever reach the risk pipeline.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine
from pm.onchain.wallet_tracker import wallet_stat
from pm.signals.common import Debouncer, ResearchSignal, mid_price

log = logging.getLogger(__name__)

STRATEGY = "whale_follow"


@dataclass
class WhaleFollowTracker:
    books: BookStore
    fees: FeeEngine
    conn: sqlite3.Connection
    debounce_s: float = 60.0           # don't double-fire on same wallet+token
    min_calibration: float = 0.55      # below this, ignore the wallet's signal
    min_resolved: int = 5              # below this, calibration is too noisy
    min_value_raw: int = 100_000_000   # ~$100 at 6 decimals; below, too small
    stale_after: float = 30.0
    venue: str = "polymarket"

    def __post_init__(self) -> None:
        self.debounce = Debouncer(self.debounce_s)

    def on_whale_transfer(self, event_payload: dict) -> list[ResearchSignal]:
        """Receive a `whale_transfer` payload from the bus and decide whether to emit."""
        wallet = str(event_payload.get("wallet", "")).lower()
        token_id = str(event_payload.get("token_id", ""))
        market_id = event_payload.get("market_id")
        side = str(event_payload.get("side", "NA")).upper()
        value_raw = int(event_payload.get("value_raw") or 0)
        if not wallet or not token_id or value_raw < self.min_value_raw:
            return []

        stat = wallet_stat(self.conn, wallet)
        if stat is None or not stat.tracked:
            return []
        if stat.n_resolved < self.min_resolved:
            return []
        if stat.calibration < self.min_calibration:
            return []

        mid = mid_price(self.books.peek(token_id), self.stale_after)
        if mid is None or not (0.0 < mid < 1.0):
            return []
        if not self.debounce.ready(f"whale:{wallet}:{token_id}", time.time()):
            return []

        category = self._category_for_market(market_id)
        per_share_fee = self.fees.taker_fee(self.venue, category, mid, 1.0)
        leg = {"token_id": token_id, "market_id": market_id,
               "side": side, "price": round(mid, 4), "size": 0.0}
        return [ResearchSignal(
            strategy=STRATEGY, kind="tracked_wallet_position",
            group_id=str(market_id or token_id), legs=[leg],
            gross_edge=0.0, fees=per_share_fee, net_edge=0.0,
            features={"wallet": wallet, "calibration": round(stat.calibration, 3),
                      "n_resolved": stat.n_resolved,
                      "realized_pnl": round(stat.realized_pnl, 2),
                      "value_raw": value_raw, "mid": round(mid, 4),
                      "category": category,
                      "tx_hash": event_payload.get("tx_hash"),
                      "block": event_payload.get("block")},
        )]

    def _category_for_market(self, market_id: str | None) -> str | None:
        if not market_id:
            return None
        row = self.conn.execute(
            "SELECT category FROM markets WHERE market_id = ?",
            (market_id,)).fetchone()
        return row[0] if row else None
