"""pm-system engine — asyncio supervisor and entry point.

Wires the whole Phase 0 pipeline together under one TaskGroup:

    event_logger   ← must be up first; it is the replay dataset
    metadata_sync  → populates markets + NegRisk groups, drives the WS universe
    ws_polymarket  ← started indirectly by metadata_sync via ws.set_assets()
    rest_recon     → WS-vs-REST drift checks
    scan_task      → structural-arb scanner (SIGNAL ONLY)
    heartbeat      → liveness for the monitor process

SIGNAL-ONLY: no orders are placed anywhere in this process. LIVE_TRADING is a
hard gate that stays False until the Phase 1 gate (G1) is passed.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

from config.settings import Settings
from pm.ai.task import ai_model_task
from pm.calibration.base_rates import load as load_base_rates
from pm.core import db
from pm.core.books import BookStore
from pm.core.bus import Bus
from pm.execution.fee_engine import FeeEngine
from pm.execution.task import execution_task
from pm.ingestion.event_logger import event_logger_task
from pm.ingestion.metadata_sync import metadata_sync_loop
from pm.ingestion.rest_recon import recon_task
from pm.ingestion.ws_polymarket import PolymarketWS
from pm.news.rss import rss_poller_task
from pm.onchain.ctf_listener import ctf_listener_task
from pm.onchain.wallet_tracker import ensure_schema as ensure_whale_schema
from pm.onchain.whale_scorer import whale_scorer_task
from pm.signals.calibration_div import calibration_div_task
from pm.signals.labeler import labeler_task
from pm.signals.scan_task import scan_task

log = logging.getLogger(__name__)

# Phase 0/1 invariant: no live orders until G1 is passed. Do not flip this
# without a reviewed execution path behind it.
LIVE_TRADING = False


async def heartbeat_task(conn, settings) -> None:
    """Beat the engine's liveness into the DB and the heartbeat file."""
    settings.heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            now = time.time()
            db.beat(conn, "engine")
            settings.heartbeat_path.write_text(str(now))
            await asyncio.sleep(settings.heartbeat_interval)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — liveness must never die
            log.exception("heartbeat failed; retrying")
            await asyncio.sleep(settings.heartbeat_interval)


async def main() -> None:
    settings = Settings()
    log.info("pm-system engine starting (LIVE_TRADING=%s)", LIVE_TRADING)
    conn = db.connect(settings.db_path)
    ensure_whale_schema(conn)
    bus = Bus()
    books = BookStore()
    fee_engine = FeeEngine.from_yaml(settings.fees_yaml)
    ws = PolymarketWS(settings.pm_ws_url, bus, books, settings.ws_assets_per_conn)

    # Shared, single-threaded mutable state: metadata_sync writes, scan_task reads.
    neg_risk_groups: dict[str, list[dict]] = {}

    try:
        async with asyncio.TaskGroup() as tg:
            # event_logger first so it has subscribed before anything publishes.
            tg.create_task(event_logger_task(bus, settings.events_dir),
                           name="event_logger")
            tg.create_task(metadata_sync_loop(conn, books, ws, settings, neg_risk_groups),
                           name="metadata_sync")
            tg.create_task(recon_task(conn, bus, books, settings),
                           name="rest_recon")
            tg.create_task(scan_task(bus, conn, books, fee_engine, settings, neg_risk_groups),
                           name="scan")
            tg.create_task(execution_task(bus, conn, settings, hard_live_gate=LIVE_TRADING),
                           name="execution")
            tg.create_task(labeler_task(conn, books, settings),
                           name="labeler")
            tg.create_task(rss_poller_task(bus, conn, settings),
                           name="rss_poller")
            tg.create_task(ctf_listener_task(conn, bus, settings),
                           name="ctf_listener")
            tg.create_task(whale_scorer_task(conn, settings),
                           name="whale_scorer")
            tg.create_task(calibration_div_task(bus, conn, books, fee_engine, settings),
                           name="calibration")
            tg.create_task(ai_model_task(conn, settings),
                           name="ai_model")
            tg.create_task(heartbeat_task(conn, settings),
                           name="heartbeat")
    finally:
        conn.close()


if __name__ == "__main__":
    # Market questions can contain non-ASCII; default Windows consoles are
    # cp1252 and would raise UnicodeEncodeError when logging them.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")
