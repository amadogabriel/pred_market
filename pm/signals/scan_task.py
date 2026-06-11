"""Signal scan task.

Drives the structural-arb scanner from live book updates. On every `book` /
`price_change` event it resolves the updated asset to its market, runs the
complement check, and — if the market is part of a NegRisk group — the
partition scan. Emitted signals are persisted to `signal_log` and republished
on the bus (so the event logger captures them too).

Signal-only: nothing here places orders. The `signal` topic is lossless, so a
wedged consumer surfaces as a bus RuntimeError rather than silent data loss.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pm.core.bus import Bus
from pm.core.db import beat, log_signal
from pm.core.events import Event, T_BOOK, T_PRICE_CHANGE, T_SIGNAL
from pm.execution.fee_engine import FeeEngine
from pm.signals.struct_arb import ArbSignal, StructArbScanner

log = logging.getLogger(__name__)

INDEX_REFRESH = 60.0
POLL_TIMEOUT = 5.0   # wake up at least this often to beat/refresh even when idle


def _load_market_index(conn) -> dict[str, dict]:
    """token_id -> market metadata, for both the yes and no legs of each market."""
    rows = conn.execute(
        "SELECT market_id, category, neg_risk_id, token_yes, token_no "
        "FROM markets WHERE active=1 AND closed=0").fetchall()
    index: dict[str, dict] = {}
    for r in rows:
        meta = {
            "market_id": r["market_id"],
            "category": r["category"],
            "neg_risk_id": r["neg_risk_id"],
            "token_yes": r["token_yes"],
            "token_no": r["token_no"],
        }
        if r["token_yes"]:
            index[r["token_yes"]] = meta
        if r["token_no"]:
            index[r["token_no"]] = meta
    return index


def _persist_and_publish(conn, bus: Bus, scanner: StructArbScanner,
                         signals: list[ArbSignal]) -> int:
    emitted = 0
    for sig in signals:
        if not scanner.should_emit(sig):
            continue
        sid = log_signal(conn, strategy="struct_arb", kind=sig.kind,
                         group_id=sig.group_id, legs=sig.legs,
                         gross_edge=sig.gross_edge, fees=sig.fees,
                         net_edge=sig.net_edge, exec_sets=sig.exec_sets,
                         features=sig.features)
        bus.publish(Event(T_SIGNAL, {
            "strategy": "struct_arb", "signal_id": sid, "kind": sig.kind,
            "group_id": sig.group_id, "net_edge": sig.net_edge,
            "exec_sets": sig.exec_sets, "total_net": sig.total_net}))
        log.info("signal %d: %s group=%s net_edge=%.4f sets=%.1f",
                 sid, sig.kind, sig.group_id, sig.net_edge, sig.exec_sets)
        emitted += 1
    return emitted


async def scan_task(bus: Bus, conn, books, fee_engine: FeeEngine, settings,
                    neg_risk_groups: dict[str, list[dict]]) -> None:
    """Subscribe to book updates and scan the affected market on each one."""
    scanner = StructArbScanner(
        books, fee_engine, buffer=settings.arb_buffer,
        min_sets=settings.arb_min_set_size, stale_after=settings.stale_book_after)
    queue = bus.subscribe(T_BOOK, T_PRICE_CHANGE)

    index = _load_market_index(conn)
    last_index = time.time()
    last_beat = 0.0

    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=POLL_TIMEOUT)
        except asyncio.TimeoutError:
            event = None
        except asyncio.CancelledError:
            raise

        now = time.time()
        if now - last_index > INDEX_REFRESH:
            index = _load_market_index(conn)
            last_index = now

        if event is not None:
            try:
                asset_id = event.payload.get("asset_id")
                meta = index.get(asset_id) if asset_id else None
                if meta is not None:
                    sigs = scanner.scan_complement(
                        meta["market_id"], meta["token_yes"], meta["token_no"],
                        meta["category"])
                    group_id = meta["neg_risk_id"]
                    if group_id and group_id in neg_risk_groups:
                        sigs = sigs + scanner.scan_partition(
                            group_id, neg_risk_groups[group_id])
                    if sigs:
                        _persist_and_publish(conn, bus, scanner, sigs)
            except Exception:  # noqa: BLE001 — one bad event must not kill the scanner
                log.exception("scan failed for event %r", event.topic)

        if now - last_beat > settings.heartbeat_interval:
            beat(conn, "scan_task")
            last_beat = now
