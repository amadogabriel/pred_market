"""S6 news-signal scanner — research signal (never executable).

Subscribes to `news_article` events on the bus. For each headline:

1. Match to one or more markets via `pm.news.matcher.build_index`.
2. Determine direction via lexicon polarity.
3. Apply a Bayesian update to the *current market mid* (`pm.news.bayesian.update`).
4. If the absolute edge is meaningful and the strong/weak threshold is met,
   emit one ResearchSignal per matched market.

The signal carries:
- the matched market's token_yes leg with BUY/SELL inferred from direction
- features: headline, feed, posterior, prior, edge, polarity, overlap score

Index is rebuilt every `index_refresh_s` seconds from the active universe.

Fail-closed: exec_sets=0; strategy 'news' is absent from
PM_EXECUTION_STRATEGIES by default.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass

from pm.core.books import BookStore
from pm.execution.fee_engine import FeeEngine
from pm.news.bayesian import update as bayesian_update
from pm.news.matcher import build_index, direction as headline_direction, match_headline
from pm.signals.common import Debouncer, ResearchSignal, mid_price

log = logging.getLogger(__name__)

STRATEGY = "news"
STRONG_POLARITY_THRESHOLD = 2   # >= 2 polarity words in (head + summary) = strong


@dataclass
class NewsSignalScanner:
    books: BookStore
    fees: FeeEngine
    conn: sqlite3.Connection
    debounce_s: float = 300.0
    min_overlap: int = 2
    top_k: int = 3
    index_refresh_s: float = 300.0
    stale_after: float = 30.0
    venue: str = "polymarket"

    def __post_init__(self) -> None:
        self.debounce = Debouncer(self.debounce_s)
        self._index: dict[str, set[str]] = {}
        self._meta: dict[str, dict] = {}
        self._last_refresh: float = 0.0

    def refresh_index(self) -> None:
        rows = self.conn.execute(
            "SELECT market_id, question, category, token_yes, token_no, neg_risk_id "
            "FROM markets WHERE active = 1 AND closed = 0").fetchall()
        markets = [dict(zip(
            ("market_id", "question", "category", "token_yes", "token_no", "neg_risk_id"),
            r)) for r in rows]
        self._index, self._meta = build_index(markets)
        self._last_refresh = time.time()
        log.info("news_signal: index refreshed (%d tokens, %d markets)",
                 len(self._index), len(self._meta))

    def on_article(self, payload: dict) -> list[ResearchSignal]:
        now = time.time()
        if not self._index or now - self._last_refresh > self.index_refresh_s:
            self.refresh_index()

        title = str(payload.get("title", ""))
        summary = str(payload.get("summary", ""))
        if not title:
            return []
        candidates = match_headline(self._index, title, summary,
                                    min_overlap=self.min_overlap, top_k=self.top_k)
        if not candidates:
            return []

        pol = headline_direction(title, summary)
        if pol == 0:
            return []
        # Strong = lexicon hits >= threshold across head+summary
        from pm.news.matcher import POSITIVE_WORDS, NEGATIVE_WORDS
        text_low = (title + " " + summary).lower()
        n_hits = (sum(1 for w in POSITIVE_WORDS if w in text_low) +
                  sum(1 for w in NEGATIVE_WORDS if w in text_low))
        strong = n_hits >= STRONG_POLARITY_THRESHOLD

        out: list[ResearchSignal] = []
        for market_id, overlap in candidates:
            meta = self._meta.get(market_id)
            if not meta:
                continue
            token_yes = meta.get("token_yes")
            if not token_yes:
                continue
            mid = mid_price(self.books.peek(token_yes), self.stale_after)
            if mid is None:
                continue
            upd = bayesian_update(mid, pol, strong=strong)
            if not upd.fired:
                continue
            side = "BUY" if upd.edge > 0 else "SELL"
            if not self.debounce.ready(f"news:{market_id}", now):
                continue
            category = meta.get("category")
            per_share_fee = self.fees.taker_fee(self.venue, category, mid, 1.0)
            leg = {"token_id": token_yes, "market_id": market_id,
                   "side": side, "price": round(mid, 4), "size": 0.0}
            out.append(ResearchSignal(
                strategy=STRATEGY, kind="headline_match", group_id=market_id,
                legs=[leg],
                gross_edge=abs(upd.edge), fees=per_share_fee,
                net_edge=max(0.0, abs(upd.edge) - per_share_fee),
                features={"prior": upd.prior, "posterior": upd.posterior,
                          "edge": upd.edge, "polarity": pol, "strong": strong,
                          "overlap": overlap,
                          "title": title[:240],
                          "feed": payload.get("feed"),
                          "category": category,
                          "lr": upd.likelihood_ratio}))
        return out
