import json
import uuid

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from .config import Config
from .models import Entry

LEASE_SECONDS = 60
DEDUPE_SECONDS = 86400
CONSUMER_GROUP = "delivery"
CONSUMER = "sender"

RENEW = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('EXPIRE', KEYS[1], ARGV[2])
"""
RELEASE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""
SUBSCRIBE = """
if redis.call('HEXISTS', KEYS[1], ARGV[1]) == 1 then return 0 end
local clock = redis.call('TIME')
local sub = cjson.decode(ARGV[2])
sub.since_us = string.format('%.0f', tonumber(clock[1]) * 1000000 + tonumber(clock[2]))
redis.call('HSET', KEYS[1], ARGV[1], cjson.encode(sub))
return 1
"""
ENQUEUE = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
if redis.call('EXISTS', KEYS[3]) == 1 then return 0 end
local matches = {}
for index = 2, 3 do
    local value = redis.call('HGET', KEYS[2], ARGV[index])
    if value then
        local sub = cjson.decode(value)
        if tonumber(sub.since_us) <= tonumber(ARGV[4]) then
            table.insert(matches, {field=ARGV[index], token=sub.token, bot_id=sub.bot_id})
        end
    end
end
if #matches == 0 then return 0 end
local task = cjson.decode(ARGV[5])
task.matches = matches
redis.call('XADD', KEYS[4], '*', 'task', cjson.encode(task))
redis.call('SET', KEYS[3], 'queued', 'EX', ARGV[6])
return 1
"""
FINISH = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[4])
redis.call('XACK', KEYS[3], ARGV[5], ARGV[2])
redis.call('XDEL', KEYS[3], ARGV[2])
redis.call('HDEL', KEYS[4], ARGV[2])
return 1
"""


class Store:
    def __init__(self, config: Config):
        self.redis = Redis.from_url(
            config.vrpsp_entry_redis_url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
        )
        self.prefix = config.vrpsp_entry_key_prefix
        self.owner = uuid.uuid4().hex
        self.subs = self.prefix + "subscriptions"
        self.outbox = self.prefix + "outbox"
        self.retry = self.prefix + "retry"
        self.lease = self.prefix + "lease"

    async def ensure(self) -> None:
        try:
            await self.redis.xgroup_create(self.outbox, CONSUMER_GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def acquire(self) -> bool:
        return bool(
            await self.redis.set(self.lease, self.owner, nx=True, ex=LEASE_SECONDS)
            or await self.renew()
        )

    async def renew(self) -> bool:
        return bool(await self.redis.eval(RENEW, 1, self.lease, self.owner, LEASE_SECONDS))

    async def release(self) -> None:
        await self.redis.eval(RELEASE, 1, self.lease, self.owner)

    async def time_us(self) -> int:
        seconds, microseconds = await self.redis.time()
        return seconds * 1_000_000 + microseconds

    @staticmethod
    def field(group: str, kind: str, identity: int) -> str:
        return f"{group}:{kind}:{identity}"

    async def subscribe(self, group: str, kind: str, identity: int, bot_id: str) -> bool:
        sub = json.dumps({"bot_id": bot_id, "token": uuid.uuid4().hex})
        return bool(
            await self.redis.eval(SUBSCRIBE, 1, self.subs, self.field(group, kind, identity), sub)
        )

    async def unsubscribe(self, group: str, kind: str, identity: int) -> bool:
        return bool(await self.redis.hdel(self.subs, self.field(group, kind, identity)))

    async def subscriptions(self, group: str | None = None) -> dict[str, dict]:
        rows = await self.redis.hgetall(self.subs)
        return {
            field: json.loads(value)
            for field, value in rows.items()
            if group is None or field.startswith(group + ":")
        }

    async def enqueue(self, group: str, entry: Entry, message: str) -> bool:
        key = entry.delivery_key(group)
        task = json.dumps({"group": group, "key": key, "message": message}, ensure_ascii=False)
        return bool(
            await self.redis.eval(
                ENQUEUE,
                4,
                self.lease,
                self.subs,
                self.prefix + "seen:" + key,
                self.outbox,
                self.owner,
                self.field(group, "room", entry.room_id),
                self.field(group, "user", entry.uid),
                entry.time_us,
                task,
                DEDUPE_SECONDS,
            )
        )

    async def read(self, cursor: str) -> list[tuple[str, dict]]:
        rows = await self.redis.xreadgroup(
            CONSUMER_GROUP, CONSUMER, {self.outbox: cursor}, count=100
        )
        return [
            (message_id, json.loads(fields["task"]))
            for _, items in rows
            for message_id, fields in items
        ]

    async def matching_bots(self, task: dict) -> list[str]:
        matches = task["matches"]
        rows = await self.redis.hmget(self.subs, [match["field"] for match in matches])
        return sorted(
            {
                match["bot_id"]
                for match, value in zip(matches, rows, strict=True)
                if value and json.loads(value)["token"] == match["token"]
            }
        )

    async def retry_due(self, message_id: str, now: float) -> bool:
        value = await self.redis.hget(self.retry, message_id)
        return not value or json.loads(value)["next"] <= now

    async def defer(self, message_id: str, now: float) -> None:
        value = await self.redis.hget(self.retry, message_id)
        attempts = json.loads(value)["attempts"] + 1 if value else 1
        delay = min(300, 2 ** min(attempts, 9))
        await self.redis.hset(
            self.retry, message_id, json.dumps({"attempts": attempts, "next": now + delay})
        )

    async def finish(self, message_id: str, task: dict, result: str) -> bool:
        return bool(
            await self.redis.eval(
                FINISH,
                4,
                self.lease,
                self.prefix + "seen:" + task["key"],
                self.outbox,
                self.retry,
                self.owner,
                message_id,
                result,
                DEDUPE_SECONDS,
                CONSUMER_GROUP,
            )
        )

    async def close(self) -> None:
        await self.redis.aclose()
