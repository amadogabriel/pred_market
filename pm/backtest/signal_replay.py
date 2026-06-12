"""Offline research-signal replay over the JSONL event log.

Rebuilds order books from logged `book` / `price_change` events, drives the S2
microstructure and S3 relative-value scanners with an event-time clock, and
evaluates every emitted signal against forward mids taken from the SAME log —
no live soak needed to tune thresholds.

Limitations (research harness, not a fill simulator):
- outcomes are mid-drift over the horizon: no spread crossing, no impact;
- books never go stale in replay (staleness is wall-clock), so a token that
  stops trading mid-log keeps its last mid;
- market metadata (categories, NegRisk groups) comes from the state DB as it
  is NOW, not as it was when the events were logged.
"""
from __future__ import annotations

import bisect
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from statistics import fmean, median

from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine
from pm.backtest.replay import event_files, iter_event_records
from pm.signals.common import ResearchSignal, mid_price
from pm.signals.microstructure import MicrostructureTracker
from pm.signals.momentum import MomentumTracker
from pm.signals.relative_value import RelativeValueMonitor

log = logging.getLogger(__name__)

MID_SAMPLE_S = 5.0  # per-token mid series resolution for forward returns


@dataclass
class KindStats:
    n: int
    labeled: int
    hit_rate: float | None
    avg_outcome: float | None
    median_outcome: float | None


@dataclass
class ReplayResult:
    events: int
    signals: list[tuple[float, ResearchSignal]] = field(default_factory=list)
    stats: dict[tuple[str, str], KindStats] = field(default_factory=dict)


def load_market_index(conn: sqlite3.Connection) -> tuple[dict[str, dict], dict[str, list[dict]]]:
    """(token -> market meta, group_id -> legs_meta) from the state DB."""
    index: dict[str, dict] = {}
    groups: dict[str, list[dict]] = {}
    for r in conn.execute(
            "SELECT market_id, category, neg_risk_id, token_yes, token_no FROM markets"):
        meta = {"market_id": r["market_id"], "category": r["category"],
                "neg_risk_id": r["neg_risk_id"], "token_yes": r["token_yes"],
                "token_no": r["token_no"]}
        if r["token_yes"]:
            index[r["token_yes"]] = meta
        if r["token_no"]:
            index[r["token_no"]] = meta
        if r["neg_risk_id"] and r["token_yes"]:
            groups.setdefault(r["neg_risk_id"], []).append({
                "token_yes": r["token_yes"], "market_id": r["market_id"],
                "category": r["category"]})
    groups = {g: legs for g, legs in groups.items() if len(legs) >= 2}
    return index, groups


class _EventClock:
    """Mutable event-time clock injected into the scanners."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def replay_signals(events_dir: Path, conn: sqlite3.Connection, fees: FeeEngine, *,
                   horizon_s: float = 900.0, stale_after: float = 30.0,
                   micro_kwargs: dict | None = None,
                   rv_kwargs: dict | None = None,
                   mom_kwargs: dict | None = None,
                   max_events: int | None = None) -> ReplayResult:
    index, groups = load_market_index(conn)
    books = BookStore()
    clock = _EventClock()
    micro = MicrostructureTracker(books, fees, stale_after=stale_after,
                                  clock=clock, **(micro_kwargs or {}))
    rv = RelativeValueMonitor(books, fees, stale_after=stale_after,
                              clock=clock, **(rv_kwargs or {}))
    mom = MomentumTracker(books, fees, stale_after=stale_after,
                          clock=clock, **(mom_kwargs or {}))

    signals: list[tuple[float, ResearchSignal]] = []
    # per-token sparse mid series for forward returns: token -> ([ts], [mid])
    series: dict[str, tuple[list[float], list[float]]] = {}

    n = 0
    for rec in iter_event_records(event_files(events_dir)):
        topic = rec["topic"]
        if topic not in ("book", "price_change", "last_trade_price"):
            continue
        payload = rec["payload"]
        ts = float(rec["ts"])
        clock.now = ts
        n += 1
        if max_events is not None and n > max_events:
            break

        if topic in ("book", "price_change"):
            books.handle_ws_message(payload)

        asset_id = payload.get("asset_id")
        meta = index.get(asset_id) if asset_id else None
        if meta is None:
            continue

        # sample mid series for the updated token
        m = mid_price(books.peek(asset_id), stale_after)
        if m is not None:
            ts_list, mid_list = series.setdefault(asset_id, ([], []))
            if not ts_list or ts - ts_list[-1] >= MID_SAMPLE_S:
                ts_list.append(ts)
                mid_list.append(m)

        if topic == "last_trade_price":
            out = micro.on_trade(payload, meta)
        else:
            out = micro.on_book_update(asset_id, meta)
            out += rv.on_market_update(meta)
            out += mom.on_book_update(asset_id, meta)
            gid = meta.get("neg_risk_id")
            if gid and gid in groups:
                out += rv.on_group_update(gid, groups[gid])
        for sig in out:
            signals.append((ts, sig))

    result = ReplayResult(events=n, signals=signals)
    result.stats = _evaluate(signals, series, horizon_s)
    return result


def _forward_mid(series: dict, token: str, after_ts: float) -> float | None:
    entry = series.get(token)
    if not entry:
        return None
    ts_list, mid_list = entry
    i = bisect.bisect_left(ts_list, after_ts)
    if i >= len(ts_list):
        return None
    return mid_list[i]


def _evaluate(signals: list[tuple[float, ResearchSignal]], series: dict,
              horizon_s: float) -> dict[tuple[str, str], KindStats]:
    """Same labeling convention as pm.signals.labeler, sourced from the log."""
    outcomes: dict[tuple[str, str], list[float]] = {}
    counts: dict[tuple[str, str], int] = {}

    for ts, sig in signals:
        key = (sig.strategy, sig.kind)
        counts[key] = counts.get(key, 0) + 1
        per_leg: list[float] = []
        for leg in sig.legs:
            fwd = _forward_mid(series, str(leg.get("token_id")), ts + horizon_s)
            if fwd is None:
                continue
            try:
                price = float(leg["price"])
            except (KeyError, TypeError, ValueError):
                continue
            side = str(leg.get("side", "NA")).upper()
            per_leg.append(price - fwd if side == "SELL" else fwd - price)
        if len(per_leg) * 2 < len(sig.legs):
            continue
        outcomes.setdefault(key, []).append(sum(per_leg) / len(per_leg))

    stats: dict[tuple[str, str], KindStats] = {}
    for key, n in counts.items():
        outs = outcomes.get(key, [])
        if outs:
            stats[key] = KindStats(
                n=n, labeled=len(outs),
                hit_rate=sum(1 for o in outs if o > 0) / len(outs),
                avg_outcome=fmean(outs), median_outcome=median(outs))
        else:
            stats[key] = KindStats(n=n, labeled=0, hit_rate=None,
                                   avg_outcome=None, median_outcome=None)
    return stats
