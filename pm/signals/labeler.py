"""Signal outcome labeler — fills signal_log.outcome / .pnl with forward returns.

signal_log was always meant to be the meta-label training dataset, but nothing
wrote the outcome column. This task does: `label_horizon_s` seconds after a
signal fires, it computes the realized forward move of each leg against the
then-current mid and writes the average per-share edge to `outcome` and
outcome × exec_sets to `pnl`.

Per-leg forward edge, signed so positive = the signal's direction was right:
    BUY  leg: mid_now − leg_price   (we said it was cheap; did it rise?)
    SELL leg: leg_price − mid_now
    NA   leg: mid_now − leg_price   (research drift signals: raw move; the
              direction feature in features_json gives sign context)

A signal is labeled only when at least half its legs have live, fresh books at
labeling time; otherwise it is retried on later passes. Signals older than
`label_max_age_s` fall out of the query window and stay NULL — that itself is
information (book died before horizon: market resolved or delisted).

Every result this produces is descriptive, not a promise: outcome measures mid
drift over a fixed horizon, ignoring spread crossing and impact.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from pm.core.books import BookStore
from pm.core.db import beat
from pm.signals.common import mid_price

log = logging.getLogger(__name__)


def label_once(conn, books: BookStore, settings, *, now: float | None = None) -> int:
    """Label one batch of mature, unlabeled signals. Returns rows labeled."""
    now = time.time() if now is None else now
    rows = conn.execute(
        "SELECT signal_id, legs_json, exec_sets FROM signal_log "
        "WHERE outcome IS NULL AND ts < ? AND ts > ? "
        "ORDER BY ts LIMIT ?",
        (now - settings.label_horizon_s, now - settings.label_max_age_s,
         settings.label_batch)).fetchall()

    labeled = 0
    for row in rows:
        try:
            legs = json.loads(row["legs_json"] or "[]")
        except (ValueError, TypeError):
            legs = []
        if not legs:
            continue

        per_leg: list[float] = []
        for leg in legs:
            mid = mid_price(books.peek(str(leg.get("token_id"))), settings.stale_book_after)
            if mid is None:
                continue
            try:
                price = float(leg["price"])
            except (KeyError, TypeError, ValueError):
                continue
            side = str(leg.get("side", "NA")).upper()
            per_leg.append(price - mid if side == "SELL" else mid - price)

        if len(per_leg) * 2 < len(legs):
            continue  # not enough live books yet; retry next pass

        outcome = sum(per_leg) / len(per_leg)
        pnl = outcome * float(row["exec_sets"] or 0.0)
        conn.execute(
            "UPDATE signal_log SET outcome=?, pnl=? WHERE signal_id=?",
            (round(outcome, 5), round(pnl, 4), row["signal_id"]))
        labeled += 1
    return labeled


async def labeler_task(conn, books: BookStore, settings) -> None:
    """Run label_once periodically; beat every pass."""
    backoff = 5.0
    while True:
        try:
            n = label_once(conn, books, settings)
            beat(conn, "labeler", f"labeled={n}")
            if n:
                log.info("labeler: labeled %d signals", n)
            backoff = 5.0
            await asyncio.sleep(settings.label_poll_s)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("labeler pass failed; retrying in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)
