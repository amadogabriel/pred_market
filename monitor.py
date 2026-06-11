"""pm-system monitor — separate watchdog process.

Runs alongside (but independent of) the engine so it survives engine crashes.
Reads the engine's heartbeat file and the heartbeats / recon_log / rules_text
tables, and raises Telegram alerts on:

    - engine heartbeat stale (file missing or too old)
    - any DB-tracked component stale
    - recon drift (large WS-vs-REST diffs recently logged)
    - resolution rules changed (a new rules_text version appeared)

Telegram is optional: with no PM_TG_TOKEN configured it prints instead, so the
monitor is fully functional in local dev. State transitions are tracked so a
standing condition alerts once and emits a recovery message when it clears.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import time

import aiohttp

from config.settings import Settings
from pm.core import db

log = logging.getLogger(__name__)

CHECK_INTERVAL = 30.0
STALE_COMPONENT_AGE = 120.0


async def send_alert(session: aiohttp.ClientSession, settings, msg: str) -> None:
    """Send to Telegram if configured, else print (dev fallback)."""
    if not settings.telegram_token or not settings.telegram_chat_id:
        print(f"[ALERT] {msg}", flush=True)  # flush: stdout may be redirected/buffered
        return
    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    try:
        async with session.post(
                url, json={"chat_id": settings.telegram_chat_id, "text": msg},
                timeout=aiohttp.ClientTimeout(total=15)) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.warning("telegram send failed (%d): %s", resp.status, body)
    except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
        log.warning("telegram send error: %r", exc)


def _heartbeat_age(settings) -> float | None:
    """Seconds since the heartbeat file was last written, or None if missing."""
    path = settings.heartbeat_path
    if not path.exists():
        return None
    return time.time() - path.stat().st_mtime


def _recent_drift(conn, since: float) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM recon_log WHERE ts > ? AND ABS(COALESCE(diff,0)) > 0.02",
        (since,)).fetchone()
    return int(row["c"])


def _recent_rules_changes(conn, since: float) -> list[str]:
    """Markets whose rules text changed (>1 version) with a recent new version."""
    rows = conn.execute(
        "SELECT market_id, COUNT(*) c, MAX(fetched_at) mx FROM rules_text "
        "GROUP BY market_id, venue HAVING c > 1 AND mx > ?",
        (since,)).fetchall()
    return [r["market_id"] for r in rows]


async def run_monitor(settings) -> None:
    conn = db.connect(settings.db_path)
    active: dict[str, bool] = {}     # alert-key -> currently firing
    last_check = time.time()

    async with aiohttp.ClientSession() as session:
        await send_alert(session, settings, "✅ monitor started")
        while True:
            try:
                now = time.time()

                # 1. Engine heartbeat file.
                age = _heartbeat_age(settings)
                engine_stale = age is None or age > settings.heartbeat_stale_after
                was = active.get("engine_stale", False)
                if engine_stale and not was:
                    detail = "file missing" if age is None else f"{age:.0f}s old"
                    await send_alert(session, settings,
                                     f"⚠️ engine heartbeat stale ({detail}) — may be down")
                elif not engine_stale and was:
                    await send_alert(session, settings, "✅ engine heartbeat recovered")
                active["engine_stale"] = engine_stale

                # 2. DB-tracked component staleness.
                for comp, comp_age in db.stale_components(conn, STALE_COMPONENT_AGE):
                    key = f"stale:{comp}"
                    if not active.get(key, False):
                        await send_alert(session, settings,
                                         f"⚠️ component '{comp}' stale ({comp_age:.0f}s)")
                    active[key] = True
                # clear recovered components
                still_stale = {f"stale:{c}" for c, _ in db.stale_components(conn, STALE_COMPONENT_AGE)}
                for key in [k for k in active if k.startswith("stale:")]:
                    if key not in still_stale and active.get(key):
                        await send_alert(session, settings, f"✅ {key[6:]} recovered")
                        active[key] = False

                # 3. Recon drift since last check (transient — alert per occurrence).
                drift = _recent_drift(conn, last_check)
                if drift:
                    await send_alert(session, settings,
                                     f"⚠️ recon drift: {drift} large WS-vs-REST diffs")

                # 4. Rules changes since last check.
                for mid in _recent_rules_changes(conn, last_check):
                    await send_alert(session, settings,
                                     f"⚠️ resolution rules changed for market {mid}")

                db.beat(conn, "monitor")
                last_check = now
                await asyncio.sleep(CHECK_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the watchdog must outlive everything
                log.exception("monitor check failed; continuing")
                await asyncio.sleep(CHECK_INTERVAL)


def _force_utf8_streams() -> None:
    """Alert messages carry emoji; default Windows consoles use cp1252 and
    raise UnicodeEncodeError on print(). Reconfigure to UTF-8 (best effort)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


if __name__ == "__main__":
    _force_utf8_streams()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        asyncio.run(run_monitor(Settings()))
    except KeyboardInterrupt:
        log.info("monitor shutting down")
