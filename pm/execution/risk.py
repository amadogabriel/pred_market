"""Fail-closed risk gates for Phase 1+ execution.

The risk manager is deliberately conservative. It can approve dry-run intents
for operational testing, but live execution needs all three gates open:

1. `PM_EXECUTION_ENABLED=true`
2. `PM_EXECUTION_MODE=live` and `PM_LIVE_TRADING=true`
3. the engine hard gate passed as `hard_live_gate=True`

The engine currently passes `False`, so live trading remains impossible until
the code is explicitly reviewed after G0.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from config.settings import Settings
from pm.execution.models import OrderIntent, RiskDecision


@dataclass(frozen=True)
class RiskLimits:
    execution_enabled: bool
    execution_mode: str
    live_trading: bool
    hard_live_gate: bool
    max_order_notional: float
    max_signal_notional: float
    max_open_notional: float
    max_daily_loss: float
    max_recon_diff_for_execution: float
    allow_unverified_negrisk: bool
    verified_groups_path: Path
    kill_switch_path: Path
    allow_short: bool = False

    @classmethod
    def from_settings(cls, settings: Settings, *, hard_live_gate: bool = False) -> "RiskLimits":
        return cls(
            execution_enabled=settings.execution_enabled,
            execution_mode=settings.execution_mode,
            live_trading=settings.live_trading,
            hard_live_gate=hard_live_gate,
            max_order_notional=settings.max_order_notional,
            max_signal_notional=settings.max_signal_notional,
            max_open_notional=settings.max_open_notional,
            max_daily_loss=settings.max_daily_loss,
            max_recon_diff_for_execution=settings.max_recon_diff_for_execution,
            allow_unverified_negrisk=settings.allow_unverified_negrisk,
            verified_groups_path=settings.verified_groups_path,
            kill_switch_path=settings.kill_switch_path,
        )


class RiskManager:
    def __init__(self, limits: RiskLimits):
        self.limits = limits
        self.verified_groups = self._load_verified_groups(limits.verified_groups_path)

    @staticmethod
    def _load_verified_groups(path: Path) -> set[str]:
        if not path.exists():
            return set()
        out = set()
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                out.add(line)
        return out

    def check_plan(self, conn: sqlite3.Connection, signal: dict,
                   intents: list[OrderIntent]) -> RiskDecision:
        if not self.limits.execution_enabled:
            return RiskDecision(False, "execution_disabled", "PM_EXECUTION_ENABLED is false")
        if self.limits.kill_switch_path.exists():
            return RiskDecision(False, "kill_switch", f"kill switch exists at {self.limits.kill_switch_path}")
        if self.limits.execution_mode not in {"dry_run", "live"}:
            return RiskDecision(False, "bad_execution_mode", self.limits.execution_mode)
        if self.limits.execution_mode == "live" and not (
            self.limits.live_trading and self.limits.hard_live_gate
        ):
            return RiskDecision(False, "live_gate_closed", "live execution requires PM_LIVE_TRADING and hard gate")
        if not intents:
            return RiskDecision(False, "empty_plan", "signal produced no executable intents")

        total = sum(i.notional for i in intents)
        if total > self.limits.max_signal_notional:
            return RiskDecision(
                False, "signal_notional_limit",
                f"signal notional {total:.2f} > {self.limits.max_signal_notional:.2f}")

        kind = str(signal.get("kind", ""))
        group_id = signal.get("group_id")
        if kind.startswith("partition_") and not self.limits.allow_unverified_negrisk:
            if not group_id or str(group_id) not in self.verified_groups:
                return RiskDecision(
                    False, "unverified_negrisk_group",
                    f"group {group_id!r} is not in {self.limits.verified_groups_path}")

        max_recent_diff = self._recent_max_recon_diff(conn)
        if max_recent_diff is not None and max_recent_diff > self.limits.max_recon_diff_for_execution:
            return RiskDecision(
                False, "recon_drift",
                f"recent max recon diff {max_recent_diff:.4f} > "
                f"{self.limits.max_recon_diff_for_execution:.4f}")

        daily_pnl = self._daily_realized_pnl(conn)
        if daily_pnl < -abs(self.limits.max_daily_loss):
            return RiskDecision(
                False, "daily_loss_limit",
                f"realized PnL {daily_pnl:.2f} breached -{self.limits.max_daily_loss:.2f}")

        open_notional = self._open_notional(conn)
        if open_notional + total > self.limits.max_open_notional:
            return RiskDecision(
                False, "open_notional_limit",
                f"open+signal notional {open_notional + total:.2f} > "
                f"{self.limits.max_open_notional:.2f}")

        return RiskDecision(True, "approved", "plan passed risk gates")

    def check_intent(self, conn: sqlite3.Connection, intent: OrderIntent) -> RiskDecision:
        if intent.side not in {"BUY", "SELL"}:
            return RiskDecision(False, "bad_side", intent.side)
        if not (0.0 < intent.price < 1.0):
            return RiskDecision(False, "bad_price", f"price={intent.price}")
        if intent.size <= 0:
            return RiskDecision(False, "bad_size", f"size={intent.size}")
        if intent.notional > self.limits.max_order_notional:
            return RiskDecision(
                False, "order_notional_limit",
                f"intent notional {intent.notional:.2f} > {self.limits.max_order_notional:.2f}")
        if intent.side == "SELL" and not self.limits.allow_short:
            available = self._position_size(conn, intent.venue, intent.token_id)
            if available < intent.size:
                return RiskDecision(
                    False, "insufficient_inventory",
                    f"sell size {intent.size:.4f} > position {available:.4f}")
        return RiskDecision(True, "approved", "intent passed risk gates")

    def _recent_max_recon_diff(self, conn: sqlite3.Connection) -> float | None:
        row = conn.execute(
            "SELECT MAX(ABS(diff)) v FROM recon_log WHERE diff IS NOT NULL AND ts > ?",
            (time.time() - 3600.0,)).fetchone()
        return None if row is None or row["v"] is None else float(row["v"])

    def _daily_realized_pnl(self, conn: sqlite3.Connection) -> float:
        # Positions carry aggregate realized PnL for now. Resolution/unwind code
        # in later phases can split this by day if needed.
        row = conn.execute("SELECT COALESCE(SUM(realized_pnl), 0) v FROM positions").fetchone()
        return float(row["v"] or 0.0)

    def _open_notional(self, conn: sqlite3.Connection) -> float:
        row = conn.execute(
            "SELECT COALESCE(SUM(ABS(size) * avg_price), 0) v FROM positions").fetchone()
        return float(row["v"] or 0.0)

    def _position_size(self, conn: sqlite3.Connection, venue: str, token_id: str) -> float:
        row = conn.execute(
            "SELECT size FROM positions WHERE venue=? AND token_id=?",
            (venue, token_id)).fetchone()
        return 0.0 if row is None else float(row["size"] or 0.0)
