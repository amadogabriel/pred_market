"""Event-log replay utilities.

The Phase 0 event log is the source of truth for later backtests. These
helpers keep replay work structured and testable instead of parsing JSONL in
one-off scripts.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


@dataclass(frozen=True)
class ReplaySummary:
    files: int
    events: int
    first_ts: float | None
    last_ts: float | None
    topics: dict[str, int]


def event_files(events_dir: Path) -> list[Path]:
    return sorted(Path(events_dir).glob("*/events.jsonl"))


def iter_event_records(paths: list[Path]) -> Iterator[dict]:
    for path in paths:
        with path.open("r", encoding="utf-8") as fh:
            for line_no, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"{path}:{line_no}: invalid JSON: {exc}") from exc
                if not {"ts", "topic", "payload"} <= set(rec):
                    raise ValueError(f"{path}:{line_no}: missing ts/topic/payload")
                yield rec


def summarize_events(events_dir: Path, *, max_events: int | None = None) -> ReplaySummary:
    paths = event_files(events_dir)
    topics: Counter[str] = Counter()
    first_ts: float | None = None
    last_ts: float | None = None
    n = 0

    for rec in iter_event_records(paths):
        ts = float(rec["ts"])
        first_ts = ts if first_ts is None else min(first_ts, ts)
        last_ts = ts if last_ts is None else max(last_ts, ts)
        topics[str(rec["topic"])] += 1
        n += 1
        if max_events is not None and n >= max_events:
            break

    return ReplaySummary(
        files=len(paths),
        events=n,
        first_ts=first_ts,
        last_ts=last_ts,
        topics=dict(sorted(topics.items())),
    )
