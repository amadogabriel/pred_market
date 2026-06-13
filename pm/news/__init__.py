"""News ingestion + contract-matching pipeline.

Primary sources are public RSS feeds (Reuters, AP, BBC, government releases).
X (Twitter) requires an API key and is plugged in via the same matcher
interface; this module ships with RSS only — adding an X poller is a
single-file extension that publishes the same `news_article` events.

Pipeline:

    rss_poller_task  →  bus(news_article)  →  match_to_contracts  →  signal

Latency budget (per the strategy report, target sub-2s):
    ingest:  0.0 - 0.3s  (RSS HEAD + body)
    parse:   0.3 - 0.6s  (keyword index + entity tag)
    match:   0.6 - 0.9s  (contract lookup)
    signal:  0.9 - 1.2s  (Bayesian update + emit)
    execute: 1.2 - 1.8s  (broker round-trip when enabled)

We do not hit this latency without a paid feed (RSS poll cadence is the
bottleneck, often 60s minimum to be polite to source servers). The
*architecture* is ready for sub-2s feeds; the *practical* latency on free
RSS is closer to 30-60s. The strategy report calls this honestly:

    "A simple keyword-to-contract mapping that fires in 2 seconds beats a
     sophisticated transformer that fires in 30 seconds."

We are firing in 30 seconds. That puts us at parity with most other free-tier
operators on the venue.
"""
