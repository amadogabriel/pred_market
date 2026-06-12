"""Time-and-token-matched random baseline for signal-outcome comparison.

For every real signal at time t on token T with encoded direction d, we
generate a paired synthetic baseline where d is drawn uniformly from
{BUY, SELL} and the forward outcome is computed the same way the labeler
does — only using the event-log mid series instead of live books.

The point is to know what hit rate a *random* signal at the same times
on the same tokens would have. If the realised signal's H_cond is
indistinguishable from this baseline, the directional encoding is
uninformative no matter how plausible the hypothesis was.

A second baseline (`contrarian`) flips the realised signal's encoded side
and labels with the same outcome direction convention. If contrarian
outperforms the realised signal, the convention is backwards (the
trade-through case).
"""
from __future__ import annotations

import bisect
import json
import random
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pm.backtest.replay import event_files, iter_event_records

MID_SAMPLE_S = 5.0
DEFAULT_HORIZON = 900.0


@dataclass
class BaselineOutcomes:
    real: list[float]
    random: list[float]
    contrarian: list[float]


def build_mid_series(events_dir: Path, *, max_events: int | None = None
                     ) -> dict[str, tuple[list[float], list[float]]]:
    """Walk the event log once and produce per-token (ts, mid) sparse series."""
    series: dict[str, tuple[list[float], list[float]]] = {}
    last_bid: dict[str, float] = {}
    last_ask: dict[str, float] = {}
    n = 0
    for rec in iter_event_records(event_files(events_dir)):
        if max_events is not None and n >= max_events:
            break
        n += 1
        topic = rec.get("topic")
        payload = rec.get("payload") or {}
        token = payload.get("asset_id")
        if not token or topic not in ("book", "price_change"):
            continue
        bids = payload.get("bids") or []
        asks = payload.get("asks") or []
        bb = _best_price(bids, want_max=True)
        ba = _best_price(asks, want_max=False)
        if bb is not None:
            last_bid[token] = bb
        if ba is not None:
            last_ask[token] = ba
        if token in last_bid and token in last_ask:
            mid = (last_bid[token] + last_ask[token]) / 2
            ts_list, mid_list = series.setdefault(token, ([], []))
            ts = float(rec["ts"])
            if not ts_list or ts - ts_list[-1] >= MID_SAMPLE_S:
                ts_list.append(ts)
                mid_list.append(mid)
    return series


def _best_price(levels: list, *, want_max: bool) -> float | None:
    if not levels:
        return None
    try:
        prices = [float(lvl["price"]) for lvl in levels]
    except (KeyError, TypeError, ValueError):
        return None
    return max(prices) if want_max else min(prices)


def _forward_mid(series: dict, token: str, after_ts: float) -> float | None:
    entry = series.get(token)
    if not entry:
        return None
    ts_list, mid_list = entry
    i = bisect.bisect_left(ts_list, after_ts)
    if i >= len(ts_list):
        return None
    return mid_list[i]


def compare_to_baseline(conn: sqlite3.Connection, events_dir: Path, *,
                        strategy: str, kind: str,
                        horizon_s: float = DEFAULT_HORIZON,
                        seed: int = 42) -> BaselineOutcomes:
    """For a given (strategy,kind), build paired real/random/contrarian outcomes."""
    series = build_mid_series(events_dir)
    rng = random.Random(seed)

    rows = conn.execute(
        "SELECT signal_id, ts, legs_json FROM signal_log "
        "WHERE strategy=? AND kind=? ORDER BY signal_id",
        (strategy, kind)).fetchall()

    real: list[float] = []
    random_: list[float] = []
    contra: list[float] = []
    for row in rows:
        legs = _safe_legs(row["legs_json"])
        if not legs:
            continue
        ts = float(row["ts"])
        # Same labeling math as pm.signals.labeler, but sourced from event-log mids
        per_real, per_rand, per_contra = [], [], []
        for leg in legs:
            token = str(leg.get("token_id"))
            try:
                price = float(leg["price"])
            except (KeyError, TypeError, ValueError):
                continue
            fwd = _forward_mid(series, token, ts + horizon_s)
            if fwd is None:
                continue
            side = str(leg.get("side", "NA")).upper()
            real_o = (fwd - price) if side != "SELL" else (price - fwd)
            rand_side = "BUY" if rng.random() < 0.5 else "SELL"
            rand_o = (fwd - price) if rand_side != "SELL" else (price - fwd)
            flip = "BUY" if side == "SELL" else "SELL"
            contra_o = (fwd - price) if flip != "SELL" else (price - fwd)
            per_real.append(real_o)
            per_rand.append(rand_o)
            per_contra.append(contra_o)
        if len(per_real) * 2 < len(legs):
            continue
        real.append(sum(per_real) / len(per_real))
        random_.append(sum(per_rand) / len(per_rand))
        contra.append(sum(per_contra) / len(per_contra))
    return BaselineOutcomes(real=real, random=random_, contrarian=contra)


def _safe_legs(legs_json: str | None) -> list[dict]:
    if not legs_json:
        return []
    try:
        legs = json.loads(legs_json)
    except (ValueError, TypeError):
        return []
    return legs if isinstance(legs, list) else []
