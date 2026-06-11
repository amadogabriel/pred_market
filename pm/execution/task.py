"""Execution task for Phase 1+.

The task subscribes to signal events and can turn them into order intents, but
the default configuration is disabled. Live mode is additionally blocked by
the engine's hard gate, which remains False until G0 has passed and Phase 1 is
explicitly reviewed.
"""
from __future__ import annotations

import asyncio
import logging

from config.settings import Settings
from pm.core import db
from pm.core.bus import Bus
from pm.core.events import Event, T_EXECUTION, T_SIGNAL
from pm.execution.broker import build_broker
from pm.execution.models import intents_from_signal
from pm.execution.risk import RiskLimits, RiskManager

log = logging.getLogger(__name__)

POLL_TIMEOUT = 5.0


async def execution_task(bus: Bus, conn, settings: Settings, *,
                         hard_live_gate: bool = False) -> None:
    """Plan and optionally submit signal-derived execution intents."""
    queue = bus.subscribe(T_SIGNAL)
    broker = build_broker(settings.execution_mode)
    risk = RiskManager(RiskLimits.from_settings(settings, hard_live_gate=hard_live_gate))

    last_beat = 0.0
    while True:
        try:
            event = await asyncio.wait_for(queue.get(), timeout=POLL_TIMEOUT)
        except asyncio.TimeoutError:
            event = None
        except asyncio.CancelledError:
            raise

        if event is not None:
            try:
                await _handle_signal_event(bus, conn, settings, broker, risk, event)
            except Exception:  # noqa: BLE001 - one bad signal must not kill the task
                log.exception("execution task failed to process signal event")

        now = asyncio.get_running_loop().time()
        if now - last_beat > settings.heartbeat_interval:
            mode = settings.execution_mode if settings.execution_enabled else "disabled"
            db.beat(conn, "execution", mode)
            last_beat = now


async def _handle_signal_event(bus: Bus, conn, settings: Settings, broker,
                               risk: RiskManager, event: Event) -> None:
    signal_id = event.payload.get("signal_id")
    if signal_id is None:
        return
    signal = db.get_signal(conn, int(signal_id))
    if signal is None:
        db.log_risk_event(conn, code="signal_missing",
                          detail=f"signal_id={signal_id} not found", signal_id=int(signal_id))
        return

    intents = intents_from_signal(signal, venue="polymarket")
    plan_decision = risk.check_plan(conn, signal, intents)
    if not plan_decision.approved:
        db.log_risk_event(conn, code=plan_decision.code, detail=plan_decision.detail,
                          signal_id=int(signal_id), severity="info")
        bus.publish(Event(T_EXECUTION, {
            "what": "plan_rejected", "signal_id": signal_id,
            "code": plan_decision.code, "detail": plan_decision.detail}))
        return

    for intent in intents:
        decision = risk.check_intent(conn, intent)
        status = "planned" if decision.approved else "rejected"
        intent_id = db.record_execution_intent(
            conn, intent.to_record(), status=status,
            reason="" if decision.approved else decision.detail)

        if not decision.approved:
            db.log_risk_event(conn, code=decision.code, detail=decision.detail,
                              signal_id=int(signal_id), intent_id=intent_id, severity="info")
            bus.publish(Event(T_EXECUTION, {
                "what": "intent_rejected", "signal_id": signal_id,
                "intent_id": intent_id, "code": decision.code, "detail": decision.detail}))
            continue

        receipt = await broker.submit(intent)
        db.update_execution_intent(
            conn, intent_id, status=receipt.status, reason=receipt.reason,
            broker_order_id=receipt.broker_order_id)
        bus.publish(Event(T_EXECUTION, {
            "what": "intent_submitted" if receipt.accepted else "intent_failed",
            "signal_id": signal_id,
            "intent_id": intent_id,
            "broker_order_id": receipt.broker_order_id,
            "status": receipt.status,
            "reason": receipt.reason,
        }))
        log.info("execution %s intent=%s signal=%s status=%s",
                 settings.execution_mode, intent_id, signal_id, receipt.status)
