"""v0.9 tests: Day check-in plan, end-of-day report, exports, journal coverage."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any
from zoneinfo import ZoneInfo

from ultra.config import Config
from ultra.day import Day
from ultra.store import Store

TZ = ZoneInfo("America/New_York")
DAY = "2026-10-05"


def ev(s: str, e: str, summary: str, **kw: Any) -> dict[str, Any]:
    return {
        "id": summary[:6],
        "start": f"{DAY}T{s}:00-04:00",
        "end": f"{DAY}T{e}:00-04:00",
        "summary": summary,
        "attendees": kw.pop("att", [1, 2]),
        **kw,
    }


CAL = {
    "events": [
        ev("10:00", "11:00", "Staff meeting"),
        ev("13:00", "13:30", "1:1", my_response="needsAction"),
        ev("14:00", "15:00", "Declined thing", my_response="declined"),
        {"id": "ad", "start": DAY, "end": DAY, "summary": "Holiday", "all_day": True},
    ]
}
STREAM = [
    {
        "key": "g-1",
        "source": "email",
        "from": "Ben",
        "subject": "Need answer",
        "court": "MINE",
        "badges": ["READY"],
        "ts": "2026-10-05T08:00",
    },
    {
        "key": "g-2",
        "source": "email",
        "from": "Cy",
        "subject": "FYI",
        "court": "MINE",
        "badges": [],
        "ts": "2026-10-04T08:00",
    },
    {
        "key": "g-3",
        "source": "email",
        "from": "Dee",
        "subject": "Waiting long",
        "court": "WAITING",
        "waiting_days": 5,
    },
    {
        "key": "g-4",
        "source": "email",
        "from": "Eli",
        "subject": "Waiting short",
        "court": "WAITING",
        "waiting_days": 1,
    },
    {"key": "t-x", "source": "task", "from": "", "subject": "a task row", "court": "MINE"},
]
TASKS = {
    "a": {
        "id": "a",
        "summary": "Overdue thing",
        "priority": "LOW",
        "status": "TODO",
        "due_date": "2026-10-01",
    },
    "b": {
        "id": "b",
        "summary": "Due today",
        "priority": "MEDIUM",
        "status": "TODO",
        "due_date": DAY,
    },
    "c": {"id": "c", "summary": "Critical", "priority": "CRITICAL", "status": "TODO"},
    "d": {"id": "d", "summary": "High started", "priority": "HIGH", "status": "IN_PROGRESS"},
    "e": {"id": "e", "summary": "Low", "priority": "LOW", "status": "TODO"},
}


def mk(tmp_path, cal=CAL, fail_cal=False) -> Day:
    def day_fn(d: str) -> dict:
        if fail_cal:
            raise RuntimeError("calendar down")
        return cal

    return Day(
        Store(tmp_path / "s.db"), TZ, day_fn, lambda: STREAM, lambda: TASKS, None, "Ada", (9, 17)
    )


def test_plan_free_windows_skip_declined_and_all_day(tmp_path):
    p = mk(tmp_path).plan(DAY)
    assert [m["summary"] for m in p["meetings"]] == ["Staff meeting", "1:1"]
    assert [f["label"] for f in p["free"]] == ["9:00am-10:00am", "11:00am-1:00pm", "1:30pm-5:00pm"]
    assert p["meeting_minutes"] == 90


def test_plan_orders_work_and_splits_tasks(tmp_path):
    p = mk(tmp_path).plan(DAY)
    assert [i["key"] for i in p["mine"]] == ["g-1", "g-2"]  # READY first; task rows excluded
    assert [i["key"] for i in p["waiting"]] == ["g-3"]  # only 3+ days
    assert [t["id"] for t in p["overdue"]] == ["a"] and [t["id"] for t in p["due_today"]] == ["b"]
    assert [t["id"] for t in p["top_tasks"]] == ["c", "d"]
    # blocks fit inside free windows and never overlap a meeting
    busy = [(600, 660), (780, 810)]
    for b in p["blocks"]:
        assert 540 <= b["start"] < b["end"] <= 1020
        assert all(b["end"] <= a or b["start"] >= e for a, e in busy)
    assert p["blocks"][0]["key"] == "g-1"


def test_plan_survives_a_calendar_failure(tmp_path):
    p = mk(tmp_path, fail_cal=True).plan(DAY)
    assert p["warnings"] and "Calendar unavailable" in p["warnings"][0]
    assert p["mine"] and p["free"]  # still useful


def test_report_counts_only_this_day_and_collapses_repeats(tmp_path):
    d = mk(tmp_path)
    base = dt.datetime(2026, 10, 5, 12, tzinfo=TZ).timestamp()
    rows = [
        ("archive", True, {"subject": "News"}),
        ("archive", True, {"subject": "News"}),
        ("sent", True, {"subject": "Re: plan"}),
        ("ledger_task_status", True, {"status": "DONE"}),
        ("slack_sent", True, None),
        ("send_failed", False, "x"),
    ]
    for a, ok, det in rows:
        d.store.journal(a, "t", ok, det)
    d.store.journal("archive", "old", True, None)
    with d.store._conn() as c:
        c.execute("UPDATE journal SET ts=?", (base,))
        c.execute("UPDATE journal SET ts=? WHERE target='old'", (base - 86400,))  # yesterday
    r = d.report(DAY)
    assert r["counts"]["archive"] == 2 and r["counts"]["sent"] == 1 and r["failures"] == 1
    text = d.report_text(r)
    assert "Archived: News (x2)" in text and "Task set to DONE" in text
    assert "Sent 1 emails and 1 Slack messages" in text
    assert "Meetings: Staff meeting; 1:1." in text


def test_ics_and_csv_exports(tmp_path):
    d = mk(tmp_path)
    ics = d.r_ics({"day": [DAY]}, None, None)
    assert ics.ctype.startswith("text/calendar") and ics.name == f"agenda-{DAY}.ics"
    assert "SUMMARY:Staff meeting" in ics.text and "Declined" not in ics.text
    assert "DTSTART:20261005T140000Z" in ics.text and "DTSTART;VALUE=DATE:20261005" in ics.text
    assert "@" not in ics.text.replace("@ultra-workstation", "")  # no attendee emails
    d.store.journal("sent", "1", True, {"subject": "a, b"})
    csv = d.r_csv({"day": [dt.datetime.now(TZ).date().isoformat()]}, None, None)
    assert csv.text.splitlines()[0] == "time,action,target,ok,detail" and "sent" in csv.text


def test_bad_day_rejected(tmp_path):
    import pytest

    from ultra.server import ApiError

    with pytest.raises(ApiError):
        mk(tmp_path).r_plan({"day": ["2026-1-5"]}, None, None)


def test_day_routes_and_text_download_in_demo():
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    httpd.server_close()
    st, p = api.dispatch("GET", "/api/day/plan", {}, None)
    assert st == 200 and "Plan for" in p["text"]
    st, r = api.dispatch("GET", "/api/day/report", {}, None)
    assert "End of day" in r["text"] and r["counts"].get("archive") == 2
    st, n = api.dispatch("POST", "/api/day/plan/note", {}, {})
    assert n["text"]
    st, f = api.dispatch("GET", "/api/day/agenda.ics", {}, None)
    assert f.text.startswith("BEGIN:VCALENDAR")


def test_report_can_become_an_unlinked_staged_card_only():
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    httpd.server_close()
    _st, card = api.dispatch(
        "POST",
        "/api/ledger/stage-briefing",
        {},
        {"key": "", "text": "End of day.", "allow_unlinked": True, "title": "EOD"},
    )
    assert card["state"] == "staged" and card["text"] == "End of day."
    assert not [c for c in api.demo_writer.calls if c[0] == "log"]


def test_calendar_writes_and_slack_sends_are_journaled(tmp_path):
    from ultra.rules import Rules
    from ultra.today import Today

    store = Store(tmp_path / "s.db")

    class Cal:
        def __init__(self) -> None:
            self.store = store
            self.tz = TZ

        def parse_local(self, s: str) -> dt.datetime:
            return dt.datetime.fromisoformat(s).replace(tzinfo=TZ)

        def create_block(self, *a: Any, **k: Any) -> dict:
            return {"ok": True, "event": {"id": "e1"}}

        def delete_block(self, cal: str, eid: str) -> dict:
            return {"ok": True, "deleted": eid}

    t = Today(Cal(), lambda k: {}, Rules(me=set()))
    t.r_block({}, {"summary": "Focus: x", "start": f"{DAY}T10:00", "minutes": 30}, None)
    t.r_delete({}, {"cal": "primary", "id": "e1abc2def3"}, None)
    acts = [r["action"] for r in store.journal_recent(10)]
    assert "cal_block" in acts and "cal_delete" in acts
    assert json.loads(store.journal_between(0, 2e10)[0]["detail"])["summary"] == "Focus: x"


def test_hidden_ticket_prefix_is_scrubbed_from_plan_text_and_ai_note(tmp_path):
    class AI:
        enabled = True
        hide_prefix = "INT-"

        def _gen(self, prompt: str, system: str, max_tokens: int = 0, **kw: Any) -> Any:
            assert "INT-" not in prompt and "start with INT-" in system
            return type(
                "R",
                (),
                {"text": "Do the portal work (INT-37) first.", "model": "m", "seconds": 0.1},
            )()

    tasks = {
        "z": {
            "id": "z",
            "summary": "Portal home (Jira INT-37). Verify.",
            "priority": "HIGH",
            "status": "TODO",
        }
    }
    d = Day(Store(tmp_path / "s.db"), TZ, lambda d: CAL, lambda: [], lambda: tasks, AI(), "Ada")
    p = d.plan(DAY)
    assert "INT-37" not in d.plan_text(p) and "Portal home. Verify." in d.plan_text(p)
    note = d.r_plan_note({}, {"day": DAY}, None)
    assert "INT-" not in note["text"]
