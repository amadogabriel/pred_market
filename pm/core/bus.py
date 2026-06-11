"""In-process pub/sub bus.

One engine process, many task-group consumers. Bounded queues so one stuck
consumer can't OOM the box: on overflow we drop the OLDEST item for lossy
topics (market data — a fresher snapshot supersedes it anyway) and count
drops, but never drop signals or system events (those queues are larger and
overflow there is a hard error we want to crash on, loudly).
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from pm.core.events import Event, T_EXECUTION, T_SIGNAL, T_SYSTEM

log = logging.getLogger(__name__)

LOSSLESS_TOPICS = {T_SIGNAL, T_EXECUTION, T_SYSTEM}


class Bus:
    def __init__(self, maxsize: int = 5000, lossless_maxsize: int = 50000) -> None:
        self._subs: dict[str, list[asyncio.Queue[Event]]] = defaultdict(list)
        self._maxsize = maxsize
        self._lossless_maxsize = lossless_maxsize
        self.drops: dict[str, int] = defaultdict(int)

    def subscribe(self, *topics: str) -> asyncio.Queue[Event]:
        size = self._lossless_maxsize if set(topics) & LOSSLESS_TOPICS else self._maxsize
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=size)
        for t in topics:
            self._subs[t].append(q)
        return q

    def publish(self, event: Event) -> None:
        for q in self._subs.get(event.topic, ()):  # no subscribers -> no-op
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                if event.topic in LOSSLESS_TOPICS:
                    raise RuntimeError(
                        f"lossless queue full for topic {event.topic}; consumer wedged")
                try:
                    q.get_nowait()  # drop oldest
                except asyncio.QueueEmpty:
                    pass
                q.put_nowait(event)
                self.drops[event.topic] += 1
                if self.drops[event.topic] % 1000 == 1:
                    log.warning("bus dropping on topic=%s (total=%d)",
                                event.topic, self.drops[event.topic])
