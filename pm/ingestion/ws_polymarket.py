"""Polymarket CLOB market-channel WebSocket consumer.

Protocol (verify against docs.polymarket.com if behavior looks off):
  endpoint  wss://ws-subscriptions-clob.polymarket.com/ws/market
  subscribe {"type": "market", "assets_ids": [...], "custom_feature_enabled": true}
  dynamic   {"assets_ids": [...], "operation": "subscribe" | "unsubscribe"}
  keepalive send "PING" every 10s, server replies "PONG"; server may also
            ping — the websockets library answers protocol-level pings itself.
  messages  JSON object OR array of objects, each with event_type in
            {book, price_change, last_trade_price, tick_size_change,
             new_market, market_resolved, best_bid_ask}

One connection per chunk of assets (chunk size configurable). Exponential
backoff reconnect; on reconnect the server emits fresh `book` snapshots, so
book state self-heals.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random

import websockets

from pm.core.books import BookStore
from pm.core.bus import Bus
from pm.core.events import (Event, T_BOOK, T_MARKET_EVT, T_PRICE_CHANGE,
                            T_SYSTEM, T_TICK, T_TRADE)

log = logging.getLogger(__name__)

TOPIC_BY_TYPE = {
    "book": T_BOOK,
    "price_change": T_PRICE_CHANGE,
    "last_trade_price": T_TRADE,
    "tick_size_change": T_TICK,
    "new_market": T_MARKET_EVT,
    "market_resolved": T_MARKET_EVT,
    "best_bid_ask": T_MARKET_EVT,
}


class PolymarketWS:
    def __init__(self, url: str, bus: Bus, books: BookStore,
                 assets_per_conn: int = 100):
        self.url = url
        self.bus = bus
        self.books = books
        self.assets_per_conn = assets_per_conn
        self._assets: list[str] = []
        self._tasks: list[asyncio.Task] = []
        self.connected_chunks: set[int] = set()
        self.msg_count = 0

    def set_assets(self, assets: list[str]) -> None:
        """Set the tracked universe. Called by metadata_sync; restarts conns on change."""
        new = sorted(set(assets))
        if new == self._assets:
            return
        self._assets = new
        self._restart()

    def _restart(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        self.connected_chunks.clear()
        chunks = [self._assets[i:i + self.assets_per_conn]
                  for i in range(0, len(self._assets), self.assets_per_conn)]
        for idx, chunk in enumerate(chunks):
            self._tasks.append(asyncio.create_task(
                self._run_conn(idx, chunk), name=f"pm-ws-{idx}"))
        log.info("ws: tracking %d assets across %d connections",
                 len(self._assets), len(chunks))

    async def _run_conn(self, idx: int, assets: list[str]) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(
                        self.url, ping_interval=20, ping_timeout=20,
                        max_size=2**23) as ws:
                    await ws.send(json.dumps({
                        "type": "market",
                        "assets_ids": assets,
                        "custom_feature_enabled": True,
                    }))
                    self.connected_chunks.add(idx)
                    self.bus.publish(Event(T_SYSTEM, {
                        "what": "ws_connected", "chunk": idx, "n_assets": len(assets)}))
                    backoff = 1.0
                    pinger = asyncio.create_task(self._pinger(ws))
                    try:
                        async for raw in ws:
                            self._handle_raw(raw)
                    finally:
                        pinger.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — reconnect on anything
                self.connected_chunks.discard(idx)
                self.bus.publish(Event(T_SYSTEM, {
                    "what": "ws_disconnected", "chunk": idx, "err": repr(exc)}))
                log.warning("ws chunk %d dropped (%r); reconnecting in %.1fs",
                            idx, exc, backoff)
                await asyncio.sleep(backoff + random.uniform(0, 0.5))
                backoff = min(backoff * 2, 60.0)

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(10)
            await ws.send("PING")

    def _handle_raw(self, raw: str | bytes) -> None:
        if raw == "PONG" or raw == b"PONG":
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            log.debug("ws: non-JSON frame: %.80r", raw)
            return
        msgs = data if isinstance(data, list) else [data]
        for msg in msgs:
            if not isinstance(msg, dict):
                continue
            et = msg.get("event_type")
            topic = TOPIC_BY_TYPE.get(et)
            if topic is None:
                continue
            self.msg_count += 1
            if et in ("book", "price_change"):
                self.books.handle_ws_message(msg)
            self.bus.publish(Event(topic, msg))
