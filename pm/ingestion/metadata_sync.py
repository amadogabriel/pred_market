"""Market-universe metadata sync.

Pulls the active market universe from Polymarket's Gamma REST API on startup
and then every `settings.metadata_sync_interval` seconds, persists it to the
`markets` table, extracts NegRisk partition groups, stores resolution-rules
text (alerting on change), and pushes the tracked CLOB token universe to the
WS consumer.

Gamma field names drift; `_extract` is intentionally tolerant and falls back
across the documented aliases. Inspect a live response and extend the alias
lists here if a field stops populating.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any

import aiohttp

from pm.core.books import BookStore
from pm.core.db import beat, store_rules, upsert_market
from pm.ingestion.ws_polymarket import PolymarketWS

log = logging.getLogger(__name__)

# We fetch events ordered by liquidity descending and stop here. The full
# active universe is ~10k+ markets across thousands of events, but we only ever
# track the top `track_top_n` most liquid markets; partition groups can only
# fire when all their legs are tracked, so the illiquid tail beyond this cap
# can't produce signals. Events are liquidity-ordered, so a few hundred cover
# every tracked market and every group that could fire; this keeps the startup
# sync to ~10s and avoids blocking the event loop on tens of thousands of
# synchronous upserts (which can starve the WS keepalive and force reconnects).
MAX_EVENTS = 400
MAX_CLOSED_REFRESH = 500
CLOSED_REFRESH_CONCURRENCY = 10

# Gamma categories we know how to price (must match keys in config/fees.yaml).
KNOWN_CATEGORIES = {
    "geopolitics", "sports", "politics", "finance", "tech", "mentions",
    "economics", "culture", "weather", "crypto", "other",
}

# Common Gamma category labels -> fee-engine categories.
CATEGORY_ALIASES = {
    "politics": "politics",
    "us-current-affairs": "politics",
    "elections": "politics",
    "geopolitics": "geopolitics",
    "world": "geopolitics",
    "crypto": "crypto",
    "cryptocurrency": "crypto",
    "sports": "sports",
    "nfl": "sports", "nba": "sports", "soccer": "sports", "mlb": "sports",
    "business": "finance",
    "finance": "finance",
    "markets": "finance",
    "economy": "economics",
    "economics": "economics",
    "tech": "tech",
    "technology": "tech",
    "ai": "tech",
    "science": "tech",
    "pop-culture": "culture",
    "culture": "culture",
    "entertainment": "culture",
    "mentions": "mentions",
    "weather": "weather",
    "climate": "weather",
}


def _first(d: dict[str, Any], *keys: str, default: Any = None) -> Any:
    """Return the first present, non-None value among `keys`."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def normalize_category(raw: Any) -> str | None:
    """Lowercase/strip a Gamma category or tag and map to a fee-engine category.

    Unknown categories return None so the fee engine falls back to default_rate.
    """
    if not raw:
        return None
    if isinstance(raw, (list, tuple)):
        for item in raw:
            mapped = normalize_category(item)
            if mapped is not None:
                return mapped
        return None
    if isinstance(raw, dict):  # tag objects like {"label": "Politics", "slug": "politics"}
        return normalize_category(_first(raw, "slug", "label", "name"))
    token = str(raw).strip().lower()
    if not token:
        return None
    if token in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[token]
    if token in KNOWN_CATEGORIES:
        return token
    return None


def _parse_token_ids(raw: Any) -> list[str]:
    """clobTokenIds arrives as a JSON-encoded string or a real list."""
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return []
    if isinstance(raw, (list, tuple)):
        return [str(t) for t in raw if t]
    return []


def _as_float(v: Any) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _json_text(raw: Any) -> str | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        try:
            json.loads(raw)
            return raw
        except (TypeError, ValueError):
            return json.dumps(raw)
    return json.dumps(raw)


def _category_from_tags(tags: Any) -> str | None:
    """Map an event's tag list to a fee-engine category (first match wins).

    Tags are [{"label","slug"}, ...]; we try the slug then the label of each.
    """
    for t in tags or []:
        if isinstance(t, dict):
            mapped = normalize_category(t.get("slug") or t.get("label"))
        else:
            mapped = normalize_category(t)
        if mapped is not None:
            return mapped
    return None


def _event_group_id(event: dict[str, Any]) -> str | None:
    """NegRisk partition group id for an event, or None if it isn't NegRisk.

    The event is the mutually-exclusive partition. We key on the canonical
    on-chain `negRiskMarketID`, falling back to the event `id`. (TASKS.md
    guessed a market-level `negRiskMarketId`; live data has it null — the
    grouping lives on the event.)
    """
    if not (event.get("negRisk") or event.get("enableNegRisk")):
        return None
    gid = event.get("negRiskMarketID") or event.get("id")
    return str(gid) if gid else None


def parse_market(raw: dict[str, Any], *, category: str | None,
                 neg_risk_id: str | None, tags: Any = None) -> dict[str, Any] | None:
    """Normalize one nested Gamma market into our `markets` row shape.

    `category` and `neg_risk_id` are supplied by the parent event (that's where
    they live on Gamma). Returns None for records we can't use.
    """
    market_id = _first(raw, "conditionId", "condition_id", "id")
    if not market_id:
        return None
    tokens = _parse_token_ids(_first(raw, "clobTokenIds", "clob_token_ids", "tokens"))
    if len(tokens) < 2:
        return None  # need a yes/no token pair to be tradable
    neg_risk = bool(_first(raw, "negRisk", "neg_risk", default=False)) or bool(neg_risk_id)
    return {
        "market_id": str(market_id),
        "venue": "polymarket",
        "question": _first(raw, "question", "title"),
        "slug": _first(raw, "slug"),
        "category": category,
        "tags_json": json.dumps(tags) if tags else None,
        "end_date": _first(raw, "endDate", "end_date"),
        "active": 1 if _first(raw, "active", default=True) else 0,
        "closed": 1 if _first(raw, "closed", default=False) else 0,
        "accepting_orders": 1 if _first(raw, "acceptingOrders", "accepting_orders",
                                        default=True) else 0,
        "outcome_prices_json": _json_text(_first(raw, "outcomePrices", "outcome_prices")),
        "resolution_status": _first(raw, "umaResolutionStatus", "resolutionStatus",
                                    "resolution_status"),
        "closed_time": _first(raw, "closedTime", "closed_time", "umaEndDate"),
        "neg_risk": 1 if neg_risk else 0,
        "neg_risk_id": neg_risk_id,
        "token_yes": tokens[0],
        "token_no": tokens[1],
        "liquidity": _as_float(_first(raw, "liquidity", "liquidityNum", "liquidityClob")),
        "volume_24h": _as_float(_first(raw, "volume24hr", "volume24hrClob", "volumeNum")),
        "_rules": _first(raw, "description", "resolutionSource", "rules"),
    }


async def _fetch_events(session: aiohttp.ClientSession, settings) -> list[dict[str, Any]]:
    """Fetch active/open events (ordered by liquidity desc) with nested markets.

    Events are the right granularity: each carries its tags (→ category) and
    NegRisk grouping, and nests its markets. Gamma caps `offset` around 10k and
    422s past it, so ordering by liquidity means the illiquid tail is dropped.
    """
    url = f"{settings.pm_gamma_rest.rstrip('/')}/events"
    out: list[dict[str, Any]] = []
    offset = 0
    page = 100  # Gamma hard-caps `limit` at 100
    while True:
        params = {"active": "true", "closed": "false", "limit": str(page),
                  "offset": str(offset), "order": "liquidity", "ascending": "false"}
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 422:  # offset past Gamma's pagination ceiling
                log.info("metadata_sync: reached Gamma pagination ceiling at offset=%d", offset)
                break
            resp.raise_for_status()
            data = await resp.json()
        batch = data.get("data", []) if isinstance(data, dict) else data
        if not batch:
            break
        out.extend(batch)
        offset += len(batch)
        if offset >= MAX_EVENTS:
            break
    return out


async def _fetch_market_by_slug(session: aiohttp.ClientSession, settings,
                                slug: str) -> dict[str, Any] | None:
    url = f"{settings.pm_gamma_rest.rstrip('/')}/markets/slug/{slug}"
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
        if resp.status == 404:
            return None
        resp.raise_for_status()
        data = await resp.json()
    return data if isinstance(data, dict) else None


async def _refresh_past_end_markets(conn, session: aiohttp.ClientSession,
                                    settings) -> int:
    """Refresh stored markets that dropped out of the active/open event feed."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT market_id, slug, category, tags_json, neg_risk_id FROM markets "
        "WHERE closed=0 AND slug IS NOT NULL AND end_date IS NOT NULL AND end_date <= ? "
        "ORDER BY COALESCE(liquidity,0) DESC LIMIT ?",
        (now_iso, MAX_CLOSED_REFRESH),
    ).fetchall()
    if not rows:
        return 0

    sem = asyncio.Semaphore(CLOSED_REFRESH_CONCURRENCY)

    async def refresh(row) -> int:
        async with sem:
            try:
                raw = await _fetch_market_by_slug(session, settings, row["slug"])
            except (aiohttp.ClientError, asyncio.TimeoutError):
                log.warning("metadata_sync: failed to refresh closed candidate %s",
                            row["slug"], exc_info=True)
                return 0
        if not raw:
            return 0
        try:
            tags = json.loads(row["tags_json"]) if row["tags_json"] else None
        except (TypeError, ValueError):
            tags = None
        event = (raw.get("events") or [{}])[0]
        category = row["category"] or _category_from_tags(event.get("tags"))
        neg_risk_id = row["neg_risk_id"] or _event_group_id(event)
        market = parse_market(raw, category=category, neg_risk_id=neg_risk_id, tags=tags)
        if market is None:
            return 0
        upsert_market(conn, market)
        return 1

    refreshed = await asyncio.gather(*(refresh(row) for row in rows))
    count = sum(refreshed)
    if count:
        log.info("metadata_sync: refreshed %d past-end markets", count)
    return count


async def sync_markets(conn, books: BookStore, ws: PolymarketWS, settings,
                       neg_risk_groups: dict[str, list[dict]] | None = None) -> int:
    """Run one full metadata sync pass. Returns the number of markets upserted."""
    async with aiohttp.ClientSession() as session:
        raw_events = await _fetch_events(session, settings)
        closed_refreshed = await _refresh_past_end_markets(conn, session, settings)

    parsed: list[dict] = []
    all_groups: dict[str, list[dict]] = {}
    rules_changed = 0
    seen: set[str] = set()

    for event in raw_events:
        if not isinstance(event, dict):
            continue
        category = _category_from_tags(event.get("tags"))
        group_id = _event_group_id(event)
        tags = event.get("tags")
        for raw in event.get("markets") or []:
            m = parse_market(raw, category=category, neg_risk_id=group_id, tags=tags)
            if m is None or m["market_id"] in seen:
                continue
            seen.add(m["market_id"])
            rules_md = m.pop("_rules", None)
            upsert_market(conn, m)
            parsed.append(m)

            if rules_md and store_rules(conn, m["market_id"], "polymarket", str(rules_md)):
                rules_changed += 1
                log.warning("resolution rules changed for market %s (%s)",
                            m["market_id"], m.get("question"))

            if m["neg_risk_id"]:
                all_groups.setdefault(m["neg_risk_id"], []).append({
                    "token_yes": m["token_yes"],
                    "market_id": m["market_id"],
                    "category": m["category"],
                })

    # Partition scanning only makes sense for groups with >= 2 legs.
    groups = {gid: legs for gid, legs in all_groups.items() if len(legs) >= 2}
    if neg_risk_groups is not None:
        neg_risk_groups.clear()
        neg_risk_groups.update(groups)

    # Bound the WS universe: the active universe is thousands of markets, but we
    # only track the most liquid `track_top_n` above the liquidity floor (one WS
    # connection per `ws_assets_per_conn` tokens). Partition signals fire only
    # for groups whose legs land in this tracked set — acceptable in Phase 0.
    eligible = [m for m in parsed if (m["liquidity"] or 0.0) >= settings.min_liquidity_usd]
    eligible.sort(key=lambda m: m["liquidity"] or 0.0, reverse=True)
    tracked = eligible[:settings.track_top_n]
    dropped = len(eligible) - len(tracked)
    tracked_tokens: list[str] = []
    for m in tracked:
        tracked_tokens.append(m["token_yes"])
        tracked_tokens.append(m["token_no"])

    ws.set_assets(tracked_tokens)
    if dropped > 0:
        log.info("metadata_sync: tracking top %d of %d eligible markets (%d dropped by track_top_n)",
                 len(tracked), len(eligible), dropped)
    beat(conn, "metadata_sync",
         f"markets={len(parsed)} tracked={len(tracked)} tokens={len(tracked_tokens)} "
         f"groups={len(groups)} rules_changed={rules_changed} "
         f"closed_refreshed={closed_refreshed}")
    log.info("metadata_sync: %d markets, %d tracked (%d tokens), %d NegRisk groups, "
             "%d rules changed, %d past-end refreshed",
             len(parsed), len(tracked), len(tracked_tokens), len(groups),
             rules_changed, closed_refreshed)
    return len(parsed)


async def metadata_sync_loop(conn, books: BookStore, ws: PolymarketWS, settings,
                             neg_risk_groups: dict[str, list[dict]] | None = None) -> None:
    """Run sync_markets on startup, then every settings.metadata_sync_interval seconds.

    The sync itself runs hourly, but we keep beating every heartbeat_interval
    while idle (convention: every long-running task beats that often) so the
    monitor doesn't flag this component stale between syncs.
    """
    backoff = 5.0
    while True:
        try:
            await sync_markets(conn, books, ws, settings, neg_risk_groups)
            backoff = 5.0
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — never let the loop die
            log.exception("metadata_sync pass failed; retrying in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300.0)
            continue

        waited = 0.0
        while waited < settings.metadata_sync_interval:
            step = min(settings.heartbeat_interval, settings.metadata_sync_interval - waited)
            await asyncio.sleep(step)
            waited += step
            beat(conn, "metadata_sync",
                 f"idle; next sync in ~{int(settings.metadata_sync_interval - waited)}s")
