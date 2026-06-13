"""Score discovered whale positions against resolved markets, promote winners.

This is the piece that makes whale-follow runnable without a hand-curated
wallet list. The bootstrap problem: you can only score a wallet once it has
resolved positions, but the signal scanner only fires on *tracked* wallets.
Discovery mode (in ctf_listener) records large positions from ANY wallet;
this scorer closes the loop:

1. score_unresolved — match each unscored whale_position to a market whose
   `outcome_prices_json` is populated (resolved). For a BUY of token T:
   correct if T paid $1 at resolution; for a SELL, the inverse. Records the
   per-position outcome via wallet_tracker.record_resolution, which updates
   the wallet's calibration aggregate.

2. promote_wallets — any wallet with calibration >= baseline over >= min
   resolved positions is flipped to tracked=1. From then on the whale-follow
   signal scanner emits on its new positions.

3. (optional) demote_wallets — drop tracked wallets back below baseline.

Outcome convention matches the rest of the system: positive = the wallet's
directional bet was correct. We do NOT compute dollar PnL here — CTF transfers
carry no entry price, so realized_pnl stays 0 and *calibration* (hit rate on
resolved bets) is the metric we promote on.
"""
from __future__ import annotations

import asyncio
import json
import logging

from pm.core.db import beat
from pm.onchain.wallet_tracker import record_resolution, track_wallet

log = logging.getLogger(__name__)


def _resolved_token_map(conn) -> dict[str, float]:
    """token_id -> payout in {0.0, 1.0} for every resolved market.

    outcome_prices_json is the Gamma `outcomePrices` array, e.g. ["0","1"]
    meaning the YES token paid 0 and the NO token paid 1. Index 0 is the YES
    (token_yes) payout, index 1 the NO (token_no) payout.
    """
    out: dict[str, float] = {}
    rows = conn.execute(
        "SELECT token_yes, token_no, outcome_prices_json FROM markets "
        "WHERE closed = 1 AND outcome_prices_json IS NOT NULL").fetchall()
    for token_yes, token_no, prices_json in rows:
        try:
            prices = json.loads(prices_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(prices, list) or len(prices) < 2:
            continue
        try:
            yes_payout = float(prices[0])
            no_payout = float(prices[1])
        except (TypeError, ValueError):
            continue
        if token_yes:
            out[str(token_yes)] = yes_payout
        if token_no:
            out[str(token_no)] = no_payout
    return out


def score_unresolved(conn, *, batch: int = 500) -> int:
    """Score whale_positions whose market has since resolved. Returns count scored."""
    token_payout = _resolved_token_map(conn)
    if not token_payout:
        return 0
    rows = conn.execute(
        "SELECT id, token_id, side FROM whale_positions "
        "WHERE outcome IS NULL ORDER BY id LIMIT ?", (int(batch),)).fetchall()
    scored = 0
    for pid, token_id, side in rows:
        payout = token_payout.get(str(token_id))
        if payout is None:
            continue  # market not resolved yet (or token not ours)
        side = str(side or "BUY").upper()
        # BUY a winning token (payout 1) => correct; SELL a losing token => correct
        won = payout >= 0.5
        correct = won if side != "SELL" else (not won)
        outcome = 1.0 if correct else -1.0
        record_resolution(conn, pid, outcome=outcome, pnl=0.0)
        scored += 1
    return scored


def promote_wallets(conn, *, min_calibration: float, min_resolved: int) -> int:
    """Flip untracked wallets that clear the bar to tracked=1. Returns count promoted."""
    rows = conn.execute(
        "SELECT wallet FROM whale_wallets "
        "WHERE tracked = 0 AND n_resolved >= ? AND calibration >= ?",
        (int(min_resolved), float(min_calibration))).fetchall()
    for (wallet,) in rows:
        track_wallet(conn, wallet, tracked=True)
        log.info("whale_scorer: promoted %s (calibration cleared bar)", wallet)
    return len(rows)


def demote_wallets(conn, *, min_calibration: float, min_resolved: int) -> int:
    """Drop tracked wallets that have fallen below the bar after more resolutions."""
    rows = conn.execute(
        "SELECT wallet FROM whale_wallets "
        "WHERE tracked = 1 AND n_resolved >= ? AND calibration < ?",
        (int(min_resolved), float(min_calibration))).fetchall()
    for (wallet,) in rows:
        track_wallet(conn, wallet, tracked=False)
        log.info("whale_scorer: demoted %s (calibration fell below bar)", wallet)
    return len(rows)


async def whale_scorer_task(conn, settings) -> None:
    """Periodically score resolved positions and re-evaluate the follow list."""
    if not getattr(settings, "polygon_rpc_url", ""):
        # No chain feed => no positions to score. Idle-beat so the monitor is happy.
        while True:
            beat(conn, "whale_scorer", "disabled")
            await asyncio.sleep(max(60, settings.heartbeat_interval))

    poll_s = float(getattr(settings, "whale_score_poll_s", 600.0))
    min_cal = float(getattr(settings, "whale_promote_calibration", 0.60))
    min_res = int(getattr(settings, "whale_promote_min_resolved", 8))
    hb = float(getattr(settings, "heartbeat_interval", 15))

    log.info("whale_scorer: promote at calibration>=%.2f over >=%d resolved",
             min_cal, min_res)
    while True:
        try:
            scored = score_unresolved(conn)
            promoted = promote_wallets(conn, min_calibration=min_cal,
                                       min_resolved=min_res)
            demoted = demote_wallets(conn, min_calibration=min_cal,
                                     min_resolved=min_res)
            detail = f"scored={scored}; promoted={promoted}; demoted={demoted}"
            if scored or promoted or demoted:
                log.info("whale_scorer: %s", detail)
            # idle-beat across the long poll so the monitor doesn't flag stale
            slept = 0.0
            while slept < poll_s:
                beat(conn, "whale_scorer", detail + f"; next ~{int(poll_s - slept)}s")
                nap = min(hb, poll_s - slept)
                await asyncio.sleep(nap)
                slept += nap
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("whale_scorer pass failed; retrying in 60s")
            await asyncio.sleep(60.0)
