"""Minimal async RSS poller. Pure stdlib + aiohttp; no feedparser dependency.

We parse a subset of RSS 2.0 and Atom 1.0 enough to extract:

    title, link, pubDate (or updated), guid (or id), description (or summary)

Each feed has its own poll cadence stored in the config. The poller maintains
a per-feed cursor (last-seen guid set) to avoid republishing items. Cursors
are kept in-memory; restart re-emits the most-recent window once. Items are
published on the bus as T_NEWS events.

Feeds are configured in `config/news_feeds.yaml` (a simple list); the format:

    - name: reuters_business
      url: https://feeds.reuters.com/reuters/businessNews
      poll_s: 90
    - name: bbc_world
      url: http://feeds.bbci.co.uk/news/world/rss.xml
      poll_s: 120

If the file doesn't exist, the poller logs a warning and exits — news is
intentionally fail-closed too: no config, no news ingest, no signals fired.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import yaml

from pm.core.bus import Bus
from pm.core.db import beat
from pm.core.events import Event

log = logging.getLogger(__name__)

T_NEWS = "news_article"  # local topic; engine extends ALL_TOPICS with this


@dataclass
class FeedItem:
    feed: str
    guid: str
    title: str
    link: str
    pub_ts: float
    summary: str


def parse_feed(xml_text: str, *, feed_name: str) -> list[FeedItem]:
    """Best-effort parse of RSS 2.0 or Atom 1.0. Returns items in feed order."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    items: list[FeedItem] = []
    # RSS 2.0: <rss><channel><item>...
    for it in root.iter("item"):
        items.append(_item_rss(it, feed_name))
    # Atom 1.0: <feed><entry>...
    ns = "{http://www.w3.org/2005/Atom}"
    for e in root.iter(f"{ns}entry"):
        items.append(_item_atom(e, feed_name, ns))
    return [i for i in items if i.title]


def _item_rss(it: ET.Element, feed_name: str) -> FeedItem:
    title = (it.findtext("title") or "").strip()
    link = (it.findtext("link") or "").strip()
    guid = (it.findtext("guid") or link or title).strip()
    pub = (it.findtext("pubDate") or "").strip()
    summary = (it.findtext("description") or "").strip()
    return FeedItem(feed=feed_name, guid=guid, title=title, link=link,
                    pub_ts=_parse_date(pub), summary=_strip_html(summary))


def _item_atom(e: ET.Element, feed_name: str, ns: str) -> FeedItem:
    title = (e.findtext(f"{ns}title") or "").strip()
    link_el = e.find(f"{ns}link")
    link = (link_el.get("href") if link_el is not None else "") or ""
    guid = (e.findtext(f"{ns}id") or link or title).strip()
    pub = (e.findtext(f"{ns}updated") or e.findtext(f"{ns}published") or "").strip()
    summary = (e.findtext(f"{ns}summary") or "").strip()
    return FeedItem(feed=feed_name, guid=guid, title=title, link=link,
                    pub_ts=_parse_date(pub), summary=_strip_html(summary))


_HTML_RE = re.compile(r"<[^>]+>")


def _strip_html(text: str) -> str:
    return _HTML_RE.sub(" ", text).strip()


def _parse_date(s: str) -> float:
    """Parse RFC822 (RSS) or ISO 8601 (Atom). Returns wallclock seconds; 0 on failure."""
    if not s:
        return 0.0
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(s).timestamp()
    except (TypeError, ValueError):
        pass
    try:
        from datetime import datetime
        # ISO 8601, e.g. 2026-06-12T09:34:00Z
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0.0


def load_feeds(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text) or []
    return list(data)


async def rss_poller_task(bus: Bus, conn, settings) -> None:
    """Poll all configured feeds; publish new items on the bus."""
    feeds_path = Path(getattr(settings, "news_feeds_yaml", "config/news_feeds.yaml"))
    feeds = load_feeds(feeds_path)
    if not feeds:
        log.info("rss_poller: no feeds in %s; news ingestion disabled", feeds_path)
        while True:
            beat(conn, "rss_poller", "disabled")
            await asyncio.sleep(max(60, settings.heartbeat_interval))

    seen: dict[str, set[str]] = {}
    next_poll: dict[str, float] = {}
    log.info("rss_poller: %d feeds configured", len(feeds))

    timeout = aiohttp.ClientTimeout(total=20.0)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                now = time.time()
                for feed in feeds:
                    name = str(feed["name"])
                    if now < next_poll.get(name, 0):
                        continue
                    poll_s = float(feed.get("poll_s", 120))
                    next_poll[name] = now + poll_s
                    await _poll_one(session, feed, seen, bus)
                beat(conn, "rss_poller", f"feeds={len(feeds)}")
                await asyncio.sleep(min(15.0, max(5.0, settings.heartbeat_interval)))
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — never let the loop die
                log.exception("rss_poller pass failed; retrying in 30s")
                await asyncio.sleep(30.0)


async def _poll_one(session: aiohttp.ClientSession, feed: dict,
                    seen: dict[str, set[str]], bus: Bus) -> None:
    name = str(feed["name"])
    url = str(feed["url"])
    try:
        async with session.get(url, headers={"User-Agent": "pm-system/0.1"}) as resp:
            if resp.status != 200:
                return
            text = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return
    items = parse_feed(text, feed_name=name)
    bucket = seen.setdefault(name, set())
    first_run = not bucket
    for it in items:
        if it.guid in bucket:
            continue
        bucket.add(it.guid)
        if first_run:
            continue  # don't flood the bus on initial backfill
        bus.publish(Event(T_NEWS, {
            "feed": it.feed, "guid": it.guid, "title": it.title,
            "link": it.link, "pub_ts": it.pub_ts,
            "summary": it.summary[:1000]}))
    # bound memory
    if len(bucket) > 5000:
        # keep last 2500 by insertion order (sets aren't ordered, so just halve)
        seen[name] = set(list(bucket)[-2500:])
