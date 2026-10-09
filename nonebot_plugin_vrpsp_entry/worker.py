import asyncio
import time
from collections.abc import Callable

import httpx
from nonebot import get_bots, logger
from nonebot.adapters.onebot.v11 import Bot, Message, MessageSegment
from redis.exceptions import RedisError

from .client import Client
from .config import Config
from .models import Names
from .store import Store


class Worker:
    def __init__(self, config: Config, bots: Callable = get_bots):
        self.config = config
        self.store = Store(config)
        self.names = Names()
        self.client: Client | None = None
        self.bots = bots
        self.active = asyncio.Event()
        self.tasks: list[asyncio.Task] = []
        self.pending_cursor = "0"

    async def start(self) -> None:
        if self.tasks:
            return
        self.client = Client(self.config)
        self.tasks = [
            asyncio.create_task(self.lease_loop(), name="vrpsp-entry-lease"),
            asyncio.create_task(self.poll_loop(), name="vrpsp-entry-poll"),
            asyncio.create_task(self.send_loop(), name="vrpsp-entry-send"),
        ]

    async def stop(self) -> None:
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks.clear()
        self.active.clear()
        try:
            await self.store.release()
        except RedisError:
            pass
        await self.store.close()
        if self.client:
            await self.client.close()
            self.client = None

    async def lease_loop(self) -> None:
        while True:
            try:
                if await self.store.acquire():
                    await self.store.ensure()
                    self.active.set()
                else:
                    self.active.clear()
            except RedisError as exc:
                self.active.clear()
                logger.warning("入场 Redis 不可用：{}", type(exc).__name__)
            await asyncio.sleep(10)

    async def poll_once(self) -> None:
        if self.client is None:
            return
        sources = sorted(self.config.vrpsp_entry_sources)
        results = await asyncio.gather(
            *(self.client.fetch(source) for source in sources), return_exceptions=True
        )
        subscriptions = await self.store.subscriptions()
        now_us = await self.store.time_us()
        rooms: dict[int, set[str]] = {}
        users: dict[int, set[str]] = {}
        for field in subscriptions:
            group, kind, identity = field.split(":")
            index = rooms if kind == "room" else users
            index.setdefault(int(identity), set()).add(group)
        for source, result in zip(sources, results, strict=True):
            if isinstance(result, BaseException):
                # 不输出响应、URL 或连接凭据；VR 失败不妨碍 PSP。
                logger.warning("入场 {} 接口异常：{}", source.upper(), type(result).__name__)
                continue
            for entry in result:
                if not now_us - 600_000_000 <= entry.time_us <= now_us:
                    continue
                groups = rooms.get(entry.room_id, set()) | users.get(entry.uid, set())
                for group in sorted(groups):
                    await self.store.enqueue(group, entry, self.names.message(entry))

    async def poll_loop(self) -> None:
        while True:
            await self.active.wait()
            started = time.monotonic()
            try:
                await self.poll_once()
            except (RedisError, ValueError, httpx.HTTPError) as exc:
                logger.warning("入场轮询异常：{}", type(exc).__name__)
            await asyncio.sleep(
                max(0, self.config.vrpsp_entry_poll_seconds - (time.monotonic() - started))
            )

    async def send_once(self) -> None:
        # 固定 consumer 恢复重启前 pending；分页遍历，失败消息不阻塞后面的新消息。
        pending = await self.store.read(self.pending_cursor)
        self.pending_cursor = pending[-1][0] if len(pending) == 100 else "0"
        fresh = await self.store.read(">")
        for message_id, task in pending + fresh:
            if not await self.store.renew():
                self.active.clear()
                return
            now = (await self.store.time_us()) / 1_000_000
            if not await self.store.retry_due(message_id, now):
                continue
            candidates = await self.store.matching_bots(task)
            if not candidates:
                await self.store.finish(message_id, task, "cancelled")
                continue
            available = self.bots()
            bot = next(
                (
                    available[identity]
                    for identity in candidates
                    if isinstance(available.get(identity), Bot)
                ),
                None,
            )
            if bot is None:
                await self.store.defer(message_id, now)
                continue
            try:
                response = await asyncio.wait_for(
                    bot.send_group_msg(
                        group_id=int(task["group"]),
                        message=Message(MessageSegment.text(task["message"])),
                    ),
                    timeout=20,
                )
                if (
                    not isinstance(response, dict)
                    or type(response.get("message_id")) is not int
                    or response["message_id"] == 0
                ):
                    raise ValueError("missing message_id")
            except Exception as exc:
                logger.warning("入场群消息发送失败：{}", type(exc).__name__)
                await self.store.defer(message_id, now)
                continue
            await self.store.finish(message_id, task, "sent")

    async def send_loop(self) -> None:
        while True:
            await self.active.wait()
            try:
                await self.send_once()
            except RedisError as exc:
                logger.warning("入场发送队列 Redis 异常：{}", type(exc).__name__)
            await asyncio.sleep(1)
