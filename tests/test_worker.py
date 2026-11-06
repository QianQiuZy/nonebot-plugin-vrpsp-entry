import gzip
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import Bot

from nonebot_plugin_vrpsp_entry import sending
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
    bot = MagicMock(spec=Bot)
    bot.send_group_msg = AsyncMock(return_value={"message_id": 1})
    instance.bots = lambda: {"1": bot}
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


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("tagged", [False, True])
async def test_replacement_bot_discards_backlog_but_uses_existing_subscriptions(
    worker, store, pending, tagged
):
    await store.subscribe("10", "room", 1820703922, "old_bot")
    await store.subscribe("10", "user", 1203217682, "old_bot")
    original = await store.subscriptions("10")
    assert not await store.subscribe("10", "room", 1820703922, "new_bot")
    entry = await new_entry(store)
    await store.enqueue("10", entry, "legacy message", "old_bot" if tagged else None)
    if pending:
        message_id, task = (await store.read(">"))[0]
        assert {match["bot_id"] for match in task["matches"]} == {"old_bot"}
        # 跨号清理不等待尚未到期的重试退避。
        await store.defer(message_id, (await store.time_us()) / 1_000_000)

    bot = MagicMock(spec=Bot)
    bot.send_group_msg = AsyncMock(return_value={"message_id": 100})
    worker.bots = lambda: {"new_bot": bot}
    await worker.send_once()
    bot.send_group_msg.assert_not_awaited()
    assert await store.redis.xlen(store.outbox) == 0
    assert not await store.redis.hgetall(store.retry)
    assert await store.subscriptions("10") == original
    assert await store.redis.get(store.prefix + "seen:" + entry.delivery_key("10")) == (
        "account_changed"
    )

    # 被丢弃的事件即使仍在上游缓存中，也不能重新入队补发。
    worker.client.fetch.return_value = [entry]
    await worker.poll_once()
    await worker.send_once()
    bot.send_group_msg.assert_not_awaited()

    # 换号后继续轮询，旧订阅的新任务使用新号，且重复轮询只发送一次。
    worker.client.fetch.return_value = [await new_entry(store)]
    await worker.poll_once()
    await worker.poll_once()
    _, task = (await store.read(">"))[0]
    assert {match["bot_id"] for match in task["matches"]} == {"new_bot"}
    await worker.send_once()
    bot.send_group_msg.assert_awaited_once()
    assert bot.send_group_msg.call_args.kwargs["group_id"] == 10
    assert await store.redis.xlen(store.outbox) == 0
    assert await store.subscriptions("10") == original


async def test_resubscribe_while_waiting_for_send_slot_does_not_revive_old_task(
    worker, store, monkeypatch
):
    await store.subscribe("10", "user", 1203217682, "old_bot")
    await store.enqueue("10", await new_entry(store), "message", "new_bot")
    bot = MagicMock(spec=Bot)
    bot.send_group_msg = AsyncMock(return_value={"message_id": 100})
    worker.bots = lambda: {"new_bot": bot}

    @asynccontextmanager
    async def changed_subscription():
        await store.unsubscribe("10", "user", 1203217682)
        await store.subscribe("10", "user", 1203217682, "new_bot")
        yield

    monkeypatch.setattr(sending.limiter, "slot", changed_subscription)
    await worker.send_once()
    bot.send_group_msg.assert_not_awaited()
    await worker.send_once()
    bot.send_group_msg.assert_not_awaited()
    assert await store.redis.xlen(store.outbox) == 0


@pytest.mark.parametrize("reconnect_id", ["new_bot", "other_bot"])
async def test_offline_polling_remembers_task_account(worker, store, reconnect_id):
    await store.subscribe("10", "user", 1203217682, "old_bot")
    bot = MagicMock(spec=Bot)
    bot.send_group_msg = AsyncMock(return_value={"message_id": 1})
    worker.bots = lambda: {"new_bot": bot}
    worker.client.fetch.return_value = []
    await worker.poll_once()

    worker.bots = lambda: {}
    entry = await new_entry(store)
    worker.client.fetch.return_value = [entry]
    await worker.poll_once()
    await worker.send_once()
    message_id, task = (await store.read("0"))[0]
    assert {match["bot_id"] for match in task["matches"]} == {"new_bot"}
    await store.defer(message_id, 0)

    # 模拟 NoneBot 重启后同号恢复或换号，归属由持久化的任务决定。
    worker.last_bot_id = None
    worker.bots = lambda: {reconnect_id: bot}
    await worker.send_once()
    assert bot.send_group_msg.await_count == (1 if reconnect_id == "new_bot" else 0)
    assert await store.redis.xlen(store.outbox) == 0
    assert not await store.redis.hgetall(store.retry)


async def test_poll_waits_for_first_bot_connection(worker, store):
    await store.subscribe("10", "user", 1203217682, "old_bot")
    worker.bots = lambda: {}
    await worker.poll_once()
    worker.client.fetch.assert_not_awaited()
    assert await store.redis.xlen(store.outbox) == 0


async def test_account_switch_while_waiting_for_send_slot_discards_task(worker, store, monkeypatch):
    await store.subscribe("10", "user", 1203217682, "old_bot")
    await store.enqueue("10", await new_entry(store), "message", "old_bot")
    old_bot, new_bot = MagicMock(spec=Bot), MagicMock(spec=Bot)
    old_bot.send_group_msg = AsyncMock(return_value={"message_id": 1})
    new_bot.send_group_msg = AsyncMock(return_value={"message_id": 2})
    worker.bots = lambda: {"old_bot": old_bot}

    @asynccontextmanager
    async def switched_account():
        worker.bots = lambda: {"new_bot": new_bot}
        yield

    monkeypatch.setattr(sending.limiter, "slot", switched_account)
    await worker.send_once()
    old_bot.send_group_msg.assert_not_awaited()
    new_bot.send_group_msg.assert_not_awaited()
    assert await store.redis.xlen(store.outbox) == 0


@pytest.mark.parametrize("pending", [False, True])
@pytest.mark.parametrize("age_seconds", [3599, 3600, 3601])
async def test_task_expires_one_hour_after_enqueue(
    worker, store, monkeypatch, pending, age_seconds
):
    await store.subscribe("10", "user", 1203217682, "1")
    entry = await new_entry(store)
    await store.enqueue("10", entry, "message")
    message_id, _ = (await store.redis.xrange(store.outbox))[0]
    queued_us = int(message_id.split("-", 1)[0]) * 1000
    now_us = queued_us + age_seconds * 1_000_000
    if pending:
        await store.read(">")
        # 过期任务必须跳过未来的重试时间；未过期任务正常发送。
        await store.defer(message_id, now_us / 1_000_000 if age_seconds >= 3600 else 0)
    monkeypatch.setattr(store, "time_us", AsyncMock(return_value=now_us))
    bot = worker.bots()["1"]
    await worker.send_once()
    assert bot.send_group_msg.await_count == (1 if age_seconds < 3600 else 0)
    assert await store.redis.xlen(store.outbox) == 0
    assert (await store.redis.xpending(store.outbox, "delivery"))["pending"] == 0
    assert not await store.redis.hgetall(store.retry)
    assert await store.redis.get(store.prefix + "seen:" + entry.delivery_key("10")) == (
        "sent" if age_seconds < 3600 else "expired"
    )
    assert not await store.enqueue("10", entry, "must not replay")


async def test_expired_task_is_cleaned_even_without_online_bot(worker, store, monkeypatch):
    await store.subscribe("10", "user", 1203217682, "1")
    await store.enqueue("10", await new_entry(store), "message")
    message_id, _ = (await store.read(">"))[0]
    now_us = int(message_id.split("-", 1)[0]) * 1000 + 3_600_000_000
    await store.defer(message_id, now_us / 1_000_000)
    worker.bots = lambda: {}
    monkeypatch.setattr(store, "time_us", AsyncMock(return_value=now_us))
    await worker.send_once()
    assert await store.redis.xlen(store.outbox) == 0
    assert not await store.redis.hgetall(store.retry)


async def test_task_expiring_while_waiting_for_send_slot_is_not_sent(worker, store, monkeypatch):
    await store.subscribe("10", "user", 1203217682, "1")
    await store.enqueue("10", await new_entry(store), "message")
    message_id, _ = (await store.redis.xrange(store.outbox))[0]
    queued_us = int(message_id.split("-", 1)[0]) * 1000
    clock = AsyncMock(return_value=queued_us + 3_599_000_000)
    monkeypatch.setattr(store, "time_us", clock)
    bot = worker.bots()["1"]

    @asynccontextmanager
    async def expired_while_waiting():
        clock.return_value = queued_us + 3_600_000_000
        yield

    monkeypatch.setattr(sending.limiter, "slot", expired_while_waiting)
    await worker.send_once()
    bot.send_group_msg.assert_not_awaited()
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
