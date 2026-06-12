"""Signal scan task.

Drives all live-data scanners from one bus subscription:

- struct_arb (S1)      — executable arb candidates (complement + partition)
- microstructure (S2)  — research: OFI pressure, liquidity shocks, trade-through
- rel_value (S3)       — research: partition-sum drift, complement mid drift

On every `book` / `price_change` event the updated asset is resolved to its
market; struct_arb and the research scanners run against the affected market
and (if NegRisk) its partition group. `last_trade_price` events feed the
trade-through detector. Emitted signals are persisted to `signal_log` and
republished on the bus (so the event logger captures them too).

Signal-only: nothing here places orders. Research signals carry exec_sets=0 so
they can never form an execution plan, and the execution task's strategy
allowlist filters them out before any risk machinery runs.
"""
from __future__ import annotations

import asyncio
import logging
import time

from pm.core.bus import Bus
from pm.core.db import beat, log_signal
from pm.core.events import Event, T_BOOK, T_PRICE_CHANGE, T_SIGNAL, T_TRADE
from pm.execution.fee_engine import FeeEngine
from pm.signals.common import ResearchSignal
from pm.signals.microstructure import MicrostructureTracker
from pm.signals.momentum import MomentumTracker
from pm.signals.relative_value import RelativeValueMonitor
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


def _persist_arb(conn, bus: Bus, scanner: StructArbScanner,
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


def _persist_research(conn, bus: Bus, signals: list[ResearchSignal]) -> int:
    """Research signals are pre-debounced by their trackers; persist directly."""
    for sig in signals:
        sid = log_signal(conn, strategy=sig.strategy, kind=sig.kind,
                         group_id=sig.group_id, legs=sig.legs,
                         gross_edge=sig.gross_edge, fees=sig.fees,
                         net_edge=sig.net_edge, exec_sets=0.0,
                         features=sig.features)
        bus.publish(Event(T_SIGNAL, {
            "strategy": sig.strategy, "signal_id": sid, "kind": sig.kind,
            "group_id": sig.group_id, "net_edge": sig.net_edge,
            "exec_sets": 0.0}))
        log.info("research %d: %s/%s group=%s gross=%.4f",
                 sid, sig.strategy, sig.kind, sig.group_id, sig.gross_edge)
    return len(signals)


async def scan_task(bus: Bus, conn, books, fee_engine: FeeEngine, settings,
                    neg_risk_groups: dict[str, list[dict]]) -> None:
    """Subscribe to book/trade updates and scan the affected market on each one."""
    scanner = StructArbScanner(
        books, fee_engine, buffer=settings.arb_buffer,
        min_sets=settings.arb_min_set_size, stale_after=settings.stale_book_after)

    micro = rv = mom = None
    if settings.research_signals_enabled:
        micro = MicrostructureTracker(
            books, fee_engine,
            window_s=settings.micro_window_s, min_samples=settings.micro_min_samples,
            ofi_threshold=settings.micro_ofi_threshold, max_spread=settings.micro_max_spread,
            liq_spread_mult=settings.micro_liq_spread_mult,
            liq_depth_drop=settings.micro_liq_depth_drop,
            trade_abs_floor=settings.micro_trade_abs_floor,
            debounce_s=settings.micro_debounce_s, stale_after=settings.stale_book_after)
        rv = RelativeValueMonitor(
            books, fee_engine,
            window_s=settings.rv_window_s, min_samples=settings.rv_min_samples,
            z_threshold=settings.rv_z_threshold, min_abs_dev=settings.rv_min_abs_dev,
            debounce_s=settings.rv_debounce_s, stale_after=settings.stale_book_after)
        mom = MomentumTracker(
            books, fee_engine,
            window_s=settings.mom_window_s, min_samples=settings.mom_min_samples,
            z_threshold=settings.mom_z_threshold,
            min_abs_drift=settings.mom_min_abs_drift,
            boundary_low=settings.mom_boundary_low,
            boundary_high=settings.mom_boundary_high,
            boundary_bounce=settings.mom_boundary_bounce,
            debounce_s=settings.mom_debounce_s, stale_after=settings.stale_book_after)

    queue = bus.subscribe(T_BOOK, T_PRICE_CHANGE, T_TRADE)

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
                    if event.topic == T_TRADE:
                        if micro is not None:
                            _persist_research(conn, bus,
                                              micro.on_trade(event.payload, meta))
                    else:
                        _scan_book_update(conn, bus, scanner, micro, rv, mom,
                                          asset_id, meta, neg_risk_groups)
            except Exception:  # noqa: BLE001 — one bad event must not kill the scanner
                log.exception("scan failed for event %r", event.topic)

        if now - last_beat > settings.heartbeat_interval:
            beat(conn, "scan_task")
            last_beat = now


def _scan_book_update(conn, bus: Bus, scanner: StructArbScanner,
                      micro: MicrostructureTracker | None,
                      rv: RelativeValueMonitor | None,
                      mom: MomentumTracker | None,
                      asset_id: str, meta: dict,
                      neg_risk_groups: dict[str, list[dict]]) -> None:
    # S1 — executable arb candidates
    sigs = scanner.scan_complement(
        meta["market_id"], meta["token_yes"], meta["token_no"], meta["category"])
    group_id = meta["neg_risk_id"]
    legs_meta = neg_risk_groups.get(group_id) if group_id else None
    if legs_meta:
        sigs = sigs + scanner.scan_partition(group_id, legs_meta)
    if sigs:
        _persist_arb(conn, bus, scanner, sigs)

    # S2 — microstructure research
    if micro is not None:
        _persist_research(conn, bus, micro.on_book_update(asset_id, meta))

    # S3 — relative-value research
    if rv is not None:
        _persist_research(conn, bus, rv.on_market_update(meta))
        if legs_meta:
            _persist_research(conn, bus, rv.on_group_update(group_id, legs_meta))

    # S4 — momentum / boundary research
    if mom is not None:
        _persist_research(conn, bus, mom.on_book_update(asset_id, meta))
