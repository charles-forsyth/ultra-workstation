"""v0.10 tests: week view, RSVP, repeating blocks, meeting invites (two approvals),
meeting prep context, meeting log card (SPEC 7.7)."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ultra.calendar import Calendar, CalendarError, is_ultra_block
from ultra.config import Config
from ultra.invites import InviteError, Invites, Rsvp, invite_hash
from ultra.store import Store

NY = ZoneInfo("America/New_York")


def t(h: int, m: int = 0, d: int = 1, mo: int = 10) -> dt.datetime:
    return dt.datetime(2026, mo, d, h, m, tzinfo=NY)


class X:
    def __init__(self, v: Any):
        self.v = v

    def execute(self) -> Any:
        return self.v


class FakeEvents:
    def __init__(self) -> None:
        self.db: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any, dict[str, Any]]] = []

    def insert(self, calendarId: str, body: dict, **kw: Any) -> X:
        eid = f"e{len(self.db) + 1:05d}"
        self.db[eid] = {
            **body,
            "id": eid,
            "organizer": {"self": True, "email": "me@example.org"},
            "status": "confirmed",
        }
        if "attendees" in body:
            self.db[eid]["attendees"] = [
                {**a, "responseStatus": "needsAction"} for a in body["attendees"]
            ] + [{"email": "me@example.org", "self": True, "responseStatus": "accepted"}]
        self.calls.append(("insert", body, kw))
        return X(self.db[eid])

    def get(self, calendarId: str, eventId: str) -> X:
        if eventId not in self.db:
            raise RuntimeError("404")
        return X(self.db[eventId])

    def patch(self, calendarId: str, eventId: str, body: dict, **kw: Any) -> X:
        self.calls.append(("patch", {"id": eventId, **body}, kw))
        self.db[eventId].update(body)
        return X(self.db[eventId])

    def delete(self, calendarId: str, eventId: str) -> X:
        self.calls.append(("delete", eventId, {}))
        del self.db[eventId]
        return X(None)

    def list(self, **kw: Any) -> X:
        items = [e for e in self.db.values() if "dateTime" in (e.get("start") or {})]
        return X({"items": items})


class FakeFreeBusy:
    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.hidden: set[str] = set()

    def query(self, body: dict) -> X:
        cals = {}
        for it in body["items"]:
            i = it["id"]
            if i in self.hidden:
                cals[i] = {"errors": [{"reason": "notFound"}]}
            else:
                cals[i] = (
                    {"busy": [{"start": body["timeMin"], "end": body["timeMax"]}]}
                    if i in self.busy
                    else {}
                )
        return X({"calendars": cals})


@pytest.fixture
def cal(tmp_path, monkeypatch):
    c = Calendar(Config({"operator": {"timezone": "America/New_York"}}), Store(tmp_path / "s.db"))
    ev, fb = FakeEvents(), FakeFreeBusy()

    class Svc:
        def events(self) -> FakeEvents:
            return ev

        def freebusy(self) -> FakeFreeBusy:
            return fb

    monkeypatch.setattr(c, "_svc", Svc)
    c.fake = ev  # type: ignore[attr-defined]
    c.fb = fb  # type: ignore[attr-defined]
    return c


def invitation(ev: FakeEvents, eid: str = "inv1", organizer_self: bool = False) -> None:
    ev.db[eid] = {
        "id": eid,
        "summary": "Their sync",
        "start": {"dateTime": t(13).isoformat()},
        "end": {"dateTime": t(14).isoformat()},
        "organizer": {"self": organizer_self, "email": "ben@example.org"},
        "attendees": [
            {"email": "ben@example.org", "responseStatus": "accepted", "organizer": True},
            {"email": "cy@example.org", "responseStatus": "tentative"},
            {"email": "me@example.org", "self": True, "responseStatus": "needsAction"},
        ],
    }


# ---------------------------------------------------------------- ownership
def test_a_meeting_ultra_sent_is_not_a_movable_block():
    tag = {"extendedProperties": {"private": {"ultra": "1"}}, "organizer": {"self": True}}
    assert is_ultra_block(tag)
    with_guest = {**tag, "attendees": [{"email": "b@x.org"}, {"email": "me@x.org", "self": True}]}
    assert not is_ultra_block(with_guest)
    kind = {"extendedProperties": {"private": {"ultra": "1", "ultra_kind": "meeting"}}}
    assert not is_ultra_block({**kind, "organizer": {"self": True}})
    room = {**tag, "attendees": [{"email": "room@x.org", "resource": True}]}
    assert is_ultra_block(room)


def test_cannot_move_or_delete_a_sent_meeting(cal):
    r = cal.create_meeting("Sync", t(10), t(10, 30), ["ben@example.org"], video=False)
    eid = r["event"]["id"]
    for fn in (
        lambda: cal.move_block("primary", eid, t(11), t(12)),
        lambda: cal.delete_block("primary", eid),
    ):
        with pytest.raises(CalendarError):
            fn()
    assert not [c for c in cal.fake.calls if c[0] in ("patch", "delete")]


# ---------------------------------------------------------------- week
def test_week_groups_by_day_and_lists_pending(cal, monkeypatch):
    # "pending" drops invitations that have ended, so pin the clock before the 13:00-14:00
    # invite (the test used the real clock and started failing at 14:00 on 2026-10-01)
    import ultra.calendar as calmod

    class Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return dt.datetime(2026, 10, 1, 9, 0, tzinfo=tz)

    monkeypatch.setattr(calmod.dt, "datetime", Frozen)
    invitation(cal.fake)
    cal.create_block("Focus", t(9, 0, 2), t(10, 0, 2))
    w = cal.week("2026-10-01")
    assert w["week_start"] == "2026-09-28"  # Monday
    assert len(w["days"]) == 7
    assert [e["summary"] for e in w["days"]["2026-10-01"]] == ["Their sync"]
    assert [e["summary"] for e in w["days"]["2026-10-02"]] == ["Focus"]
    assert [e["id"] for e in w["pending"]] == ["inv1"]
    with pytest.raises(CalendarError):
        cal.week("10/01/2026")


# ---------------------------------------------------------------- RSVP
def test_rsvp_changes_only_my_answer_and_notifies(cal):
    invitation(cal.fake)
    r = cal.rsvp("primary", "inv1", "declined", "Out that day")
    assert r["ok"] and r["event"]["my_response"] == "declined"
    call = next(c for c in cal.fake.calls if c[0] == "patch")
    assert call[2] == {"sendUpdates": "all"}
    atts = {a["email"]: a for a in call[1]["attendees"]}
    assert atts["cy@example.org"]["responseStatus"] == "tentative"
    assert atts["ben@example.org"]["responseStatus"] == "accepted"
    assert atts["me@example.org"]["comment"] == "Out that day"
    assert set(call[1]) == {"id", "attendees"}  # nothing else patched


def test_rsvp_refuses_bad_cases(cal):
    invitation(cal.fake, "own", organizer_self=True)
    with pytest.raises(CalendarError):
        cal.rsvp("primary", "own", "accepted")
    invitation(cal.fake)
    with pytest.raises(CalendarError):
        cal.rsvp("primary", "inv1", "maybe")
    cal.fake.db["notme"] = {
        **cal.fake.db["inv1"],
        "id": "notme",
        "attendees": [{"email": "b@x.org"}],
    }
    with pytest.raises(CalendarError):
        cal.rsvp("primary", "notme", "accepted")
    assert not [c for c in cal.fake.calls if c[0] == "patch"]


def test_rsvp_token_is_bound_single_use_and_expires(cal, tmp_path):
    invitation(cal.fake)
    rs = Rsvp(cal.store, cal)
    a = rs.ask("primary", "inv1", "accepted")
    with pytest.raises(InviteError):  # different response than confirmed
        rs.confirm(a["token"], "primary", "inv1", "declined")
    a = rs.ask("primary", "inv1", "accepted")
    assert rs.confirm(a["token"], "primary", "inv1", "accepted")["ok"]
    with pytest.raises(InviteError):  # replay
        rs.confirm(a["token"], "primary", "inv1", "accepted")
    b = rs.ask("primary", "inv1", "tentative")
    rs.tokens[b["token"]] = (*rs.tokens[b["token"]][:3], time.time() - 1)
    with pytest.raises(InviteError):
        rs.confirm(b["token"], "primary", "inv1", "tentative")
    with pytest.raises(InviteError):
        rs.ask("primary", "inv1", "yes")


# ---------------------------------------------------------------- repeating blocks
def test_repeating_block_has_an_end_no_guests_and_tag(cal):
    r = cal.create_recurring_block("Focus: writing", t(9), t(10), ["mo", "WE", "xx"], 4)
    assert r["ok"], r["problems"]
    body = cal.fake.calls[0][1]
    rule = body["recurrence"][0]
    assert rule.startswith("RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=2026102")
    assert "attendees" not in body and body["extendedProperties"]["private"]["ultra"] == "1"
    with pytest.raises(CalendarError):
        cal.create_recurring_block("x", t(9), t(10), ["MO"], 27)
    with pytest.raises(CalendarError):
        cal.create_recurring_block("x", t(9), t(10), [], 4)
    with pytest.raises(CalendarError):
        cal.create_recurring_block("x", t(8), t(17, 30), ["MO"], 4)


def test_delete_series_only_for_ultra_series(cal):
    r = cal.create_recurring_block("Focus", t(9), t(10), ["MO"], 2)
    master = r["event"]["id"]
    cal.fake.db["occ"] = {**cal.fake.db[master], "id": "occ", "recurringEventId": master}
    single = cal.create_block("One", t(11), t(12))["event"]["id"]
    with pytest.raises(CalendarError):
        cal.delete_series("primary", single)
    invitation(cal.fake)
    with pytest.raises(CalendarError):
        cal.delete_series("primary", "inv1")
    assert cal.delete_series("primary", "occ")["deleted"] == master
    assert master not in cal.fake.db


# ---------------------------------------------------------------- invites (two approvals)
def future(days: int = 3, h: int = 10) -> str:
    d = dt.datetime.now(NY).date() + dt.timedelta(days=days)
    return f"{d.isoformat()}T{h:02d}:00"


@pytest.fixture
def inv(cal):
    return Invites(cal.store, cal, "example.org", {"me@example.org"})


def draft(**kw: Any) -> dict[str, Any]:
    return {
        "summary": "Storage plan",
        "start": future(),
        "minutes": 30,
        "guests": "Ben Carter <ben@example.org>, dee@other.org, me@example.org, BEN@example.org",
        "description": "Agenda",
        "video": False,
        **kw,
    }


def test_invite_fields_are_validated(inv):
    d = inv.create(draft())["data"]
    assert d["guests"] == ["ben@example.org", "dee@other.org"]  # me dropped, deduped
    for bad in (
        {"summary": "  "},
        {"guests": "not-an-address"},
        {"guests": "me@example.org"},
        {"minutes": 2},
        {"minutes": 600},
        {"start": "tomorrow"},
        {"guests": ",".join(f"p{i}@example.org" for i in range(41))},
    ):
        with pytest.raises(InviteError):
            inv.create(draft(**bad))


def test_invite_full_flow_sends_exactly_what_was_approved(inv, cal):
    i = inv.create(draft(), "g-abc")
    with pytest.raises(InviteError):
        inv.review(i["id"])  # approval 1 first
    inv.approve(i["id"])
    rv = inv.review(i["id"])
    assert rv["checks"]["external"] == ["dee@other.org"]
    assert not [c for c in cal.fake.calls if c[0] == "insert"]  # nothing sent yet
    r = inv.confirm(i["id"], rv["token"])
    assert r["ok"], r["problems"]
    ins = [c for c in cal.fake.calls if c[0] == "insert"]
    assert len(ins) == 1 and ins[0][2]["sendUpdates"] == "all"
    assert sorted(a["email"] for a in ins[0][1]["attendees"]) == [
        "ben@example.org",
        "dee@other.org",
    ]
    assert r["invite"]["state"] == "SENT" and r["invite"]["event_id"]
    with pytest.raises(InviteError):  # token single use
        inv.confirm(i["id"], rv["token"])
    with pytest.raises(InviteError):
        inv.update(i["id"], {"summary": "changed"})  # sent invites are frozen
    assert inv.store.journal_recent(5)[0]["action"] == "invite_sent"


def test_edit_after_approval_voids_it_and_its_token(inv, cal):
    i = inv.create(draft())
    inv.approve(i["id"])
    rv = inv.review(i["id"])
    inv.update(i["id"], {"guests": "ben@example.org, eve@example.org"})
    with pytest.raises(InviteError):
        inv.confirm(i["id"], rv["token"])
    assert inv.get(i["id"])["state"] == "DRAFT"
    assert not [c for c in cal.fake.calls if c[0] == "insert"]


def test_tampered_store_or_expired_token_sends_nothing(inv, cal):
    import json

    i = inv.create(draft())
    inv.approve(i["id"])
    rv = inv.review(i["id"])
    d = inv.get(i["id"])["data"]
    d["guests"].append("mallory@evil.org")
    with inv._db() as c:  # a change that bypassed update()
        c.execute("UPDATE invites SET data=? WHERE id=?", (json.dumps(d), i["id"]))
    with pytest.raises(InviteError):
        inv.confirm(i["id"], rv["token"])
    j = inv.create(draft())
    inv.approve(j["id"])
    rv2 = inv.review(j["id"])
    with inv._db() as c:
        c.execute(
            "UPDATE invite_tokens SET expires=? WHERE token=?", (time.time() - 1, rv2["token"])
        )
    with pytest.raises(InviteError):
        inv.confirm(j["id"], rv2["token"])
    rv3 = inv.review(j["id"])  # a fresh, valid token for invite j...
    with pytest.raises(InviteError):  # ...is refused for invite i
        inv.confirm(i["id"], rv3["token"])
    assert not [c for c in cal.fake.calls if c[0] == "insert"]


def test_past_time_cannot_be_approved_or_sent(inv, cal):
    i = inv.create(draft(start=future(-1)))
    with pytest.raises(InviteError):
        inv.approve(i["id"])
    assert not [c for c in cal.fake.calls if c[0] == "insert"]


def test_review_shows_busy_and_unknown_guests(inv, cal):
    cal.fb.busy.add("ben@example.org")
    cal.fb.hidden.add("dee@other.org")
    i = inv.create(draft())
    inv.approve(i["id"])
    c = inv.review(i["id"])["checks"]
    assert c["busy"] == ["ben@example.org"] and c["unknown"] == ["dee@other.org"]


def test_invite_hash_ignores_guest_order():
    a = {
        "summary": "x",
        "start": "s",
        "minutes": 30,
        "guests": ["a@x", "b@x"],
        "description": "",
        "video": True,
        "location": "",
    }
    assert invite_hash(a) == invite_hash({**a, "guests": ["b@x", "a@x"]})
    assert invite_hash(a) != invite_hash({**a, "minutes": 31})


def test_create_meeting_reads_back_and_flags_differences(cal, monkeypatch):
    r = cal.create_meeting("Sync", t(10), t(10, 30), ["ben@example.org"], video=True)
    assert r["ok"]
    ins = cal.fake.calls[-1]
    assert ins[2]["conferenceDataVersion"] == 1 and ins[1]["guestsCanModify"] is False
    orig = cal.fake.get

    def drop_guest(calendarId: str, eventId: str) -> X:
        e = dict(orig(calendarId, eventId).execute())
        e["attendees"] = [a for a in e.get("attendees") or [] if a.get("self")]
        return X(e)

    monkeypatch.setattr(cal.fake, "get", drop_guest)
    r2 = cal.create_meeting("Sync", t(11), t(11, 30), ["ben@example.org"], video=False)
    assert "guest list differs from what was approved" in r2["problems"]


# ---------------------------------------------------------------- demo routes
def _api():
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    httpd.server_close()
    return api


def _call(api, method: str, path: str, body: Any = None, q: dict | None = None) -> Any:
    return api.dispatch(method, path, q or {}, body)[1]


def test_demo_week_rsvp_invite_and_series_routes():
    from ultra.server import ApiError

    api = _api()
    cal = api.demo_calendar
    today = dt.datetime.now(cal.tz).date()
    w = _call(api, "GET", "/api/cal/week", q={"day": [today.isoformat()]})
    assert len(w["days"]) == 7
    pend = [e for e in cal.events.values() if e["my_response"] == "needsAction"]
    assert pend, "demo needs an invitation to answer"
    e = pend[0]
    with pytest.raises(ApiError):  # confirm without a token
        _call(
            api, "POST", "/api/cal/rsvp", {"cal": "primary", "id": e["id"], "response": "accepted"}
        )
    a = _call(
        api, "POST", "/api/cal/rsvp/ask", {"cal": "primary", "id": e["id"], "response": "accepted"}
    )
    r = _call(
        api,
        "POST",
        "/api/cal/rsvp",
        {"cal": "primary", "id": e["id"], "response": "accepted", "token": a["token"]},
    )
    assert r["ok"] and cal.events[e["id"]]["my_response"] == "accepted"

    start = f"{(today + dt.timedelta(days=2)).isoformat()}T10:00"
    i = _call(
        api,
        "POST",
        "/api/invites",
        {"summary": "Plan", "start": start, "minutes": 30, "guests": "ben@example.org"},
    )
    with pytest.raises(ApiError):
        _call(api, "POST", f"/api/invites/{i['id']}/send", {"token": "x"})
    _call(api, "POST", f"/api/invites/{i['id']}/approve")
    rv = _call(api, "POST", f"/api/invites/{i['id']}/review")
    before = len(getattr(cal, "sent_invites", []))
    s = _call(api, "POST", f"/api/invites/{i['id']}/send", {"token": rv["token"]})
    assert s["ok"] and len(cal.sent_invites) == before + 1
    new = cal.events[s["event"]["id"]]
    assert not new["ultra"]  # a sent meeting is read-only in Ultra
    with pytest.raises(ApiError):
        _call(api, "POST", "/api/cal/delete", {"cal": "primary", "id": new["id"]})

    sr = _call(
        api,
        "POST",
        "/api/cal/series",
        {"summary": "Focus", "start": start, "minutes": 60, "weekdays": ["MO", "TH"], "weeks": 2},
    )
    assert sr["ok"] and sr["event"]["count"] >= 3
    with pytest.raises(ApiError):
        _call(
            api,
            "POST",
            "/api/cal/series",
            {"summary": "Focus", "start": start, "minutes": 60, "weekdays": ["XX"], "weeks": 2},
        )


def test_demo_meeting_context_and_log_card():
    api = _api()
    cal = api.demo_calendar
    e = next(x for x in cal.events.values() if x["attendees"] and not x["organizer_self"])
    key = f"c-primary~{e['id']}"
    ppl = _call(api, "GET", f"/api/item/people/{key}")
    names = " ".join(p.get("name", "") for p in ppl.get("people", []))
    assert "Ben" in names
    card = _call(api, "POST", "/api/ledger/stage-meeting", {"event": e})
    assert card["action"] == "log" and card["text"].startswith(f"Meeting: {e['summary']}")
    assert card["state"] == "staged"  # nothing written until Commit
    from ultra.server import ApiError

    with pytest.raises(ApiError):
        _call(api, "POST", "/api/ledger/stage-meeting", {"event": {}})
    with pytest.raises(ApiError):
        _call(api, "GET", "/api/item/people/c-primary~bad key!")


def test_failed_send_is_not_retried_with_the_same_token(inv, cal, monkeypatch):
    """No automatic retry: a token is spent even when Google refuses the insert."""
    i = inv.create(draft())
    inv.approve(i["id"])
    rv = inv.review(i["id"])

    def boom(*a: Any, **k: Any) -> Any:
        raise CalendarError("Google said no")

    monkeypatch.setattr(cal, "create_meeting", boom)
    with pytest.raises(InviteError):
        inv.confirm(i["id"], rv["token"])
    monkeypatch.undo()
    assert inv.get(i["id"])["state"] == "APPROVED"
    with pytest.raises(InviteError):
        inv.confirm(i["id"], rv["token"])
    assert not [c for c in cal.fake.calls if c[0] == "insert"]


def test_token_is_bound_to_its_own_invite_even_for_identical_invites(inv, cal):
    a = inv.create(draft())
    b = inv.create(draft())  # same fields, same hash
    inv.approve(a["id"])
    inv.approve(b["id"])
    tok_b = inv.review(b["id"])["token"]
    with pytest.raises(InviteError):
        inv.confirm(a["id"], tok_b)
    assert not [c for c in cal.fake.calls if c[0] == "insert"]


def test_sent_invite_cannot_be_reviewed_again(inv):
    i = inv.create(draft())
    inv.approve(i["id"])
    inv.confirm(i["id"], inv.review(i["id"])["token"])
    with pytest.raises(InviteError):
        inv.review(i["id"])


def test_item_key_from_a_body_is_validated():
    from ultra.server import ApiError

    api = _api()
    for bad in ("x-123", "g-bad key", "c-primary", "t-notauuid", ""):
        with pytest.raises(ApiError):
            _call(api, "POST", "/api/item/suggest", {"key": bad})


def test_series_route_rejects_any_unknown_weekday():
    from ultra.server import ApiError

    api = _api()
    today = dt.datetime.now(api.demo_calendar.tz).date()
    start = f"{(today + dt.timedelta(days=2)).isoformat()}T10:00"
    n = len(api.demo_calendar.events)
    with pytest.raises(ApiError):  # one bad day spoils the request; nothing silently dropped
        _call(
            api,
            "POST",
            "/api/cal/series",
            {
                "summary": "Focus",
                "start": start,
                "minutes": 60,
                "weekdays": ["MO", "XX"],
                "weeks": 2,
            },
        )
    assert len(api.demo_calendar.events) == n


def test_pending_skips_ended_invitations_and_counts_each_once(cal, monkeypatch):
    invitation(cal.fake, "past")
    cal.fake.db["past"].update(
        start={"dateTime": t(9, 0, 29, 9).isoformat()},
        end={"dateTime": t(10, 0, 29, 9).isoformat()},
    )
    invitation(cal.fake, "later")
    cal.fake.db["later"].update(
        start={"dateTime": t(23, 0, 1).isoformat()}, end={"dateTime": t(1, 0, 2).isoformat()}
    )  # spans midnight: on two days, listed once

    class Now(dt.datetime):
        @classmethod
        def now(cls, tz: Any = None) -> dt.datetime:  # type: ignore[override]
            return dt.datetime(2026, 9, 30, 12, 0, tzinfo=tz)

    monkeypatch.setattr("ultra.calendar.dt.datetime", Now)
    w = cal.week("2026-09-30")
    assert [e["id"] for e in w["pending"]] == ["later"]
    assert sum(1 for d in w["days"].values() for e in d if e["id"] == "later") == 2


def test_google_clients_are_not_shared_between_threads(monkeypatch):
    """httplib2 is not thread-safe; a client shared by two threads corrupts TLS."""
    import threading

    from ultra import google_auth

    class Creds:
        valid = True

    built: list[int] = []
    monkeypatch.setattr(google_auth, "credentials", lambda cfg, cap: Creds())
    monkeypatch.setattr(google_auth, "_services", {})

    def fake_build(api: str, version: str, credentials: Any, cache_discovery: bool) -> Any:
        built.append(threading.get_ident())
        return type("Svc", (), {})()

    disc = pytest.importorskip("googleapiclient.discovery")
    monkeypatch.setattr(disc, "build", fake_build)
    got: dict[str, Any] = {}

    def grab(name: str) -> None:
        got[name] = google_auth.service(Config({}), "calendar", "v3", "calendar")
        got[name + "2"] = google_auth.service(Config({}), "calendar", "v3", "calendar")

    ths = [threading.Thread(target=grab, args=(n,)) for n in ("a", "b")]
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    assert got["a"] is got["a2"] and got["b"] is got["b2"]  # reused within a thread
    assert got["a"] is not got["b"]  # never shared across threads
    assert len(built) == 2
