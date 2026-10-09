import gzip
import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import Bot

from nonebot_plugin_vrpsp_entry.client import Client
from nonebot_plugin_vrpsp_entry.config import Config
from nonebot_plugin_vrpsp_entry.models import Names, parse_entries
from nonebot_plugin_vrpsp_entry.worker import Worker

from .test_store import new_entry


@pytest.fixture
async def worker(store):
    instance = Worker(Config())
    await instance.store.close()
    instance.store = store
    instance.active.set()
    instance.client = AsyncMock()
    yield instance


async def test_source_failure_does_not_block_psp_and_old_future_rows_skipped(worker, store):
    await store.subscribe("10", "room", 1820703922, "1")
    await store.subscribe("10", "user", 1203217682, "1")
    valid = await new_entry(store)
    old = await new_entry(store, offset=-700_000_000)
    future = await new_entry(store, offset=100_000_000)

    async def fetch(source):
        if source == "vr":
            raise ValueError("not yet deployed")
        return [valid, old, future]

    worker.client.fetch.side_effect = fetch
    await worker.poll_once()
    await worker.poll_once()
    rows = await store.read(">")
    assert len(rows) == 1
    assert rows[0][1]["message"] == Names().message(valid)


async def test_cross_source_repeat_and_same_group_with_two_bots_sends_once(worker, store):
    await store.subscribe("10", "room", 1820703922, "1")
    await store.subscribe("10", "user", 1203217682, "2")
    entry = await new_entry(store)
    worker.client.fetch.return_value = [entry]
    first, second = MagicMock(spec=Bot), MagicMock(spec=Bot)
    first.send_group_msg = AsyncMock(return_value={"message_id": 1})
    second.send_group_msg = AsyncMock(return_value={"message_id": 2})
    worker.bots = lambda: {"1": first, "2": second}
    for _ in range(3):
        await worker.poll_once()
        await worker.send_once()
    first.send_group_msg.assert_awaited_once()
    second.send_group_msg.assert_not_awaited()
    args = first.send_group_msg.call_args.kwargs
    assert args["group_id"] == 10
    assert args["message"].extract_plain_text() == Names().message(entry)
    assert await store.redis.xlen(store.outbox) == 0


@pytest.mark.parametrize("result", [RuntimeError("offline"), {}, {"message_id": 0}])
async def test_send_failure_retains_pending_then_recovers(worker, store, result):
    await store.subscribe("10", "user", 1203217682, "1")
    entry = await new_entry(store)
    await store.enqueue("10", entry, "message")
    bot = MagicMock(spec=Bot)
    bot.send_group_msg = AsyncMock()
    if isinstance(result, Exception):
        bot.send_group_msg.side_effect = result
    else:
        bot.send_group_msg.return_value = result
    worker.bots = lambda: {"1": bot}
    await worker.send_once()
    assert await store.redis.xlen(store.outbox) == 1
    retry = await store.redis.hgetall(store.retry)
    assert len(retry) == 1
    await worker.send_once()  # 未到重试时刻
    assert bot.send_group_msg.await_count == 1
    for key, value in retry.items():
        data = json.loads(value)
        data["next"] = 0
        await store.redis.hset(store.retry, key, json.dumps(data))
    bot.send_group_msg.side_effect = None
    bot.send_group_msg.return_value = {"message_id": 100}
    await worker.send_once()
    assert bot.send_group_msg.await_count == 2
    assert await store.redis.xlen(store.outbox) == 0


async def test_bot_disconnected_retains_task_and_cancel_discards(worker, store):
    await store.subscribe("10", "user", 1203217682, "1")
    await store.enqueue("10", await new_entry(store), "message")
    worker.bots = lambda: {}
    await worker.send_once()
    assert await store.redis.xlen(store.outbox) == 1
    await store.unsubscribe("10", "user", 1203217682)
    for key in await store.redis.hkeys(store.retry):
        await store.redis.hset(store.retry, key, '{"attempts":1,"next":0}')
    await worker.send_once()
    assert await store.redis.xlen(store.outbox) == 0


async def test_large_pending_backlog_does_not_starve_fresh_messages(worker, store):
    await store.subscribe("10", "user", 1203217682, "1")
    for offset in range(110):
        await store.enqueue("10", await new_entry(store, offset=offset), f"{offset}")
    worker.bots = lambda: {}
    await worker.send_once()
    await worker.send_once()
    assert await store.redis.hlen(store.retry) == 110
    bot = MagicMock(spec=Bot)
    bot.send_group_msg = AsyncMock(return_value={"message_id": 3})
    worker.bots = lambda: {"1": bot}
    await store.enqueue("10", await new_entry(store, offset=1_000_000), "fresh")
    await worker.send_once()
    assert bot.send_group_msg.await_count == 1
    assert bot.send_group_msg.call_args.kwargs["message"].extract_plain_text() == "fresh"


async def test_http_contract_gzip_and_empty_response():
    config = Config(vrpsp_entry_sources={"psp"})
    client = Client(config)

    def respond(request):
        assert str(request.url) == "https://psp.qianqiuzy.cn/gift/entry?cache=1"
        body = json.dumps({"mode": "cache", "cache": 1, "items": []}).encode()
        return httpx.Response(
            200,
            content=gzip.compress(body),
            headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
        )

    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert await client.fetch("psp") == []
    finally:
        await client.close()


def test_text_segments_cannot_inject_cq_codes():
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    entry = parse_entries(
        {
            "mode": "cache",
            "cache": 1,
            "items": [{"room_id": 1, "uid": 2, "event_time": "2026-10-08T22:30:00+08:00"}],
        }
    )[0]
    names = Names()
    names.users[2] = "[CQ:at,qq=all]"
    message = Message(MessageSegment.text(names.message(entry)))
    assert len(message) == 1
    assert message[0].type == "text"
