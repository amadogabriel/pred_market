"""Append-only event log writer.

Subscribes to every bus topic and appends each event, one JSON object per
line, to `events_dir/YYYY-MM-DD/events.jsonl` (UTC date). This is the
replay/backtest dataset, so it must start before anything else publishes and
must not lose data across crashes: each line is flushed immediately, and the
file is opened in append mode so a restart continues the same day's file.

Rotation is lazy: when the UTC date rolls over we simply open the next day's
file; the previous one is left intact.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from pm.core.bus import Bus
from pm.core.events import ALL_TOPICS

log = logging.getLogger(__name__)


def _utc_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def event_logger_task(bus: Bus, events_dir: Path) -> None:
    """Drain the bus and append every event to the current UTC day's JSONL file."""
    events_dir = Path(events_dir)
    queue = bus.subscribe(*ALL_TOPICS)

    current_date: str | None = None
    fh = None
    try:
        while True:
            event = await queue.get()
            day = _utc_date()
            if day != current_date:
                if fh is not None:
                    fh.close()
                day_dir = events_dir / day
                day_dir.mkdir(parents=True, exist_ok=True)
                # append mode + line buffering: restart-safe, crash-resistant.
                fh = (day_dir / "events.jsonl").open("a", encoding="utf-8", buffering=1)
                current_date = day
                log.info("event_logger: writing to %s", day_dir / "events.jsonl")
            fh.write(json.dumps(event.to_record(), separators=(",", ":")) + "\n")
            fh.flush()
    except asyncio.CancelledError:
        raise
    finally:
        if fh is not None:
            fh.close()
