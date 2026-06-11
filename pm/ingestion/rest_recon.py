"""WS-vs-REST reconciliation.

Periodically samples tracked tokens, fetches their current book from the CLOB
REST endpoint, and compares the REST top-of-book to the in-memory WS book.
Diffs are logged to `recon_log`; large diffs (> 2 cents) also publish a
`recon_drift` system event. This flags drift — it never auto-corrects the WS
book (the WS feed self-heals on reconnect via fresh snapshots).

If diffs are consistently large under normal conditions, the WS parsing has a
bug — fix `ws_polymarket._handle_raw` or `books.handle_ws_message`, not this.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import aiohttp

from pm.core.books import BookStore
from pm.core.bus import Bus
from pm.core.db import beat
from pm.core.events import Event, T_SYSTEM

log = logging.getLogger(__name__)

DRIFT_THRESHOLD = 0.02  # 2 cents
SAMPLE_SIZE = 20


def _tracked_tokens(conn, settings) -> list[str]:
    rows = conn.execute(
        "SELECT token_yes, token_no FROM markets "
        "WHERE active=1 AND closed=0 AND COALESCE(liquidity,0) >= ?",
        (settings.min_liquidity_usd,)).fetchall()
    tokens: list[str] = []
    for r in rows:
        if r["token_yes"]:
            tokens.append(r["token_yes"])
        if r["token_no"]:
            tokens.append(r["token_no"])
    return tokens


def _best_from_rest(payload: dict) -> tuple[float | None, float | None]:
    """Extract (best_bid, best_ask) from a CLOB /book response.

    bids/asks are lists of {"price","size"}; best bid is the highest price,
    best ask the lowest. The API usually pre-sorts, but we don't rely on it.
    """
    def _prices(levels):
        out = []
        for lvl in levels or []:
            try:
                if float(lvl["size"]) > 0:
                    out.append(float(lvl["price"]))
            except (KeyError, TypeError, ValueError):
                continue
        return out

    bids = _prices(payload.get("bids"))
    asks = _prices(payload.get("asks"))
    return (max(bids) if bids else None, min(asks) if asks else None)


async def _fetch_book(session: aiohttp.ClientSession, base: str, token_id: str) -> dict | None:
    url = f"{base.rstrip('/')}/book"
    try:
        async with session.get(url, params={"token_id": token_id},
                               timeout=aiohttp.ClientTimeout(total=15)) as resp:
            resp.raise_for_status()
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.debug("recon: fetch failed for %s: %r", token_id, exc)
        return None


def _record_diff(conn, bus: Bus, token_id: str, field: str,
                 ws_value: float | None, rest_value: float | None) -> None:
    diff = None
    if ws_value is not None and rest_value is not None:
        diff = ws_value - rest_value
    conn.execute(
        "INSERT INTO recon_log (ts, token_id, field, ws_value, rest_value, diff) "
        "VALUES (?,?,?,?,?,?)",
        (time.time(), token_id, field, ws_value, rest_value, diff))
    if diff is not None and abs(diff) > DRIFT_THRESHOLD:
        bus.publish(Event(T_SYSTEM, {
            "what": "recon_drift", "token_id": token_id, "field": field,
            "ws": ws_value, "rest": rest_value, "diff": diff}))
        log.warning("recon drift: token=%s %s ws=%s rest=%s diff=%.4f",
                    token_id, field, ws_value, rest_value, diff)


async def recon_once(conn, bus: Bus, books: BookStore, settings) -> int:
    """Run one reconciliation pass over a random sample. Returns rows written."""
    candidates = [t for t in _tracked_tokens(conn, settings) if books.peek(t) is not None]
    if not candidates:
        return 0
    sample = random.sample(candidates, min(SAMPLE_SIZE, len(candidates)))
    written = 0
    async with aiohttp.ClientSession() as session:
        payloads = await asyncio.gather(
            *(_fetch_book(session, settings.pm_clob_rest, t) for t in sample))
    for token_id, payload in zip(sample, payloads):
        if payload is None:
            continue
        book = books.peek(token_id)
        if book is None:
            continue
        rest_bid, rest_ask = _best_from_rest(payload)
        ws_bid = book.best_bid()[0] if book.best_bid() else None
        ws_ask = book.best_ask()[0] if book.best_ask() else None
        _record_diff(conn, bus, token_id, "best_bid", ws_bid, rest_bid)
        _record_diff(conn, bus, token_id, "best_ask", ws_ask, rest_ask)
        written += 2
    return written


async def recon_task(conn, bus: Bus, books: BookStore, settings) -> None:
    """Run recon_once every settings.recon_interval seconds, forever.

    On startup the WS books haven't populated yet, so the first pass finds no
    candidates. Rather than wait a full interval, we retry quickly until books
    warm up, then settle into the configured cadence.
    """
    backoff = 5.0
    warmup_retry = min(15.0, settings.recon_interval)
    while True:
        try:
            n = await recon_once(conn, bus, books, settings)
            beat(conn, "rest_recon", f"rows={n}")
            backoff = 5.0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("recon pass failed; retrying in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)
            continue

        # Wait until the next pass, but keep the heartbeat fresh every
        # heartbeat_interval so the monitor doesn't flag this component stale
        # (recon_interval is well past the staleness threshold).
        target = settings.recon_interval if n > 0 else warmup_retry
        waited = 0.0
        while waited < target:
            step = min(settings.heartbeat_interval, target - waited)
            await asyncio.sleep(step)
            waited += step
            beat(conn, "rest_recon", f"rows={n}; next recon in ~{int(target - waited)}s")
