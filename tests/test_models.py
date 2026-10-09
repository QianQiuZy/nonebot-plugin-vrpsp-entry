from datetime import datetime, timezone

import pytest

from nonebot_plugin_vrpsp_entry.config import Config, is_allowed
from nonebot_plugin_vrpsp_entry.models import Entry, Names, parse_entries, parse_id


def payload(time="2026-10-08T22:30:00+08:00", **changes):
    row = {"room_id": 1820703922, "uid": 1203217682, "event_time": time}
    row.update(changes)
    return {"mode": "cache", "cache": 1, "items": [row]}


def test_message_and_canonical_identity():
    entry = parse_entries(payload())[0]
    assert Names().message(entry) == "泽音Melody在22:30:00进入了花礼Harei的直播间"
    utc = parse_entries(payload("2026-10-08T14:30:00+00:00"))[0]
    assert entry.delivery_key("10") == utc.delivery_key("10")
    assert entry.delivery_key("10") != utc.delivery_key("11")
    assert Names().room(10317) == "这是亦枝YY"
    assert Names().user(999) == "用户999"
    assert Names().room(999) == "房间999"


def test_milliseconds_are_preserved_and_rows_deduped_sorted():
    data = payload("2026-10-08T22:30:00.123+08:00")
    row = data["items"][0]
    data["items"].extend([row.copy(), {**row, "event_time": "2026-10-08T22:30:00.122+08:00"}])
    entries = parse_entries(data)
    assert len(entries) == 2
    assert entries[1].time_us - entries[0].time_us == 1000


@pytest.mark.parametrize(
    "data",
    [
        {},
        [],
        {"mode": "cache", "cache": 1, "items": None},
        {"mode": "room", "cache": 1, "items": []},
        {"mode": "cache", "cache": True, "items": []},
        {"mode": "cache", "cache": 2, "items": []},
        payload("2026-10-08T22:30:00"),
        payload("bad"),
        payload(uid=True),
        payload(room_id=-1),
        payload(uid="1203217682"),
        payload(uid=2**64),
    ],
)
def test_invalid_response_fails_without_partial_processing(data):
    with pytest.raises(ValueError):
        parse_entries(data)


@pytest.mark.parametrize("value", ["", "0", "-1", "1 2", "123abc", "１２３", str(2**64)])
def test_invalid_id(value):
    with pytest.raises(ValueError):
        parse_id(value)


def test_default_authorization_and_config_validation():
    config = Config()
    assert is_allowed(config, 308025580)
    assert not is_allowed(config, 99)
    assert not is_allowed(Config(vrpsp_entry_allowed_users=[]), 308025580)
    assert config.vrpsp_entry_poll_seconds == 30
    assert Config(vrpsp_entry_key_prefix="example").vrpsp_entry_key_prefix == "example:"
    for changes in (
        {"vrpsp_entry_key_prefix": ""},
        {"vrpsp_entry_sources": []},
        {"vrpsp_entry_sources": ["other"]},
    ):
        with pytest.raises(ValueError):
            Config(**changes)
    entry = Entry(1, 2, datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert entry.time_us == 1767225600000000
