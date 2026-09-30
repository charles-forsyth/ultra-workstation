"""v0.6 tests: calendar rules, ownership guard, block text, Today routes (demo)."""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ultra.calendar import Calendar, CalendarError, free_windows, is_ultra_block, slim_event
from ultra.config import Config
from ultra.store import Store

NY = ZoneInfo("America/New_York")


def t(h: int, m: int = 0, d: int = 1) -> dt.datetime:
    return dt.datetime(2026, 10, d, h, m, tzinfo=NY)


def test_free_windows_merges_overlaps_and_respects_minimum():
    busy = [(t(9), t(10)), (t(9, 30), t(10, 30)), (t(11), t(11, 20)), (t(15), t(18))]
    wins = free_windows(busy, t(8), t(17), 30)
    assert wins == [(t(8), t(9)), (t(10, 30), t(11)), (t(11, 20), t(15))]
    assert free_windows(busy, t(8), t(17), 61) == [(t(11, 20), t(15))]
    assert free_windows([], t(8), t(9), 30) == [(t(8), t(9))]


def test_ultra_tag_and_organizer_decide_ownership():
    tagged = {"extendedProperties": {"private": {"ultra": "1"}}, "organizer": {"self": True}}
    assert is_ultra_block(tagged)
    assert not is_ultra_block({"organizer": {"self": True}})  # made elsewhere
    assert not is_ultra_block({**tagged, "organizer": {"self": False, "email": "x@y"}})
    assert not is_ultra_block({"extendedProperties": {"private": {"ultra": "0"}}})


def test_slim_event_fields():
    e = {
        "id": "abc",
        "summary": "Sync",
        "start": {"dateTime": "2026-10-01T13:00:00-04:00"},
        "end": {"dateTime": "2026-10-01T14:00:00-04:00"},
        "organizer": {"email": "b@x.org"},
        "attendees": [
            {"email": "me@x.org", "self": True, "responseStatus": "needsAction"},
            {"email": "room@x.org", "resource": True},
        ],
        "conferenceData": {
            "entryPoints": [{"entryPointType": "video", "uri": "https://meet.google.com/aaa"}]
        },
    }
    s = slim_event(e, "primary")
    assert s["my_response"] == "needsAction" and s["link"] == "https://meet.google.com/aaa"
    assert not s["ultra"] and not s["all_day"] and s["attendee_count"] == 2
    z = slim_event(
        {
            "id": "z",
            "start": {"date": "2026-10-01"},
            "end": {"date": "2026-10-02"},
            "description": "join https://example.zoom.us/j/123 now",
        },
        "primary",
    )
    assert z["all_day"] and z["link"] == "https://example.zoom.us/j/123"


class FakeEvents:
    """Just enough of the Calendar API events() surface."""

    def __init__(self) -> None:
        self.db: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []

    def _x(self, v: Any) -> Any:
        class R:
            def execute(self) -> Any:
                return v

        return R()

    def insert(self, calendarId: str, body: dict) -> Any:
        eid = f"e{len(self.db) + 1:05d}"
        self.db[eid] = {**body, "id": eid, "organizer": {"self": True}, "status": "confirmed"}
        self.calls.append(("insert", body))
        return self._x(self.db[eid])

    def get(self, calendarId: str, eventId: str) -> Any:
        if eventId not in self.db:
            raise RuntimeError("404")
        return self._x(self.db[eventId])

    def patch(self, calendarId: str, eventId: str, body: dict) -> Any:
        self.calls.append(("patch", eventId))
        self.db[eventId].update(body)
        return self._x(self.db[eventId])

    def delete(self, calendarId: str, eventId: str) -> Any:
        self.calls.append(("delete", eventId))
        del self.db[eventId]
        return self._x(None)


@pytest.fixture
def cal(tmp_path, monkeypatch):
    c = Calendar(Config({"operator": {"timezone": "America/New_York"}}), Store(tmp_path / "s.db"))
    ev = FakeEvents()

    class Svc:
        def events(self) -> FakeEvents:
            return ev

    monkeypatch.setattr(c, "_svc", Svc)
    c.fake = ev  # type: ignore[attr-defined]
    return c


def test_create_block_tags_and_verifies(cal):
    r = cal.create_block("Focus: storage", t(10), t(10, 30), description="ctx", color="tomato")
    assert r["ok"] and r["event"]["ultra"]
    body = cal.fake.calls[0][1]
    assert body["extendedProperties"]["private"]["ultra"] == "1"
    assert body["visibility"] == "default" and body["colorId"] == "11"
    assert "attendees" not in body  # never invites anyone
    p = cal.create_block("Pick up", t(16), t(16, 30), personal=True)
    assert p["event"]["visibility"] == "private"


def test_block_validation(cal):
    with pytest.raises(CalendarError):
        cal.create_block("  ", t(10), t(10, 30))
    with pytest.raises(CalendarError):
        cal.create_block("x", t(10), t(10, 2))
    with pytest.raises(CalendarError):
        cal.create_block("x", t(8), t(17, 30))
    assert cal.fake.calls == []


def test_move_and_delete_only_ultra_blocks(cal):
    cal.fake.db["foreign"] = {
        "id": "foreign",
        "summary": "Their meeting",
        "start": {"dateTime": t(9).isoformat()},
        "end": {"dateTime": t(10).isoformat()},
        "organizer": {"self": False, "email": "b@x.org"},
    }
    cal.fake.db["mine-untagged"] = {
        "id": "mine-untagged",
        "summary": "Mine",
        "organizer": {"self": True},
    }
    for eid in ("foreign", "mine-untagged"):
        with pytest.raises(CalendarError):
            cal.move_block("primary", eid, t(11), t(12))
        with pytest.raises(CalendarError):
            cal.delete_block("primary", eid)
    assert [c for c in cal.fake.calls if c[0] in ("patch", "delete")] == []
    r = cal.create_block("Focus", t(10), t(10, 30))
    eid = r["event"]["id"]
    m = cal.move_block("primary", eid, t(14), t(15), summary="Focus 2")
    assert m["ok"] and m["event"]["summary"] == "Focus 2"
    assert cal.delete_block("primary", eid) == {"ok": True, "deleted": eid}


def test_verify_flags_a_mismatch(cal, monkeypatch):
    real_insert = cal.fake.insert

    def shifted(calendarId: str, body: dict) -> Any:
        body = {**body, "start": {"dateTime": t(11).isoformat()}}
        return real_insert(calendarId, body)

    monkeypatch.setattr(cal.fake, "insert", shifted)
    r = cal.create_block("Focus", t(10), t(10, 30))
    assert not r["ok"] and "time differs from what was asked" in r["problems"]


# ---------------------------------------------------------------- routes (demo)
def _api():
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    httpd.server_close()
    return api


def _call(api, method: str, path: str, body: Any = None, q: dict | None = None) -> Any:
    return api.dispatch(method, path, q or {}, body)[1]


def test_demo_today_block_move_delete_and_guard():
    from ultra.server import ApiError

    api = _api()
    cal = api.demo_calendar
    today = dt.datetime.now(cal.tz).date().isoformat()
    d = _call(api, "GET", "/api/cal/day", q={"day": [today]})
    assert [e["summary"] for e in d["events"]][:2] == [
        "Storage planning with Ben",
        "Weekly team sync",
    ]
    foreign = next(e for e in d["events"] if not e["ultra"])
    with pytest.raises(ApiError) as ei:
        _call(api, "POST", "/api/cal/block/delete", {"cal": "primary", "id": foreign["id"]})
    assert ei.value.status == 409
    txt = _call(api, "POST", "/api/cal/block-text", {"key": "g-100"})
    assert txt["summary"].startswith("Focus: ")
    r = _call(
        api,
        "POST",
        "/api/cal/block",
        {"summary": txt["summary"], "start": f"{today}T16:00", "minutes": 30, "key": "g-100"},
    )
    assert r["ok"] and r["event"]["ultra"]
    eid = r["event"]["id"]
    mv = _call(
        api,
        "POST",
        "/api/cal/block/move",
        {"cal": "primary", "id": eid, "start": f"{today}T16:30", "end": f"{today}T17:00"},
    )
    assert mv["event"]["start"].startswith(f"{today}T16:30")
    assert _call(api, "POST", "/api/cal/block/delete", {"cal": "primary", "id": eid})["ok"]
    with pytest.raises(ApiError):
        _call(
            api,
            "POST",
            "/api/cal/block",
            {"summary": "x", "start": f"{today}T16:00", "color": "neon"},
        )
    with pytest.raises(ApiError):
        _call(api, "GET", "/api/cal/day", q={"day": ["tomorrow"]})


def test_demo_meeting_prep_excludes_me_and_slots():
    api = _api()
    cal = api.demo_calendar
    today = dt.datetime.now(cal.tz).date().isoformat()
    d = _call(api, "GET", "/api/cal/day", q={"day": [today]})
    mtg = d["events"][0]
    e = _call(api, "GET", "/api/cal/event", q={"cal": ["primary"], "id": [mtg["id"]]})
    assert [p["addr"] for p in e["prep_people"]] == ["ben@example.org"]
    s = _call(
        api,
        "POST",
        "/api/cal/slots",
        {"emails": ["ben@example.org, x@elsewhere.com"], "minutes": 30, "days": 3},
    )
    assert "x@elsewhere.com" in s["unknown"]
    assert all(w["start"] < w["end"] for w in s["windows"])
