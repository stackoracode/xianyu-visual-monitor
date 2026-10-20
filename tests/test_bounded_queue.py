import asyncio

import pytest

from xianyu_monitor.queueing.bounded import BoundedQueue


async def test_block_policy_applies_backpressure_without_dropping():
    q: BoundedQueue[int] = BoundedQueue("t", 2, "block")
    await q.put(1)
    await q.put(2)
    producer = asyncio.create_task(q.put(3))
    await asyncio.sleep(0.01)
    assert not producer.done() and q.stats.blocked == 1
    assert await q.get() == 1
    await asyncio.wait_for(producer, 1)
    assert [await q.get(), await q.get()] == [2, 3]
    assert q.stats.dropped == 0 and q.stats.high_water == 2


async def test_drop_oldest_counts_drops():
    q: BoundedQueue[int] = BoundedQueue("diag", 1, "drop_oldest")
    await q.put(1)
    await q.put(2)
    assert q.stats.dropped == 1 and await q.get() == 2


def test_invalid_configuration():
    with pytest.raises(ValueError):
        BoundedQueue("x", 0)
    with pytest.raises(ValueError):
        BoundedQueue("x", 1, "drop_newest")
