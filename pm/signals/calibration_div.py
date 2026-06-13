"""S7 calibration-divergence scanner — research signal (never executable).

Runs on a slow cadence (every `calibration_poll_s`, default 600s). For each
tracked active market:

1. Compute the model probability via base_rates + (optional) Metaculus.
2. Compare to the current market mid.
3. If |edge| ≥ threshold and time-to-expiry is sufficient, emit a signal.

This is the slowest of all the scanners (the report rates this strategy as
days–weeks hold period), but it is also the most durable edge per the report:

    "A well-calibrated base-rate model compounds because it is grounded in
     domain knowledge — not technical speed. It cannot be cheaply replicated
     by faster infrastructure."

This implementation is intentionally separate from `scan_task` because it
does not run on every book update — it runs on a fixed cadence over the
universe.

Fail-closed: exec_sets=0, strategy 'calibration' must be explicitly added
to PM_EXECUTION_STRATEGIES.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path

import aiohttp

from pm.calibration.base_rates import first_match, load as load_base_rates
from pm.calibration.model import blend
from pm.calibration.sources import metaculus_search
from pm.core.books import BookStore
from pm.core.bus import Bus
from pm.core.db import beat, log_signal
from pm.core.events import Event, T_SIGNAL
from pm.execution.fee_engine import FeeEngine
from pm.signals.common import Debouncer, mid_price

log = logging.getLogger(__name__)

STRATEGY = "calibration"


async def calibration_div_task(bus: Bus, conn: sqlite3.Connection,
                                books: BookStore, fees: FeeEngine,
                                settings) -> None:
    """Periodic divergence scan over the active universe."""
    rates_path = Path(getattr(settings, "base_rates_yaml", "config/base_rates.yaml"))
    rates = load_base_rates(rates_path)
    if not rates:
        log.info("calibration: %s empty; scanner disabled", rates_path)
        while True:
            beat(conn, "calibration", "disabled")
            await asyncio.sleep(max(60, settings.heartbeat_interval))

    edge_threshold = float(getattr(settings, "calibration_edge_threshold", 0.10))
    min_ttm_s = float(getattr(settings, "calibration_min_ttm_s", 86400.0))
    poll_s = float(getattr(settings, "calibration_poll_s", 600.0))
    use_metaculus = bool(getattr(settings, "calibration_use_metaculus", False))
    debounce = Debouncer(float(getattr(settings, "calibration_debounce_s", 3600.0)))
    stale_after = float(getattr(settings, "stale_book_after", 30.0))
    venue = "polymarket"

    timeout = aiohttp.ClientTimeout(total=10.0)
    log.info("calibration: %d base rates loaded, threshold=%.2f, ttm_min=%.0fs",
             len(rates), edge_threshold, min_ttm_s)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                emitted = await _one_pass(
                    bus, conn, books, fees, rates, session,
                    edge_threshold=edge_threshold, min_ttm_s=min_ttm_s,
                    use_metaculus=use_metaculus, debounce=debounce,
                    stale_after=stale_after, venue=venue)
                beat(conn, "calibration", f"emitted={emitted}")
                await asyncio.sleep(poll_s)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("calibration pass failed; retrying in 60s")
                await asyncio.sleep(60.0)


async def _one_pass(bus, conn, books, fees, rates, session, *,
                    edge_threshold: float, min_ttm_s: float,
                    use_metaculus: bool, debounce: Debouncer,
                    stale_after: float, venue: str) -> int:
    rows = conn.execute(
        "SELECT market_id, question, category, token_yes, end_date "
        "FROM markets WHERE active = 1 AND closed = 0 "
        "AND token_yes IS NOT NULL").fetchall()
    now = time.time()
    emitted = 0
    for row in rows:
        market_id, question, category, token_yes, end_date = row
        if not question:
            continue
        ttm = _seconds_to_end(end_date, now)
        if ttm is not None and ttm < min_ttm_s:
            continue

        mid = mid_price(books.peek(token_yes), stale_after)
        if mid is None or not (0.0 < mid < 1.0):
            continue

        internal = first_match(rates, question, category)
        externals = []
        if use_metaculus and internal is not None:
            ext = await metaculus_search(session, question[:80])
            if ext is not None:
                externals.append(ext)

        mp = blend(internal, externals)
        if mp is None:
            continue
        edge = mp.p - mid
        if abs(edge) < edge_threshold:
            continue
        if not debounce.ready(f"calib:{market_id}", now):
            continue

        side = "BUY" if edge > 0 else "SELL"
        per_share_fee = fees.taker_fee(venue, category, mid, 1.0)
        legs = [{"token_id": token_yes, "market_id": market_id,
                 "side": side, "price": round(mid, 4), "size": 0.0}]
        features = {
            "model_p": mp.p, "market_mid": round(mid, 4),
            "edge": round(edge, 4), "sources": mp.sources,
            "weight_total": mp.weight_total,
            "category": category, "ttm_s": ttm,
            "base_rate_name": internal.name if internal else None,
            "base_rate_p": internal.p if internal else None,
            "external_sources": [e.source for e in externals],
        }
        sid = log_signal(conn, strategy=STRATEGY, kind="model_divergence",
                         group_id=market_id, legs=legs, gross_edge=abs(edge),
                         fees=per_share_fee,
                         net_edge=max(0.0, abs(edge) - per_share_fee),
                         exec_sets=0.0, features=features)
        bus.publish(Event(T_SIGNAL, {"strategy": STRATEGY, "signal_id": sid,
                                     "kind": "model_divergence",
                                     "group_id": market_id,
                                     "net_edge": max(0.0, abs(edge) - per_share_fee),
                                     "exec_sets": 0.0}))
        log.info("calibration %d: market=%s edge=%.3f mid=%.3f model=%.3f",
                 sid, market_id, edge, mid, mp.p)
        emitted += 1
    return emitted


def _seconds_to_end(end_date: str | None, now: float) -> float | None:
    if not end_date:
        return None
    try:
        from datetime import datetime
        ts = datetime.fromisoformat(end_date.replace("Z", "+00:00")).timestamp()
        return max(0.0, ts - now)
    except (TypeError, ValueError):
        return None
