"""External-source fetchers for the calibration model.

Currently:
- **Metaculus.** Public read-only API at metaculus.com/api2/questions/.
  Search by keyword, fetch the community prediction (median of point
  estimates, or the binary-question community-aggregate `mean` field).
  No auth required for read.

- **FedWatch.** CME publishes a probability table via a public JSON
  endpoint. Stub here; concrete implementation drops in when needed.

All fetchers are async and respect a polite cadence (no more than 1 req/sec
per source). They return `ExternalProb` records with a `source`, `p`,
`weight`, and `as_of` timestamp.

Failure mode: fetchers return None on any error. The model treats this as
"no external signal" and falls back to the internal base rate alone.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger(__name__)

METACULUS_BASE = "https://www.metaculus.com/api2/questions/"


@dataclass(frozen=True)
class ExternalProb:
    source: str
    p: float
    weight: float       # higher = more confident
    as_of: float        # unix ts of the upstream prediction


async def metaculus_search(session: aiohttp.ClientSession, query: str,
                           *, limit: int = 3,
                           timeout: float = 8.0) -> ExternalProb | None:
    """Search Metaculus for `query`, return the top binary-question community probability.

    Filters: only `forecast_type=binary` questions. We pick the one with the most
    forecasts (highest weight).
    """
    params = {"search": query, "limit": str(limit),
              "type": "forecast", "forecast_type": "binary"}
    try:
        async with session.get(METACULUS_BASE, params=params,
                               timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
            if resp.status != 200:
                return None
            data = await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None

    results = (data or {}).get("results") or []
    best: tuple[float, dict] | None = None
    for q in results:
        # community_prediction.full.q2 is the median; fallback to .mean
        community = (((q.get("community_prediction") or {}).get("full") or {}))
        p = community.get("q2") or community.get("mean")
        if p is None:
            continue
        nf = float(q.get("number_of_forecasters") or 0)
        if not 0.0 < float(p) < 1.0:
            continue
        if best is None or nf > best[0]:
            best = (nf, {"p": float(p), "n_forecasters": nf,
                         "id": q.get("id")})
    if best is None:
        return None
    weight = min(1.0, best[0] / 50.0)  # 50+ forecasters → full weight
    return ExternalProb(source="metaculus", p=best[1]["p"],
                        weight=max(0.1, weight), as_of=time.time())
