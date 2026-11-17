from datetime import datetime, timezone

import pytest

from nonebot_plugin_vrpsp_entry.config import Config, is_allowed
from nonebot_plugin_vrpsp_entry.models import (
    Entry,
    Names,
    parse_entries,
    parse_id,
    parse_price,
    parse_red_pockets,
)


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


@pytest.mark.parametrize(
    "kind,amount,expected",
    [
        ("guard", 5, "花礼Harei有红包啦！\n舰长红包5个\n开始时间：01:02:03"),
        ("battery", 100, "花礼Harei有红包啦！\n电池红包100电池\n开始时间：01:02:03"),
        ("gift", 520, "花礼Harei有红包啦！\n礼物红包520电池\n开始时间：01:02:03"),
    ],
)
def test_pocket_parsing_deduplication_and_exact_notification(kind, amount, expected):
    data = {
        "mode": "cache",
        "cache": 1,
        "items": [],
        "red_pocket": [
            {
                "room_id": 1820703922,
                "pocket_type": kind,
                kind + "_num": amount,
                "event_time": "2026-11-16T01:02:03+08:00",
            }
        ],
    }
    first = parse_red_pockets(data)[0]
    data["red_pocket"].append({**data["red_pocket"][0], "event_time": "2026-11-15T17:02:03+00:00"})
    assert parse_red_pockets(data) == [first]
    assert Names().pocket_message(first) == expected
    assert first.delivery_key("1") != first.delivery_key("2")
    assert first.delivery_key("1") != Entry(first.room_id, 5, first.when).delivery_key("1")


def test_older_source_without_pocket_field_is_compatible():
    assert parse_red_pockets(payload()) == []


@pytest.mark.parametrize(
    "changes",
    [
        {"room_id": True},
        {"room_id": 0},
        {"pocket_type": []},
        {"pocket_type": "unknown"},
        {"battery_num": "100"},
        {"battery_num": True},
        {"battery_num": -1},
        {"battery_num": 2**53},
        {"event_time": "2026-11-16T01:02:03"},
        {"event_time": "bad"},
    ],
)
def test_invalid_pocket_response_is_rejected(changes):
    row = {
        "room_id": 1820703922,
        "pocket_type": "battery",
        "battery_num": 100,
        "event_time": "2026-11-16T01:02:03+08:00",
        **changes,
    }
    with pytest.raises(ValueError):
        parse_red_pockets({"mode": "cache", "cache": 1, "red_pocket": [row]})


@pytest.mark.parametrize("value", ["-1", "100.0", "100 200", "１２３", str(2**53), ""])
def test_invalid_pocket_price(value):
    with pytest.raises(ValueError):
        parse_price(value)


def test_zero_price_allows_all_battery_and_gift_pockets():
    assert parse_price("0") == 0
