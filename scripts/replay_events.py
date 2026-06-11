"""Summarize event-log replay data."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config.settings import Settings  # noqa: E402
from pm.backtest.replay import ReplaySummary, summarize_events  # noqa: E402


def _fmt_ts(ts: float | None) -> str:
    if ts is None:
        return "n/a"
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def _as_dict(summary: ReplaySummary) -> dict:
    return {
        "files": summary.files,
        "events": summary.events,
        "first_ts": summary.first_ts,
        "first_utc": _fmt_ts(summary.first_ts),
        "last_ts": summary.last_ts,
        "last_utc": _fmt_ts(summary.last_ts),
        "topics": summary.topics,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize pm-system event logs.")
    parser.add_argument("--events-dir", type=Path, default=Settings().events_dir)
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    summary = summarize_events(args.events_dir, max_events=args.max_events)
    data = _as_dict(summary)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        print(f"files:      {data['files']}")
        print(f"events:     {data['events']}")
        print(f"first UTC:  {data['first_utc']}")
        print(f"last UTC:   {data['last_utc']}")
        print("topics:")
        for topic, count in data["topics"].items():
            print(f"  {topic:<18} {count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
