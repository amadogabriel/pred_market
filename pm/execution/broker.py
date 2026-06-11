"""Broker interfaces.

Only a dry-run broker is implemented. The live broker intentionally fails
closed until Phase 1 has passed the manual review and credential work.
"""
from __future__ import annotations

import asyncio
from typing import Protocol

from pm.execution.models import BrokerReceipt, OrderIntent


class Broker(Protocol):
    async def submit(self, intent: OrderIntent) -> BrokerReceipt:
        ...


class DryRunBroker:
    async def submit(self, intent: OrderIntent) -> BrokerReceipt:
        await asyncio.sleep(0)
        return BrokerReceipt(
            accepted=True,
            status="submitted",
            broker_order_id=f"dryrun-{intent.client_order_id}",
            reason="dry run only; no exchange call made",
        )


class LiveBrokerUnavailable:
    async def submit(self, intent: OrderIntent) -> BrokerReceipt:
        await asyncio.sleep(0)
        return BrokerReceipt(
            accepted=False,
            status="failed",
            reason="live broker is not implemented; refusing to place orders",
        )


def build_broker(mode: str) -> Broker:
    if mode == "dry_run":
        return DryRunBroker()
    return LiveBrokerUnavailable()
