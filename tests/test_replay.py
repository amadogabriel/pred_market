"""Tests for event-log replay helpers."""
from __future__ import annotations

import json

import pytest

from pm.backtest.replay import iter_event_records, summarize_events


def test_summarize_events_counts_topics(tmp_path):
    day = tmp_path / "2026-06-12"
    day.mkdir()
    path = day / "events.jsonl"
    path.write_text(
        "\n".join([
            json.dumps({"ts": 1.0, "topic": "book", "payload": {"asset_id": "A"}}),
            json.dumps({"ts": 2.0, "topic": "system", "payload": {"what": "connected"}}),
            json.dumps({"ts": 3.0, "topic": "book", "payload": {"asset_id": "B"}}),
        ]) + "\n",
        encoding="utf-8",
    )

    summary = summarize_events(tmp_path)
    assert summary.files == 1
    assert summary.events == 3
    assert summary.first_ts == 1.0
    assert summary.last_ts == 3.0
    assert summary.topics == {"book": 2, "system": 1}


def test_iter_event_records_rejects_missing_keys(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text(json.dumps({"topic": "book"}) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing ts/topic/payload"):
        list(iter_event_records([path]))
