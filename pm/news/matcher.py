"""Match news headlines to tracked Polymarket contracts.

We use a two-stage approach. First, a fast keyword index built from market
questions (lower-cased, deduplicated word stems). A headline is a *candidate*
match for a market if it shares enough rare tokens — common stopwords don't
count, but proper nouns and category-specific keywords do.

Second, an optional sentiment polarity (positive/negative wording) maps to
a directional update. Without a sophisticated NLP model we use a curated
small lexicon — "wins", "loses", "raises", "cuts", "halts" etc. Per the
strategy report, simple-and-fast beats sophisticated-and-slow on this venue.

This module exposes:

    build_index(markets)  →  dict[token -> set[market_id]]
    match_headline(idx, headline, summary)  →  list[(market_id, score)]
    direction(headline)   →  +1 / 0 / -1

It is pure stdlib (re + collections). The Bayesian update lives in
`bayesian.py`. The signal scanner that wraps everything is
`pm/signals/news_signal.py`.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

# Tokens shorter than 3 chars or in this list don't contribute to matching.
STOPWORDS = {
    "the", "a", "an", "of", "to", "in", "on", "for", "at", "by", "with", "as",
    "is", "are", "was", "were", "be", "been", "being", "or", "and", "but",
    "if", "than", "this", "that", "these", "those", "from", "into", "over",
    "under", "after", "before", "during", "between", "about", "via", "per",
    "says", "say", "said", "new", "old", "us", "uk", "eu", "u.s.", "u.s",
}

POSITIVE_WORDS = {
    "wins", "won", "victory", "approves", "approved", "passes", "passed",
    "raises", "raised", "hikes", "hiked", "upgrades", "upgraded",
    "confirms", "confirmed", "agrees", "agreed", "succeeds", "succeeded",
    "beats", "beat", "exceeds", "exceeded", "rallies", "rallied",
}
NEGATIVE_WORDS = {
    "loses", "lost", "loss", "defeat", "rejects", "rejected", "fails", "failed",
    "cuts", "cut", "drops", "dropped", "downgrades", "downgraded",
    "denies", "denied", "halts", "halted", "withdraws", "withdrew",
    "resigns", "resigned", "indicted", "convicted", "crashes", "crashed",
}


_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9']{2,}")


def tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")
            if t.lower() not in STOPWORDS]


def build_index(markets: list[dict]) -> tuple[dict[str, set[str]], dict[str, dict]]:
    """Build (token -> {market_id, ...}, market_id -> meta) from market rows.

    Each market row must have `market_id` and `question`. Optional: `category`,
    `token_yes`, `token_no`. Other keys are passed through into the meta map.
    """
    inv: dict[str, set[str]] = defaultdict(set)
    meta: dict[str, dict] = {}
    for m in markets:
        mid = m.get("market_id")
        question = m.get("question") or ""
        if not mid or not question:
            continue
        meta[mid] = dict(m)
        for tok in set(tokenize(question)):
            inv[tok].add(mid)
    return dict(inv), meta


def match_headline(index: dict[str, set[str]], headline: str, summary: str = "",
                   *, min_overlap: int = 2, top_k: int = 5) -> list[tuple[str, int]]:
    """Find markets whose question shares ≥ min_overlap rare tokens with the article.

    Score is the count of overlapping tokens. Returns the top-k by score, ties
    broken by market_id for stability.
    """
    toks = tokenize(headline) + tokenize(summary)
    if not toks:
        return []
    counts: Counter = Counter()
    for tok in set(toks):
        for mid in index.get(tok, ()):
            counts[mid] += 1
    candidates = [(mid, n) for mid, n in counts.items() if n >= min_overlap]
    candidates.sort(key=lambda x: (-x[1], x[0]))
    return candidates[:top_k]


def direction(headline: str, summary: str = "") -> int:
    """Crude lexicon-based polarity. +1, -1, or 0."""
    text = (headline + " " + summary).lower()
    pos = sum(1 for w in POSITIVE_WORDS if w in text)
    neg = sum(1 for w in NEGATIVE_WORDS if w in text)
    if pos == neg:
        return 0
    return 1 if pos > neg else -1
