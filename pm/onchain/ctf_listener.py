"""Async loop that polls Polygon for new CTF transfers and publishes them to the bus.

The listener uses `eth_getLogs` over a moving block window. We do not subscribe
to websocket logs because most public/free Polygon endpoints don't expose
`eth_subscribe`. Polling every 5 seconds gives 1-2 block lag which is
acceptable for whale-follow (the wallet was already paying when the trade
landed; we are always second).

On each poll:
1. Fetch logs for TransferSingle and TransferBatch on the CTF contract
2. Decode into `CtfTransfer` records
3. For trades involving a *tracked* wallet (either side), record the position
   in `whale_positions` and publish a `system` event so the whale-follow
   signal scanner can pick it up

The token_id → market_id resolution requires querying Polymarket Gamma since
the CTF tokenId is a hash that maps to a `condition_id`. We do a best-effort
join against the live `markets` table; unresolved ids stay null and can be
filled in later.

Configuration:
- PM_POLYGON_RPC_URL  — JSON-RPC endpoint (default: empty = listener disabled)
- PM_POLYGON_CTF_ADDRESS — CTF contract (default: 0x4D97DCd97eC945f40cF65F87097ACe5EA0476045)
- PM_POLYGON_POLL_S — poll cadence (default: 5)
- PM_POLYGON_LOOKBACK_BLOCKS — initial window size (default: 50)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from pm.core.bus import Bus
from pm.core.db import beat
from pm.core.events import Event, T_SYSTEM
from pm.onchain.ctf_events import (TOPIC_TRANSFER_BATCH, TOPIC_TRANSFER_SINGLE,
                                   decode_batch, decode_single)
from pm.onchain.polygon_rpc import LogFilter, PolygonRpc, RpcError
from pm.onchain.wallet_tracker import (ensure_schema, tracked_wallets,
                                       upsert_position)

log = logging.getLogger(__name__)

DEFAULT_CTF = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"


async def ctf_listener_task(conn, bus: Bus, settings) -> None:
    """Watch Polygon for tracked-wallet CTF transfers; publish + persist."""
    if not getattr(settings, "polygon_rpc_url", ""):
        log.info("ctf_listener: PM_POLYGON_RPC_URL empty; listener disabled")
        while True:
            beat(conn, "ctf_listener", "disabled")
            await asyncio.sleep(max(60, settings.heartbeat_interval))

    ensure_schema(conn)
    ctf_addr = (getattr(settings, "polygon_ctf_address", DEFAULT_CTF)
                or DEFAULT_CTF).lower()
    poll_s = float(getattr(settings, "polygon_poll_s", 5.0))
    lookback = int(getattr(settings, "polygon_lookback_blocks", 50))

    last_block: int | None = None
    backoff = 5.0
    async with PolygonRpc(settings.polygon_rpc_url) as rpc:
        log.info("ctf_listener: watching %s every %.0fs", ctf_addr, poll_s)
        while True:
            try:
                head = await rpc.block_number()
                if last_block is None:
                    last_block = max(0, head - lookback)
                from_block = last_block + 1
                if from_block <= head:
                    await _scan_window(rpc, conn, bus, ctf_addr,
                                       from_block, head)
                    last_block = head
                beat(conn, "ctf_listener", f"head={head}")
                backoff = 5.0
                await asyncio.sleep(poll_s)
            except asyncio.CancelledError:
                raise
            except (RpcError, Exception) as e:  # noqa: BLE001
                log.warning("ctf_listener pass failed: %r; backing off %.0fs",
                            e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 300.0)


async def _scan_window(rpc: PolygonRpc, conn, bus: Bus, ctf_addr: str,
                       from_block: int, to_block: int) -> None:
    """Fetch + decode + persist tracked-wallet transfers in the block window."""
    tracked = set(w.lower() for w in tracked_wallets(conn))
    if not tracked:
        return

    flt = LogFilter(
        from_block=from_block, to_block=to_block, address=ctf_addr,
        topics=[[TOPIC_TRANSFER_SINGLE, TOPIC_TRANSFER_BATCH]])
    logs = await rpc.get_logs(flt)

    transfers = []
    for lg in logs:
        topic0 = (lg.get("topics") or [""])[0].lower()
        if topic0 == TOPIC_TRANSFER_SINGLE:
            t = decode_single(lg)
            if t:
                transfers.append(t)
        elif topic0 == TOPIC_TRANSFER_BATCH:
            transfers.extend(decode_batch(lg))

    if not transfers:
        return

    market_lookup = _market_id_lookup(conn)
    relevant = 0
    for t in transfers:
        # Either side participating counts
        sides = []
        if t.from_addr.lower() in tracked:
            sides.append(("SELL", t.from_addr))
        if t.to_addr.lower() in tracked:
            sides.append(("BUY", t.to_addr))
        if not sides:
            continue
        token_id_str = str(t.token_id)
        market_id = market_lookup.get(token_id_str)
        for side, wallet in sides:
            upsert_position(
                conn, wallet=wallet, token_id=token_id_str,
                market_id=market_id, side=side, value_raw=t.value,
                tx_hash=t.tx_hash, block_number=t.block_number)
            bus.publish(Event(T_SYSTEM, {
                "what": "whale_transfer", "wallet": wallet,
                "token_id": token_id_str, "market_id": market_id,
                "side": side, "value_raw": t.value,
                "tx_hash": t.tx_hash, "block": t.block_number}))
            relevant += 1
    if relevant:
        log.info("ctf_listener: %d tracked transfers in blocks %d..%d",
                 relevant, from_block, to_block)


def _market_id_lookup(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT token_yes, token_no, market_id FROM markets "
        "WHERE token_yes IS NOT NULL OR token_no IS NOT NULL").fetchall()
    out: dict[str, str] = {}
    for ty, tn, mid in rows:
        if ty:
            out[str(ty)] = mid
        if tn:
            out[str(tn)] = mid
    return out
