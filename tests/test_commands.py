from unittest.mock import AsyncMock

import pytest
from nonebot.adapters.onebot.v11 import Bot, GroupMessageEvent, Message, PrivateMessageEvent
from nonebot.adapters.onebot.v11.event import Sender
from redis.exceptions import ConnectionError

import nonebot_plugin_vrpsp_entry as plugin
from nonebot_plugin_vrpsp_entry.commands import DENIED


def group_event(text, user=99, role="admin", group=10):
    return GroupMessageEvent(
        time=1,
        self_id=1,
        post_type="message",
        sub_type="normal",
        message_type="group",
        user_id=user,
        group_id=group,
        message_id=1,
        message=Message(text),
        raw_message=text,
        font=0,
        sender=Sender(user_id=user, nickname="测试", role=role),
        to_me=False,
    )


@pytest.mark.parametrize("role", ["admin", "owner", "member"])
@pytest.mark.parametrize(
    "command,text",
    [
        ("room_add", "/入场房间订阅 1820703922"),
        ("user_add", "/入场用户订阅 1048135385"),
        ("room_remove", "/入场房间取消 1820703922"),
        ("user_remove", "/入场用户取消 1048135385"),
        ("room_remove", "/入场房间订阅取消 1820703922"),
        ("user_remove", "/入场用户订阅取消 1048135385"),
        ("list", "/入场订阅列表"),
    ],
)
async def test_roles_and_superuser_cannot_bypass_whitelist(app, monkeypatch, role, command, text):
    store = AsyncMock()
    monkeypatch.setattr(plugin.worker, "store", store)
    async with app.test_matcher(plugin.commands[command]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event(text, role=role)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, DENIED, result={"message_id": 1})
        ctx.should_pass_rule()
    assert store.mock_calls == []


@pytest.mark.parametrize(
    "command,text,kind,identity,name",
    [
        ("room_add", "/入场房间订阅 1820703922", "room", 1820703922, "花礼Harei"),
        ("user_add", "/入场用户订阅 1048135385", "user", 1048135385, "花礼Harei"),
    ],
)
async def test_whitelisted_member_can_subscribe_to_current_group(
    app,
    monkeypatch,
    command,
    text,
    kind,
    identity,
    name,
):
    store = AsyncMock()
    store.subscribe.return_value = True
    monkeypatch.setattr(plugin.worker, "store", store)
    async with app.test_matcher(plugin.commands[command]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event(text, user=308025580, role="member", group=42)
        ctx.receive_event(bot, event)
        noun = "房间" if kind == "room" else "用户"
        ctx.should_call_send(
            event,
            f"已订阅{noun}：{name}（{identity}）；仅推送订阅后的新入场",
            result={"message_id": 1},
        )
        ctx.should_pass_rule()
    store.subscribe.assert_awaited_once_with("42", kind, identity, "1")


async def test_bad_id_never_calls_redis(app, monkeypatch):
    store = AsyncMock()
    monkeypatch.setattr(plugin.worker, "store", store)
    async with app.test_matcher(plugin.commands["user_add"]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event("/入场用户订阅 123abc", user=308025580)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, "用法：/入场用户订阅 <UID>", result={"message_id": 1})
        ctx.should_pass_rule()
    assert store.mock_calls == []


@pytest.mark.parametrize(
    "command,text,user",
    [("list", "/入场订阅列表", 308025580), ("public_list", "/入场订阅", 12345)],
)
async def test_private_command_is_rejected(app, command, text, user):
    async with app.test_matcher(plugin.commands[command]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = PrivateMessageEvent(
            time=1,
            self_id=1,
            post_type="message",
            sub_type="friend",
            message_type="private",
            user_id=user,
            message_id=1,
            message=Message(text),
            raw_message=text,
            font=0,
            sender=Sender(user_id=user, nickname="测试"),
            to_me=True,
        )
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, "请在需要订阅的群聊中使用此指令", result={"message_id": 1})
        ctx.should_pass_rule()


async def test_redis_error_is_reported_without_success_claim(app, monkeypatch):
    store = AsyncMock()
    store.subscribe.side_effect = ConnectionError("secret connection URL not printed")
    monkeypatch.setattr(plugin.worker, "store", store)
    async with app.test_matcher(plugin.commands["room_add"]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event("/入场房间订阅 1820703922", user=308025580)
        ctx.receive_event(bot, event)
        ctx.should_call_send(event, "Redis 暂不可用，请稍后查询或重试", result={"message_id": 1})
        ctx.should_pass_rule()


async def test_cancel_and_listing(app, monkeypatch):
    store = AsyncMock()
    store.unsubscribe.return_value = True
    store.subscriptions.return_value = {"10:room:1820703922": {}, "10:user:1048135385": {}}
    monkeypatch.setattr(plugin.worker, "store", store)
    async with app.test_matcher(plugin.commands["room_remove"]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event("/入场房间取消 1820703922", user=308025580)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event, "已取消订阅房间：花礼Harei（1820703922）", result={"message_id": 1}
        )
        ctx.should_pass_rule()
    store.unsubscribe.assert_awaited_once_with("10", "room", 1820703922)
    async with app.test_matcher(plugin.commands["list"]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event("/入场订阅列表", user=308025580)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "本群入场订阅：\n房间：花礼Harei（1820703922）\n用户：花礼Harei（1048135385）",
            result={"message_id": 1},
        )
        ctx.should_pass_rule()
    store.subscriptions.assert_awaited_once_with("10")


@pytest.mark.parametrize(
    "command,text,kind,identity,name",
    [
        ("room_remove", "/入场房间订阅取消 1820703922", "room", 1820703922, "花礼Harei"),
        ("user_remove", "/入场用户订阅取消 1203217682", "user", 1203217682, "泽音Melody"),
    ],
)
async def test_requested_cancel_commands(app, monkeypatch, command, text, kind, identity, name):
    store = AsyncMock()
    store.unsubscribe.return_value = True
    monkeypatch.setattr(plugin.worker, "store", store)
    async with app.test_matcher(plugin.commands[command]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event(text, user=308025580)
        ctx.receive_event(bot, event)
        noun = "房间" if kind == "room" else "用户"
        ctx.should_call_send(
            event, f"已取消订阅{noun}：{name}（{identity}）", result={"message_id": 1}
        )
        ctx.should_pass_rule()
    store.unsubscribe.assert_awaited_once_with("10", kind, identity)


@pytest.mark.parametrize("role", ["member", "admin", "owner"])
async def test_public_listing_is_open_with_requested_format(app, monkeypatch, role):
    store = AsyncMock()
    store.subscriptions.return_value = {"10:room:1820703922": {}, "10:user:1203217682": {}}
    monkeypatch.setattr(plugin.worker, "store", store)
    async with app.test_matcher(plugin.commands["public_list"]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event("/入场订阅", user=12345, role=role)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "当前群聊订阅情况\n房间订阅：花礼Harei\n用户订阅：泽音Melody",
            result={"message_id": 1},
        )
        ctx.should_pass_rule()
    store.subscriptions.assert_awaited_once_with("10")
    store.subscribe.assert_not_awaited()
    store.unsubscribe.assert_not_awaited()


async def test_public_empty_listing(app, monkeypatch):
    store = AsyncMock()
    store.subscriptions.return_value = {}
    monkeypatch.setattr(plugin.worker, "store", store)
    async with app.test_matcher(plugin.commands["public_list"]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event("/入场订阅", user=12345)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event, "当前群聊订阅情况\n房间订阅：无\n用户订阅：无", result={"message_id": 1}
        )
        ctx.should_pass_rule()
