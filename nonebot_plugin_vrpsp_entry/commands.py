from typing import Annotated

from nonebot import on_command
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from redis.exceptions import RedisError

from . import sending
from .config import is_allowed
from .models import parse_id
from .worker import Worker

DENIED = "无权限订阅，请联系千秋紫莹"


def register(worker: Worker) -> dict:
    commands = {}

    async def group_only(matcher: Matcher, event: MessageEvent):
        if not isinstance(event, GroupMessageEvent):
            await sending.finish(matcher, "请在需要订阅的群聊中使用此指令")

    async def check(matcher: Matcher, event: MessageEvent):
        if not is_allowed(worker.config, event.user_id):
            await sending.finish(matcher, DENIED)
        await group_only(matcher, event)

    def management(kind: str, remove: bool):
        noun = "房间" if kind == "room" else "用户"
        name = f"入场{noun}{'订阅取消' if remove else '订阅'}"
        aliases = {f"入场{noun}取消"} if remove else set()
        command = on_command(name, aliases=aliases, force_whitespace=True, priority=10, block=True)
        command.handle()(check)

        @command.handle()
        async def handle(
            bot: Bot,
            event: GroupMessageEvent,
            matcher: Matcher,
            args: Annotated[Message, CommandArg()],
        ):
            try:
                identity = parse_id(args.extract_plain_text().strip())
            except ValueError:
                await sending.finish(
                    matcher, f"用法：/{name} <{'房间号' if kind == 'room' else 'UID'}>"
                )
            display = worker.names.room(identity) if kind == "room" else worker.names.user(identity)
            try:
                if remove:
                    changed = await worker.store.unsubscribe(str(event.group_id), kind, identity)
                    text = "已取消订阅" if changed else "本群未订阅"
                else:
                    changed = await worker.store.subscribe(
                        str(event.group_id), kind, identity, bot.self_id
                    )
                    text = "已订阅" if changed else "本群已订阅"
            except RedisError:
                await sending.finish(matcher, "Redis 暂不可用，请稍后查询或重试")
            suffix = "；仅推送订阅后的新入场" if changed and not remove else ""
            await sending.finish(matcher, f"{text}{noun}：{display}（{identity}）{suffix}")

        return command

    for kind in ("room", "user"):
        for remove in (False, True):
            commands[f"{kind}_{'remove' if remove else 'add'}"] = management(kind, remove)

    listing = on_command("入场订阅列表", priority=10, block=True)
    listing.handle()(check)

    @listing.handle()
    async def show(event: GroupMessageEvent, matcher: Matcher):
        try:
            rows = await worker.store.subscriptions(str(event.group_id))
        except RedisError:
            await sending.finish(matcher, "Redis 暂不可用，请稍后再试")
        if not rows:
            await sending.finish(matcher, "本群暂无入场订阅")
        text = "本群入场订阅："
        for field in sorted(rows):
            _, kind, value = field.split(":")
            identity = int(value)
            name = worker.names.room(identity) if kind == "room" else worker.names.user(identity)
            label = "房间" if kind == "room" else "用户"
            line = f"\n{label}：{name}（{identity}）"
            if len(text) + len(line) > 1800:
                await sending.send(matcher, text)
                text = "本群入场订阅（续）："
            text += line
        await sending.finish(matcher, text)

    commands["list"] = listing

    public_listing = on_command("入场订阅", force_whitespace=True, priority=10, block=True)
    public_listing.handle()(group_only)

    @public_listing.handle()
    async def public_show(event: GroupMessageEvent, matcher: Matcher):
        try:
            rows = await worker.store.subscriptions(str(event.group_id))
        except RedisError:
            await sending.finish(matcher, "Redis 暂不可用，请稍后再试")
        rooms, users = [], []
        for field in sorted(rows):
            _, kind, value = field.split(":")
            identity = int(value)
            if kind == "room":
                rooms.append(worker.names.room(identity))
            else:
                users.append(worker.names.user(identity))
        text = (
            "当前群聊订阅情况\n"
            f"房间订阅：{'、'.join(rooms) if rooms else '无'}\n"
            f"用户订阅：{'、'.join(users) if users else '无'}"
        )
        # 大量订阅时分段，仍只显示当前群的名称。
        while len(text) > 1800:
            await sending.send(matcher, text[:1800])
            text = text[1800:]
        await sending.finish(matcher, text)

    commands["public_list"] = public_listing
    return commands
