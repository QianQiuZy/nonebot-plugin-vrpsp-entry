import asyncio
import json
from datetime import timedelta

from nonebot_plugin_vrpsp_entry.config import Config
from nonebot_plugin_vrpsp_entry.models import EPOCH, Entry
from nonebot_plugin_vrpsp_entry.store import CONSUMER_GROUP, Store


async def new_entry(store, offset=0, room=1820703922, uid=1203217682):
    stamp = await store.time_us()
    return Entry(room, uid, EPOCH + timedelta(microseconds=stamp + offset))


async def test_overlap_and_concurrent_duplicate_enqueue_once_per_group(store):
    await store.subscribe("10", "room", 1820703922, "1")
    await store.subscribe("10", "user", 1203217682, "1")
    await store.subscribe("11", "user", 1203217682, "1")
    entry = await new_entry(store)
    results = await asyncio.gather(*(store.enqueue("10", entry, "message") for _ in range(10)))
    assert sum(results) == 1
    assert await store.enqueue("11", entry, "message")
    rows = await store.read(">")
    assert len(rows) == 2
    assert len(rows[0][1]["matches"]) == 2
    assert await store.redis.ttl(store.prefix + "seen:" + entry.delivery_key("10")) > 600


async def test_cutoff_and_reentry_and_idempotent_subscribe(store):
    old = await new_entry(store, offset=-1_000_000)
    assert await store.subscribe("10", "user", old.uid, "1")
    first = await store.subscriptions("10")
    assert not await store.subscribe("10", "user", old.uid, "2")
    assert first == await store.subscriptions("10")
    assert not await store.enqueue("10", old, "old")
    fresh = await new_entry(store)
    again = Entry(fresh.room_id, fresh.uid, fresh.when + timedelta(milliseconds=1))
    assert await store.enqueue("10", fresh, "new")
    assert await store.enqueue("10", again, "reentry")
    assert len(await store.read(">")) == 2


async def test_cancel_only_discards_task_when_all_matches_removed(store):
    await store.subscribe("10", "room", 1820703922, "1")
    await store.subscribe("10", "user", 1203217682, "2")
    entry = await new_entry(store)
    await store.enqueue("10", entry, "message")
    _, task = (await store.read(">"))[0]
    assert await store.matching_bots(task) == ["1", "2"]
    await store.unsubscribe("10", "room", entry.room_id)
    assert await store.matching_bots(task) == ["2"]
    await store.unsubscribe("10", "user", entry.uid)
    await store.subscribe("10", "user", entry.uid, "2")
    assert await store.matching_bots(task) == []


async def test_restart_recovers_pending_and_ack_cleans_queue(store, redis_url):
    await store.subscribe("10", "room", 1820703922, "1")
    entry = await new_entry(store)
    await store.enqueue("10", entry, "message")
    first = await store.read(">")
    message_id, task = first[0]
    await store.defer(message_id, 100)
    await store.release()
    other = Store(Config(vrpsp_entry_redis_url=redis_url, vrpsp_entry_key_prefix=store.prefix))
    try:
        await other.ensure()
        assert await other.acquire()
        assert await other.read("0") == first
        assert not await other.retry_due(message_id, 101)
        assert await other.retry_due(message_id, 102)
        assert await other.finish(message_id, task, "sent")
        assert await other.redis.xlen(other.outbox) == 0
        assert (await other.redis.xpending(other.outbox, CONSUMER_GROUP))["pending"] == 0
        assert not await other.redis.hgetall(other.retry)
        assert not await other.enqueue("10", entry, "message")
        assert await other.subscriptions("10")
    finally:
        await other.release()
        await other.close()


async def test_exclusive_lease_and_stale_owner_cannot_mutate_outbox(store, redis_url):
    other = Store(Config(vrpsp_entry_redis_url=redis_url, vrpsp_entry_key_prefix=store.prefix))
    try:
        assert not await other.acquire()
        await other.release()
        assert await store.renew()
        await store.subscribe("10", "user", 1203217682, "1")
        entry = await new_entry(store)
        assert not await other.enqueue("10", entry, "message")
        await store.release()
        assert await other.acquire()
        assert not await store.renew()
        assert not await store.enqueue("10", entry, "message")
        assert await other.enqueue("10", entry, "message")
    finally:
        await other.release()
        await other.close()


async def test_group_listing_and_isolation(store):
    await store.redis.set("unrelated:sentinel", "keep")
    await store.subscribe("10", "user", 1, "1")
    await store.subscribe("100", "room", 2, "1")
    assert list(await store.subscriptions("10")) == ["10:user:1"]
    await store.unsubscribe("10", "user", 1)
    assert await store.subscriptions("10") == {}
    assert await store.redis.get("unrelated:sentinel") == "keep"
    assert int(json.loads((await store.redis.hgetall(store.subs))["100:room:2"])["since_us"]) > 0
