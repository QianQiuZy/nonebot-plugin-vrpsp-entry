from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from nonebot.adapters.onebot.v11 import Bot

import nonebot_plugin_vrpsp_entry as plugin
from nonebot_plugin_vrpsp_entry.client import Client
from nonebot_plugin_vrpsp_entry.config import Config
from nonebot_plugin_vrpsp_entry.models import Names, parse_entries
from nonebot_plugin_vrpsp_entry.worker import Worker

from .test_commands import group_event
from .test_store import new_entry


def test_vup_default_and_explicit_source_selection():
    assert Config().vrpsp_entry_sources == {"vr", "psp", "vup"}
    assert Config(vrpsp_entry_sources={"vr", "psp"}).vrpsp_entry_sources == {"vr", "psp"}
    assert Config(vrpsp_entry_sources={"vup"}).vrpsp_entry_sources == {"vup"}
    assert Names().room(263605) == "小缘"
    assert Names().user(271887040) == "Hooxi_Official"


@pytest.mark.parametrize("url", [
    "https://vup.qianqiuzy.cn/gift/entry", "https://example.test/gift/entry",
])
async def test_vup_http_contract_and_custom_url(url):
    client = Client(Config(vrpsp_entry_vup_url=url))
    assert client.http.trust_env is False
    body = {
        "mode": "cache",
        "cache": 1,
        "items": [{"room_id": 263605, "uid": 271887040,
                   "event_time": "2026-11-06T13:00:00.123+08:00"}],
    }

    def respond(request):
        assert str(request.url) == url + "?cache=1"
        return httpx.Response(200, json=body)

    await client.http.aclose()
    client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        assert await client.fetch("vup") == parse_entries(body)
    finally:
        await client.close()


@pytest.mark.parametrize("failed_sources", [{"vr", "psp"}, {"vup"}, set()])
async def test_vup_polling_failure_isolation_and_cross_source_dedup(store, failed_sources):
    worker = Worker(Config())
    await worker.store.close()
    worker.store = store
    worker.client = AsyncMock()
    bot = MagicMock(spec=Bot)
    bot.send_group_msg = AsyncMock(return_value={"message_id": 1})
    worker.bots = lambda: {"1": bot}
    await store.subscribe("10", "room", 263605, "1")
    await store.subscribe("10", "user", 1203217682, "1")
    entry = replace(await new_entry(store), room_id=263605)

    async def fetch(source):
        if source in failed_sources:
            raise ValueError("source unavailable")
        return [entry]

    worker.client.fetch.side_effect = fetch
    await worker.poll_once()
    await worker.poll_once()
    assert {call.args[0] for call in worker.client.fetch.call_args_list} == {"vr", "psp", "vup"}
    assert await store.redis.xlen(store.outbox) == 1
    await worker.send_once()
    await worker.send_once()
    bot.send_group_msg.assert_awaited_once()
    text = bot.send_group_msg.call_args.kwargs["message"].extract_plain_text()
    assert text == Names().message(entry)
    assert "小缘的直播间" in text


async def test_existing_subscription_command_accepts_vup_room(app, monkeypatch):
    store = AsyncMock()
    store.subscribe.return_value = True
    monkeypatch.setattr(plugin.worker, "store", store)
    async with app.test_matcher(plugin.commands["room_add"]) as ctx:
        bot = ctx.create_bot(base=Bot, self_id="1")
        event = group_event("/入场房间订阅 263605", user=308025580, group=42)
        ctx.receive_event(bot, event)
        ctx.should_call_send(
            event,
            "已订阅房间：小缘（263605）；仅推送订阅后的新入场",
            result={"message_id": 1},
        )
        ctx.should_pass_rule()
    store.subscribe.assert_awaited_once_with("42", "room", 263605, "1")
