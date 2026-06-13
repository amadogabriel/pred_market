"""Tests for the news pipeline: RSS parsing, matching, Bayesian update."""
from __future__ import annotations

from pm.news.bayesian import MIN_EDGE, update
from pm.news.matcher import (NEGATIVE_WORDS, POSITIVE_WORDS, build_index,
                              direction, match_headline, tokenize)
from pm.news.rss import parse_feed


# ---------- tokenizer / matcher ----------

def test_tokenize_drops_stopwords_and_short_tokens():
    toks = tokenize("The Fed raises rates in the US")
    assert "the" not in toks
    assert "in" not in toks
    assert "fed" in toks
    assert "raises" in toks
    assert "rates" in toks


def test_build_index_returns_token_to_market_map():
    markets = [
        {"market_id": "M1", "question": "Will the Fed raise rates in March?"},
        {"market_id": "M2", "question": "Bitcoin above $100k by year end?"},
    ]
    idx, meta = build_index(markets)
    assert "fed" in idx and "M1" in idx["fed"]
    assert "bitcoin" in idx and "M2" in idx["bitcoin"]
    assert meta["M1"]["question"].startswith("Will")


def test_match_headline_picks_overlap():
    markets = [
        {"market_id": "M1", "question": "Will the Fed raise rates in March?"},
        {"market_id": "M2", "question": "Bitcoin above $100k by year end?"},
    ]
    idx, _ = build_index(markets)
    out = match_headline(idx, "Fed raises rates this March",
                         min_overlap=2)
    assert any(mid == "M1" for mid, _ in out)


def test_match_headline_requires_min_overlap():
    markets = [{"market_id": "M1", "question": "Will the Fed raise rates in March?"}]
    idx, _ = build_index(markets)
    out = match_headline(idx, "Bitcoin rallies", min_overlap=2)
    assert out == []


def test_direction_positive():
    assert direction("Fed approves rate hike") == 1


def test_direction_negative():
    assert direction("Fed rejects proposal") == -1


def test_direction_neutral():
    assert direction("Fed officials meet to discuss") == 0


# ---------- Bayesian update ----------

def test_update_fires_on_strong_positive():
    u = update(0.50, direction=1, strong=True)
    assert u.fired is True
    assert u.posterior > u.prior
    assert u.edge >= MIN_EDGE


def test_update_fires_on_strong_negative():
    u = update(0.50, direction=-1, strong=True)
    assert u.fired is True
    assert u.posterior < u.prior
    assert u.edge <= -MIN_EDGE


def test_update_no_fire_on_zero_direction():
    u = update(0.50, direction=0)
    assert u.fired is False
    assert u.edge == 0.0


def test_update_at_boundary_returns_no_change():
    u = update(0.0, direction=1)
    assert u.fired is False
    assert u.posterior == 0.0


def test_weak_positive_smaller_than_strong():
    weak = update(0.50, direction=1, strong=False)
    strong = update(0.50, direction=1, strong=True)
    assert strong.edge > weak.edge > 0


# ---------- RSS feed parser ----------

RSS_SAMPLE = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Test Feed</title>
    <item>
      <title>Fed cuts interest rates</title>
      <link>https://example.com/1</link>
      <guid>g1</guid>
      <pubDate>Tue, 10 Jun 2026 12:00:00 GMT</pubDate>
      <description>The central bank announced a 25bp cut.</description>
    </item>
    <item>
      <title>Bitcoin above $100k</title>
      <link>https://example.com/2</link>
      <guid>g2</guid>
      <pubDate>Tue, 10 Jun 2026 13:00:00 GMT</pubDate>
      <description>BTC crossed the threshold.</description>
    </item>
  </channel>
</rss>
"""


def test_parse_rss_returns_items():
    items = parse_feed(RSS_SAMPLE, feed_name="test")
    assert len(items) == 2
    assert items[0].title == "Fed cuts interest rates"
    assert items[0].guid == "g1"
    assert items[0].pub_ts > 0


ATOM_SAMPLE = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Test Atom</title>
  <entry>
    <title>BTC rallies on news</title>
    <id>urn:1</id>
    <link href="https://example.com/a1"/>
    <updated>2026-06-10T12:00:00Z</updated>
    <summary>Markets respond positively.</summary>
  </entry>
</feed>
"""


def test_parse_atom_returns_items():
    items = parse_feed(ATOM_SAMPLE, feed_name="atom")
    assert len(items) == 1
    assert items[0].title == "BTC rallies on news"


def test_parse_garbage_returns_empty():
    assert parse_feed("not xml at all", feed_name="x") == []
