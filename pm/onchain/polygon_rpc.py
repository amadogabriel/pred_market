"""Async JSON-RPC client for read-only Polygon access.

Single-purpose: enough to call `eth_getLogs`, `eth_blockNumber`, and
`eth_getBlockByNumber`. We deliberately do not pull in `web3.py` to keep the
runtime light and the dependency surface auditable. Callers using this in
production should provide their own RPC URL (Alchemy, Infura, public node);
the synthetic backend in `tests/` is what we use under unit test.

JSON-RPC requests are batched where possible. We use aiohttp's session pool
for connection reuse. Errors surface as `RpcError`; the caller decides
whether to retry.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

log = logging.getLogger(__name__)


class RpcError(RuntimeError):
    """Raised on JSON-RPC error responses or non-200 HTTP responses."""


@dataclass
class LogFilter:
    from_block: int | str = "latest"
    to_block: int | str = "latest"
    address: str | None = None
    topics: list[str | list[str] | None] | None = None

    def to_params(self) -> dict[str, Any]:
        p: dict[str, Any] = {
            "fromBlock": _hex(self.from_block),
            "toBlock": _hex(self.to_block),
        }
        if self.address:
            p["address"] = self.address
        if self.topics:
            p["topics"] = self.topics
        return p


def _hex(v: int | str) -> str:
    if isinstance(v, str):
        return v
    return hex(v)


class PolygonRpc:
    def __init__(self, url: str, *, timeout: float = 15.0,
                 session: aiohttp.ClientSession | None = None):
        self.url = url
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._owned_session = session is None
        self._session = session
        self._req_id = 0

    async def __aenter__(self) -> "PolygonRpc":
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owned_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def call(self, method: str, params: list[Any]) -> Any:
        assert self._session is not None, "PolygonRpc must be used as async context"
        self._req_id += 1
        body = {"jsonrpc": "2.0", "id": self._req_id, "method": method, "params": params}
        async with self._session.post(self.url, json=body) as resp:
            if resp.status != 200:
                raise RpcError(f"HTTP {resp.status} from {self.url}")
            data = await resp.json()
        if "error" in data:
            raise RpcError(f"{method}: {data['error']}")
        return data.get("result")

    async def block_number(self) -> int:
        result = await self.call("eth_blockNumber", [])
        return int(result, 16)

    async def get_logs(self, flt: LogFilter) -> list[dict]:
        result = await self.call("eth_getLogs", [flt.to_params()])
        return list(result or [])

    async def get_block(self, block_num: int | str, *, full: bool = False) -> dict:
        result = await self.call("eth_getBlockByNumber", [_hex(block_num), full])
        return dict(result or {})


def topic_address(addr: str) -> str:
    """Encode a 20-byte address as a 32-byte topic (left-padded with zeros)."""
    addr = addr.lower().removeprefix("0x")
    if len(addr) != 40:
        raise ValueError(f"bad address length: {addr}")
    return "0x" + "0" * 24 + addr


def decode_address(topic: str) -> str:
    """Reverse of topic_address: extract a 0x-prefixed 20-byte address."""
    t = topic.lower().removeprefix("0x")
    return "0x" + t[-40:]


def decode_uint(hex_data: str) -> int:
    return int(hex_data.removeprefix("0x") or "0", 16)
