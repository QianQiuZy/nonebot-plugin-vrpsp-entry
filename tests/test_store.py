import asyncio
import json
from datetime import timedelta

from nonebot_plugin_vrpsp_entry.config import Config
from nonebot_plugin_vrpsp_entry.models import EPOCH, Entry, RedPocket
from nonebot_plugin_vrpsp_entry.store import CONSUMER_GROUP, Store


async def new_entry(store, offset=0, room=1820703922, uid=1203217682):
    stamp = await store.time_us()
    return Entry(room, uid, EPOCH + timedelta(microseconds=stamp + offset))


async def new_pocket(store, kind="battery", amount=100, offset=0, room=1820703922):
    stamp = await store.time_us()
    return RedPocket(room, kind, amount, EPOCH + timedelta(microseconds=stamp + offset))


async def test_pocket_subscriptions_update_only_their_type_and_keep_entry_data(store):
    await store.subscribe("10", "room", 1820703922, "1")
    original = (await store.subscriptions("10"))["10:room:1820703922"]
    assert await store.subscribe_pocket("10", "battery", 100, "1") == 1
    first = (await store.subscriptions("10"))["10:pocket_battery:0"]
    assert await store.subscribe_pocket("10", "battery", 100, "2") == 0
    assert (await store.subscriptions("10"))["10:pocket_battery:0"] == first
    assert await store.subscribe_pocket("10", "battery", 200, "1") == 2
    second = (await store.subscriptions("10"))["10:pocket_battery:0"]
    assert second["min_price"] == "200" and second["token"] != first["token"]
    assert await store.subscribe_pocket("10", "gift", 520, "1") == 1
    assert await store.unsubscribe_pocket("10", "battery")
    assert not await store.unsubscribe_pocket("10", "battery")
    rows = await store.subscriptions("10")
    assert "10:pocket_gift:0" in rows and "10:pocket_battery:0" not in rows
    assert rows["10:room:1820703922"] == original
    assert await store.subscriptions("11") == {}


async def test_pocket_enqueue_checks_price_cutoff_and_duplicates_atomically(store):
    before = await new_pocket(store)
    await store.subscribe_pocket("10", "battery", 100, "1")
    assert not await store.enqueue_pocket("10", before, "old", "1")
    assert not await store.enqueue_pocket("10", await new_pocket(store, amount=99), "low", "1")
    pocket = await new_pocket(store)
    results = await asyncio.gather(
        *(store.enqueue_pocket("10", pocket, "new", "1") for _ in range(10))
    )
    assert sum(results) == 1
    message_id, task = (await store.read(">"))[0]
    assert len(task["matches"]) == 1
    assert int(task["expires_us"]) == pocket.time_us + 600_000_000
    assert await store.has_matching_subscription(task)
    assert await store.subscribe_pocket("10", "battery", 0, "1") == 2
    assert not await store.has_matching_subscription(task)
    await store.finish(message_id, task, "cancelled")
    assert not await store.enqueue_pocket("10", pocket, "do not revive", "1")


async def test_pocket_enqueue_rechecks_threshold_changed_after_poll_snapshot(store):
    await store.subscribe_pocket("10", "gift", 100, "1")
    await store.subscribe_pocket("10", "gift", 520, "1")
    pocket = await new_pocket(store, kind="gift", amount=519)
    assert not await store.enqueue_pocket("10", pocket, "below new threshold", "1")
    qualifying = await new_pocket(store, kind="gift", amount=520)
    assert await store.enqueue_pocket("10", qualifying, "equal threshold", "1")


async def test_pocket_expired_future_and_restart_subscription_persistence(store, redis_url):
    await store.subscribe_pocket("10", "guard", 0, "1")
    await store.subscribe_pocket("10", "battery", 100, "1")
    assert not await store.enqueue_pocket(
        "10", await new_pocket(store, offset=-600_000_000), "expired"
    )
    assert not await store.enqueue_pocket(
        "10", await new_pocket(store, offset=100_000_000), "future"
    )
    restarted = Store(Config(vrpsp_entry_redis_url=redis_url, vrpsp_entry_key_prefix=store.prefix))
    try:
        assert await restarted.subscriptions("10") == await store.subscriptions("10")
        assert await restarted.unsubscribe_pocket("10", "guard")
        assert "10:pocket_guard:0" not in await store.subscriptions("10")
        assert "10:pocket_battery:0" in await store.subscriptions("10")
    finally:
        await restarted.close()


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
    assert await store.has_matching_subscription(task)
    await store.unsubscribe("10", "room", entry.room_id)
    assert await store.has_matching_subscription(task)
    await store.unsubscribe("10", "user", entry.uid)
    await store.subscribe("10", "user", entry.uid, "2")
    assert not await store.has_matching_subscription(task)


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
