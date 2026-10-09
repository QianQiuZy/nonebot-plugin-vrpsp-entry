import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from nonebot.adapters.onebot.v11 import Bot

from nonebot_plugin_vrpsp_entry import sending
from nonebot_plugin_vrpsp_entry.config import Config
from nonebot_plugin_vrpsp_entry.worker import Worker

from .test_store import new_entry


async def test_concurrent_messages_share_global_spacing_after_completion():
    records = []

    async def send(text):
        started = time.monotonic()
        await asyncio.sleep(0.01)
        records.append((text, started, time.monotonic()))

    # 模拟不同群各自的指令处理器，同时回复也必须串行。
    matchers = [MagicMock() for _ in range(4)]
    for matcher in matchers:
        matcher.send = AsyncMock(side_effect=send)
    await asyncio.gather(
        *(sending.send(matcher, f"group-{index}") for index, matcher in enumerate(matchers))
    )
    assert len(records) == 4
    for previous, current in zip(records, records[1:], strict=False):
        assert current[1] - previous[2] >= 0.3


async def test_group_notifications_batches_retries_and_replies_share_interval(store, monkeypatch):
    clock = [10.0]
    sleeps = []
    records = []

    async def advance(seconds):
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(sending, "monotonic", lambda: clock[0])
    monkeypatch.setattr(sending, "sleep", advance)
    worker = Worker(Config())
    await worker.store.close()
    worker.store = store
    bot = MagicMock(spec=Bot)
    failed = False

    async def notify(**kwargs):
        nonlocal failed
        group = kwargs["group_id"]
        records.append((group, clock[0]))
        if group == 11 and not failed:
            failed = True
            raise RuntimeError("temporary send failure")
        return {"message_id": len(records)}

    bot.send_group_msg = AsyncMock(side_effect=notify)
    worker.bots = lambda: {"1": bot}
    entry = await new_entry(store)
    for group in (10, 11, 12):
        await store.subscribe(str(group), "user", entry.uid, "1")
    entry = await new_entry(store)
    for group in (10, 11, 12):
        await store.enqueue(str(group), entry, "entry")
    await worker.send_once()

    matcher = MagicMock()

    async def reply(_):
        records.append(("reply", clock[0]))

    matcher.send = AsyncMock(side_effect=reply)
    await sending.send(matcher, "command reply")
    for message_id, value in (await store.redis.hgetall(store.retry)).items():
        retry = json.loads(value)
        retry["next"] = 0
        await store.redis.hset(store.retry, message_id, json.dumps(retry))
    await store.subscribe("13", "user", entry.uid, "1")
    await store.enqueue("13", await new_entry(store), "new batch")
    await worker.send_once()

    assert [group for group, _ in records] == [10, 11, 12, "reply", 11, 13]
    assert [stamp for _, stamp in records] == pytest.approx([10, 10.3, 10.6, 10.9, 11.2, 11.5])
    assert sleeps == pytest.approx([0.3] * 5)
    assert await store.redis.xlen(store.outbox) == 0


async def test_cancelled_waiter_does_not_block_following_messages(send_limiter, monkeypatch):
    clock = [10.0]
    entered_wait = asyncio.Event()

    monkeypatch.setattr(sending, "monotonic", lambda: clock[0])
    async with send_limiter.slot():
        pass

    async def blocked_sleep(_):
        entered_wait.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sending, "sleep", blocked_sleep)

    async def waiting_send():
        async with send_limiter.slot():
            pytest.fail("cancelled message must not send")

    task = asyncio.create_task(waiting_send())
    await asyncio.wait_for(entered_wait.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async def advance(seconds):
        clock[0] += seconds

    monkeypatch.setattr(sending, "sleep", advance)
    async with send_limiter.slot():
        assert clock[0] == pytest.approx(10.3)
