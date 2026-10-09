import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SHANGHAI = timezone(timedelta(hours=8))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
RESOURCE = Path(__file__).resolve().parent / "resource"


def parse_id(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 20:
        raise ValueError("请输入一个正整数 ID")
    number = int(value)
    if not 0 < number < 2**64:
        raise ValueError("ID 超出有效范围")
    return number


@dataclass(frozen=True)
class Entry:
    room_id: int
    uid: int
    when: datetime

    @property
    def time_us(self) -> int:
        delta = self.when - EPOCH
        return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds

    def delivery_key(self, group: str) -> str:
        # 来源和 Bot ID 不参与：同一事件命中 VR/PSP、房间/用户都只投递一次。
        raw = f"{group}:{self.room_id}:{self.uid}:{self.time_us}"
        return hashlib.sha256(raw.encode()).hexdigest()


def parse_entries(payload: object) -> list[Entry]:
    if (
        not isinstance(payload, dict)
        or payload.get("mode") != "cache"
        or type(payload.get("cache")) is not int
        or payload["cache"] != 1
        or not isinstance(payload.get("items"), list)
    ):
        raise ValueError("invalid entry response")
    result = set()
    for row in payload["items"]:
        if not isinstance(row, dict):
            raise ValueError("invalid entry row")
        ids = [row.get("room_id"), row.get("uid")]
        if any(type(value) is not int or not 0 < value < 2**64 for value in ids):
            raise ValueError("invalid entry identity")
        value = row.get("event_time")
        if not isinstance(value, str):
            raise ValueError("invalid entry timestamp")
        when = datetime.fromisoformat(value)
        if when.tzinfo is None:
            raise ValueError("entry timestamp must contain timezone")
        result.add(Entry(ids[0], ids[1], when.astimezone(SHANGHAI)))
    return sorted(result, key=lambda item: (item.time_us, item.room_id, item.uid))


class Names:
    def __init__(self, directory: Path = RESOURCE):
        rows = json.loads((directory / "entry_users.json").read_text(encoding="utf-8-sig"))
        self.users = {parse_id(str(row["uid"])): str(row["name"]) for row in rows}
        self.rooms: dict[int, str] = {}
        for filename in ("rooms.json", "psp_rooms.json"):
            data = json.loads((directory / filename).read_text(encoding="utf-8-sig"))
            self.rooms.update(
                {parse_id(room): str(name) for room, name in data["room_anchors"].items()}
            )

    def user(self, uid: int) -> str:
        return self.users.get(uid, f"用户{uid}")

    def room(self, room_id: int) -> str:
        return self.rooms.get(room_id, f"房间{room_id}")

    def message(self, entry: Entry) -> str:
        clock = entry.when.astimezone(SHANGHAI).strftime("%H:%M:%S")
        return f"{self.user(entry.uid)}在{clock}进入了{self.room(entry.room_id)}的直播间"
