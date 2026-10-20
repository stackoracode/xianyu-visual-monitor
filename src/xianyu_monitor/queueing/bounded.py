"""Bounded in-memory transport queue with an explicit backpressure policy.

In-memory queues are *transport only*. Anything that must survive a crash (notification and
reply intents) is written to MySQL before it is put on a queue, so a dropped or lost queue
item can always be recovered from the database.

Policies:

* ``block``       - producers wait for space (default; used for frames and jobs).
* ``drop_oldest`` - discard the oldest item; only allowed for re-creatable work such as
                    diagnostic frame writes. Every drop is counted and logged.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
log = logging.getLogger(__name__)


@dataclass
class QueueStats:
    put: int = 0
    got: int = 0
    dropped: int = 0
    blocked: int = 0
    high_water: int = 0


class BoundedQueue(Generic[T]):
    def __init__(self, name: str, maxsize: int, policy: str = "block"):
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        if policy not in {"block", "drop_oldest"}:
            raise ValueError(f"unknown policy {policy}")
        self.name = name
        self.policy = policy
        self._queue: asyncio.Queue[T] = asyncio.Queue(maxsize=maxsize)
        self.stats = QueueStats()

    @property
    def maxsize(self) -> int:
        return self._queue.maxsize

    def qsize(self) -> int:
        return self._queue.qsize()

    def free(self) -> int:
        return self._queue.maxsize - self._queue.qsize()

    async def put(self, item: T) -> None:
        if self._queue.full():
            if self.policy == "drop_oldest":
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                    self.stats.dropped += 1
                    log.warning("queue %s full; dropped oldest re-creatable item", self.name)
                except asyncio.QueueEmpty:  # pragma: no cover - race
                    pass
            else:
                self.stats.blocked += 1
                log.info("queue %s full; applying backpressure", self.name)
        await self._queue.put(item)
        self.stats.put += 1
        self.stats.high_water = max(self.stats.high_water, self._queue.qsize())

    async def get(self) -> T:
        item = await self._queue.get()
        self.stats.got += 1
        return item

    def task_done(self) -> None:
        self._queue.task_done()

    async def join(self) -> None:
        await self._queue.join()

    def snapshot(self) -> dict:
        return {"name": self.name, "size": self.qsize(), "maxsize": self.maxsize, "policy": self.policy,
                **self.stats.__dict__}
