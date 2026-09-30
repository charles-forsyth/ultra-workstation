"""Demo mode: synthetic data only, for screenshots, docs and UI tests.

Every name, address and id here is invented (example.org). Real mail is never read
in demo mode: the adapters are not constructed at all.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from ultra.server import Api

STREAM: list[dict[str, Any]] = [
    {
        "key": "g-100",
        "source": "email",
        "from": "Ben Carter",
        "addr": "ben@example.org",
        "subject": "Lab project handover",
        "snippet": "The project is ready. Please work with Ada to hand it over.",
        "ts": "2026-09-29T09:14:00-04:00",
        "court": "MINE",
        "badges": ["VIP", "READY"],
        "waiting_days": 1,
    },
    {
        "key": "s-200",
        "source": "slack",
        "from": "Cy Dunn",
        "addr": "",
        "subject": "Group DM",
        "snippet": "Budget updated, lock process is active.",
        "ts": "2026-09-29T08:02:00-04:00",
        "court": "MINE",
        "badges": [],
        "waiting_days": 0,
    },
    {
        "key": "k-300",
        "source": "ticket",
        "from": "Service desk",
        "addr": "desk@example.org",
        "subject": "RITM0000001 comments added",
        "snippet": "Requester asked for an update on storage access.",
        "ts": "2026-09-28T16:40:00-04:00",
        "court": "MINE",
        "badges": [],
        "waiting_days": 1,
    },
    {
        "key": "g-400",
        "source": "email",
        "from": "Dee Evans",
        "addr": "dee@example.org",
        "subject": "Security review items",
        "snippet": "Thanks, I will get back to you on the remaining items.",
        "ts": "2026-09-25T11:00:00-04:00",
        "court": "WAITING",
        "badges": [],
        "waiting_days": 4,
    },
]

THREADS: dict[str, list[dict[str, str]]] = {
    "g-100": [
        {
            "from": "Ben Carter <ben@example.org>",
            "ts": "2026-09-28 17:54",
            "body": "The project is ready. Please work with Ada to hand it over. "
            "Dee Evans signed off on the budget, and Eli Fox will move the "
            "lab-share data once the ada-lab APIs are on. Grant NSF-2400001 covers "
            "the storage. Also looping in Grace for the Hopper Lab side.",
        },
        {
            "from": "Ada Lovelace <ada@example.org>",
            "ts": "2026-09-29 09:14",
            "body": "Great, when will the account be ready for my students?",
        },
    ]
}

CONTEXT = {
    "ben@example.org": {
        "name": "Ben Carter",
        "netid": "bcarter",
        "title": "Deputy Director",
        "labs": ["Lovelace Lab"],
        "projects": ["ada-lab"],
        "open_tasks": [
            {
                "id": "t-1",
                "summary": "Hand over ada-lab to Ada: turn on APIs, add Ada.",
            },
            {
                "id": "t-2",
                "summary": "Confirm the budget alert covers the new account.",
            },
        ],
        "interactions": [{"id": "i-1", "summary": "Ben asked for the ada-lab handover plan."}],
        "interaction_count": 1,
        "matched_by": "address",
    }
}


def _rel_day(n: int) -> str:
    """A date n days from today, so the demo always shows overdue / today badges."""
    import datetime as _dt

    return (_dt.date.today() + _dt.timedelta(days=n)).isoformat() + " 00:00:00"


DEMO_TASKS: list[dict[str, Any]] = [
    {
        "id": "55555555-5555-4555-8555-555555555551",
        "summary": "Send Ben the handover plan",
        "status": "TODO",
        "priority": "CRITICAL",
        "assigned_to": "adal",
        "due_date": _rel_day(-1),
    },
    {
        "id": "55555555-5555-4555-8555-555555555552",
        "summary": "Confirm the budget alert covers the new account",
        "status": "IN_PROGRESS",
        "priority": "HIGH",
        "assigned_to": "adal",
        "due_date": _rel_day(0),
    },
    {
        "id": "55555555-5555-4555-8555-555555555553",
        "summary": "Waiting on security review",
        "status": "BLOCKED",
        "priority": "MEDIUM",
        "assigned_to": "adal",
    },
    {
        "id": "55555555-5555-4555-8555-555555555554",
        "summary": "Tidy lab storage notes",
        "status": "TODO",
        "priority": "LOW",
        "assigned_to": "adal",
        "due_date": _rel_day(9),
    },
]


def register(api: Api) -> None:
    import tempfile
    from pathlib import Path

    from ultra.store import Store
    from ultra.tasks import slack_done, slack_undone, slack_visible, snooze_task, task_rows

    tstore = Store(Path(tempfile.mkdtemp(prefix="ultra-demo-tasks-")) / "t.db")
    tasks = {t["id"]: dict(t) for t in DEMO_TASKS}

    def items() -> list[dict[str, Any]]:
        rows = [s for s in STREAM if s["source"] != "slack" or slack_visible(tstore, s)]
        return rows + task_rows(list(tasks.values()), tstore)

    def stream(q: dict, body: Any, m: re.Match[str]) -> dict:
        from ultra.live import FILTERS

        f = (q.get("filter") or ["mine"])[0]
        keep = FILTERS.get(f, FILTERS["mine"])
        rows = items()
        counts = {k: sum(1 for s in rows if fn(s)) for k, fn in FILTERS.items()}
        return {"items": [s for s in rows if keep(s)], "counts": counts, "demo": True}

    def thread(q: dict, body: Any, m: re.Match[str]) -> dict:
        key = m.group(1)
        if key.startswith("t-"):
            t = tasks.get(key[2:])
            if not t:
                return {"key": key, "task": None, "error": "not found", "messages": []}
            return {
                "key": key,
                "messages": [],
                "task": {"due_date": "", **t, "details": {}, "snoozed_until": None},
                "links": [
                    {
                        "id": DEMO_IDS["ben@example.org"],
                        "name": "Ben Carter (bcarter)",
                        "type": "Researcher",
                        "edge": "REFERENCED_IN",
                    }
                ],
            }
        out: dict[str, Any] = {"key": key, "messages": THREADS.get(key, [])}
        if key.startswith("s-"):
            row = next((s for s in STREAM if s["key"] == key), {})
            out["reply_target"] = {
                "channel_id": DEMO_SLACK_CH,
                "thread_ts": "",
                "label": row.get("subject", "Slack"),
            }
        return out

    def task_action(q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        t = tasks.get(str(b.get("id", "")))
        if not t:
            from ultra.server import ApiError

            raise ApiError(404, "task not found")
        a = b.get("action")
        st = {"complete": "DONE", "start": "IN_PROGRESS", "block": "BLOCKED", "reopen": "TODO"}.get(
            str(a)
        )
        if st:
            t["status"] = st
        elif a == "priority":
            t["priority"] = str(b.get("priority"))
        elif a == "due":
            from ultra.ledger_write import WriteError, check_due
            from ultra.server import ApiError

            want = str(b.get("due") or "").strip()
            try:
                t["due_date"] = (check_due(want) + " 00:00:00") if want else ""
            except WriteError as e:
                raise ApiError(400, str(e)) from e
            return {"ok": True, "due_date": want}
        elif a == "snooze":
            return {"ok": True, "until": snooze_task(tstore, t["id"], int(b.get("days", 1)))}
        return {"ok": True}

    def sdone(q: dict, body: Any, m: re.Match[str]) -> dict:
        key = str((body or {}).get("key", ""))
        row = next((s for s in STREAM if s["key"] == key), {})
        slack_done(tstore, key, int(row.get("ts_ms") or 10**13))
        return {"ok": True}

    def sundone(q: dict, body: Any, m: re.Match[str]) -> dict:
        slack_undone(tstore, str((body or {}).get("key", "")))
        return {"ok": True}

    api.add("POST", r"/api/task/action", task_action)
    api.add("POST", r"/api/slack/done", sdone)
    api.add("POST", r"/api/slack/undone", sundone)

    def context(q: dict, body: Any, m: re.Match[str]) -> dict:
        return CONTEXT.get(m.group(1), {"unresolved": True})

    api.add("GET", r"/api/stream", stream)
    api.add("GET", r"/api/thread/([A-Za-z0-9_-]+)", thread)
    api.add("GET", r"/api/context/([^/]+)", context)
    _register_composer(api)
    _register_desk(api)
    _register_tools(api)
    _register_today(api)


class DemoResearch:
    """Canned research client: nothing runs and nothing is spent."""

    enabled = True
    dashboard_url = ""
    depth, breadth = 1, 3

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {"ok": True, "error": ""}

    def search(self, q: str, limit: int = 5) -> dict[str, Any]:
        return {
            "query": q,
            "answer": f"Demo: two earlier reports discuss {q[:40]}.",
            "matches": [{"session_id": 7, "score": 0.81, "prompt": "Lab storage options"}],
            "model": "demo",
        }

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "id": 7,
                "prompt": "Lab storage options",
                "status": "completed",
                "created_at": "2026-09-28T10:00:00",
            }
        ]

    def show(self, sid: int) -> dict[str, Any]:
        return {
            "id": sid,
            "prompt": "Lab storage options",
            "status": "completed",
            "result": "## Summary\n\nDemo report text about storage tiers.",
        }

    def estimate(self, prompt: str, depth: int, breadth: int, upload: Any) -> dict[str, Any]:
        return {
            "prompt": prompt,
            "depth": depth,
            "breadth": breadth,
            "cost_usd": 0.95,
            "note": "Demo estimate.",
        }

    def start(self, prompt: str, depth: int, breadth: int, thread: str | None) -> dict:
        self.started.append({"prompt": prompt, "thread": bool(thread)})
        return {"session_id": 100 + len(self.started), "status": "running", "demo": True}

    def _thread_file(self, text: str) -> Any:
        import tempfile
        from pathlib import Path

        fd, name = tempfile.mkstemp()
        import os

        os.close(fd)
        return Path(name)


class DemoAI:
    enabled = True
    model = "demo"

    def web_search(self, q: str, context: str = "") -> dict[str, Any]:
        return {
            "text": f"Demo web answer about {q[:60]}.",
            "grounded": True,
            "sources": [{"title": "example.org", "url": "https://example.org/"}],
            "queries": [q],
            "model": "demo",
            "seconds": 0,
        }

    def explain(self, passage: str, thread: str, operator: str) -> Any:
        from ultra.ai import Result

        return Result(f"Demo: '{passage[:40]}' is explained here.", "demo", 0, 0.0)

    def _gen(self, prompt: str, system: str, max_tokens: int = 0, **kw: Any) -> Any:
        from ultra.ai import Result

        return Result(
            "- Protect 3:30-5:00 for the handover plan.\n- Answer Ben first: he is waiting "
            "on you.\n- Dee can wait until tomorrow.",
            "demo",
            0,
            0.0,
        )

    def briefing(self, context_text: str, operator: str) -> Any:
        from ultra.ai import Result

        md = (
            "## What this is about\n"
            "Ben Carter is handing the ada-lab cloud project over to Ada, with storage "
            "funded by NSF-2400001 [M1, L:11111111].\n\n"
            "## History\n"
            "* 2026-09-14: Kickoff for ada-lab; Ben is the admin contact [L:11111111].\n"
            "* 2026-09-22: Dee Evans approved the storage budget [L:11111111].\n"
            "* 2026-09-25: Eli Fox sized the lab-share move at 6 TB [L:11111111].\n"
            "* 2026-09-28: Ben wrote that the project is ready to hand over [M1].\n\n"
            "## Issues and open items\n"
            "* The APIs must be on before Eli can move lab-share [M1].\n\n"
            "## Current state\n"
            "Ada asked when the account will be ready for her students [M2].\n\n"
            "## Suggested next steps\n"
            "* Turn on the ada-lab APIs, then tell Eli to start the move [M1].\n"
        )
        return Result(md, "demo", 0, 0.0)

    def possible_matches(
        self, item_text: str, candidates: list[dict[str, Any]], known: list[str]
    ) -> list[dict[str, Any]]:
        return [
            {**c, "why": "'the storage' in message 1"}
            for c in candidates
            if c.get("name") == "Campus Storage"
        ][:1]


def _register_tools(api: Api) -> None:
    import tempfile
    from pathlib import Path

    from ultra.audio import Audio
    from ultra.config import Config
    from ultra.store import Store
    from ultra.tools import Tools

    store = Store(Path(tempfile.mkdtemp(prefix="ultra-demo-tools-")) / "t.db")
    research = DemoResearch()
    api.demo_research = research  # type: ignore[attr-defined]
    audio = Audio(Config({}), store, type("Off", (), {"enabled": False})())

    def text(key: str, for_speech: bool = False) -> str:
        return "\n\n".join(f"{m['from']}: {m['body']}" for m in THREADS.get(key, []))

    Tools(research, DemoAI(), audio, text, "Ada").register(api)


# ---------------------------------------------------------------- demo calendar
class DemoCalendar:
    """In-memory calendar with the same surface as ultra.calendar.Calendar.

    Seeded with invented meetings on today's date. Writes follow the real rules: only
    blocks tagged ultra can be moved or deleted; a verify step re-reads the event.
    """

    def __init__(self) -> None:
        import datetime as dt
        from zoneinfo import ZoneInfo

        self.tz = ZoneInfo("America/New_York")
        self.state: dict[str, Any] = {"ok": True, "error": ""}
        self.events: dict[str, dict[str, Any]] = {}
        self.n = 0
        today = dt.datetime.now(self.tz).date()

        def at(h: int, m: int = 0, day: int = 0) -> str:
            return dt.datetime.combine(
                today + dt.timedelta(days=day), dt.time(h, m), self.tz
            ).isoformat()

        seed = [
            (
                "Storage planning with Ben",
                at(10),
                at(10, 30),
                False,
                [
                    ("ben@example.org", "Ben Carter", "accepted"),
                    ("ada@example.org", "Ada Lovelace", "accepted"),
                ],
                "https://meet.example.org/abc",
                "Agenda: quota for the new lab share.",
            ),
            (
                "Weekly team sync",
                at(13),
                at(14),
                False,
                [
                    ("cy@example.org", "Cy Young", "needsAction"),
                    ("ben@example.org", "Ben Carter", "declined"),
                    ("ada@example.org", "Ada Lovelace", "accepted"),
                ],
                "",
                "",
            ),
            ("Focus: budget alert check", at(15), at(15, 30), True, [], "", "Made by Ultra."),
            (
                "Vendor review",
                at(11, 0, 1),
                at(12, 0, 1),
                False,
                [("cy@example.org", "Cy Young", "accepted")],
                "",
                "",
            ),
        ]
        for summary, s, e, mine, att, link, desc in seed:
            self.n += 1
            self.events[f"demo{self.n:04d}"] = {
                "id": f"demo{self.n:04d}",
                "cal": "primary",
                "summary": summary,
                "start": s,
                "end": e,
                "all_day": False,
                "visibility": "default",
                "color": "11" if mine else "",
                "status": "confirmed",
                "type": "default",
                "organizer_self": mine,
                "organizer": "ada@example.org" if mine else "ben@example.org",
                "my_response": "accepted",
                "attendees": [
                    {
                        "email": a,
                        "name": n,
                        "response": r,
                        "self": a == "ada@example.org",
                        "organizer": False,
                        "resource": False,
                    }
                    for a, n, r in att
                ],
                "attendee_count": len(att),
                "link": link,
                "html_link": "",
                "location": "",
                "has_description": bool(desc),
                "ultra": mine,
                "recurring": False,
                "description": desc,
            }

    def parse_local(self, s: str) -> Any:
        import datetime as dt

        from ultra.calendar import CalendarError

        try:
            t = dt.datetime.fromisoformat(s)
        except (TypeError, ValueError) as e:
            raise CalendarError(f"bad time: {s!r}") from e
        return t.replace(tzinfo=self.tz) if t.tzinfo is None else t.astimezone(self.tz)

    def day(self, day: str, fresh: bool = False) -> dict[str, Any]:
        evs = sorted(
            (e for e in self.events.values() if e["start"][:10] == day), key=lambda e: e["start"]
        )
        return {
            "day": day,
            "tz": "America/New_York",
            "age": 0.0,
            "work_hours": ["08:00", "17:00"],
            "events": [{k: v for k, v in e.items() if k != "description"} for e in evs],
        }

    def event(self, cal: str, eid: str) -> dict[str, Any]:
        from ultra.calendar import CalendarError

        if eid not in self.events:
            raise CalendarError("not found")
        return dict(self.events[eid])

    def _check(self, summary: str, start: Any, end: Any) -> None:
        from ultra.calendar import MAX_BLOCK_MIN, CalendarError

        if not summary.strip():
            raise CalendarError("A block needs a title.")
        mins = (end - start).total_seconds() / 60
        if mins < 5 or mins > MAX_BLOCK_MIN:
            raise CalendarError("A block must be between 5 minutes and 8 hours.")

    def create_block(
        self,
        summary: str,
        start: Any,
        end: Any,
        description: str = "",
        personal: bool = False,
        color: str = "",
        source: str = "",
    ) -> dict:
        self._check(summary, start, end)
        self.n += 1
        eid = f"demo{self.n:04d}"
        self.events[eid] = {
            "id": eid,
            "cal": "primary",
            "summary": summary.strip(),
            "start": start.isoformat(),
            "end": end.isoformat(),
            "all_day": False,
            "visibility": "private" if personal else "default",
            "color": "11",
            "status": "confirmed",
            "type": "default",
            "organizer_self": True,
            "organizer": "ada@example.org",
            "my_response": "",
            "attendees": [],
            "attendee_count": 0,
            "link": "",
            "html_link": "",
            "location": "",
            "has_description": bool(description),
            "ultra": True,
            "recurring": False,
            "description": description,
        }
        return {"ok": True, "problems": [], "event": dict(self.events[eid])}

    def _owned(self, eid: str) -> dict[str, Any]:
        from ultra.calendar import CalendarError

        e = self.events.get(eid)
        if not e or not e["ultra"]:
            raise CalendarError(
                "Ultra only changes blocks it created. Edit this event in Google Calendar."
            )
        return e

    def move_block(
        self, cal: str, eid: str, start: Any, end: Any, summary: str | None = None
    ) -> dict:
        e = self._owned(eid)
        self._check(summary if summary is not None else e["summary"], start, end)
        e["start"], e["end"] = start.isoformat(), end.isoformat()
        if summary is not None:
            e["summary"] = summary.strip()
        return {"ok": True, "problems": [], "event": dict(e)}

    def delete_block(self, cal: str, eid: str) -> dict:
        self._owned(eid)
        del self.events[eid]
        return {"ok": True, "deleted": eid}

    def freebusy(self, emails: list[str], start: Any, end: Any, min_minutes: int) -> dict:
        import datetime as dt

        from ultra.calendar import free_windows

        busy = [
            (dt.datetime.fromisoformat(e["start"]), dt.datetime.fromisoformat(e["end"]))
            for e in self.events.values()
            if e["my_response"] != "declined"
        ]
        wins = []
        d = start.date()
        while dt.datetime.combine(d, dt.time(0), self.tz) < end:
            ws = max(start, dt.datetime.combine(d, dt.time(8), self.tz))
            we = min(end, dt.datetime.combine(d, dt.time(17), self.tz))
            if we > ws and d.weekday() < 5:
                wins += free_windows(busy, ws, we, min_minutes)
            d += dt.timedelta(days=1)
        return {
            "windows": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in wins],
            "unknown": [x for x in emails if not x.endswith("@example.org")],
            "checked": emails,
        }


def _register_today(api: Api) -> None:
    from ultra.rules import Rules
    from ultra.today import Today

    cal = DemoCalendar()
    api.demo_calendar = cal  # type: ignore[attr-defined]

    def info(key: str) -> dict[str, Any]:
        msgs = THREADS.get(key, [])
        row = next((s for s in STREAM if s["key"] == key), {})
        return {
            "subject": row.get("subject", ""),
            "summary": f"Last from {msgs[-1]['from']}: {msgs[-1]['body'][:300]}" if msgs else "",
            "draft": "",
            "task_id": key[2:] if key.startswith("t-") else "",
            "url": "",
        }

    Today(cal, info, Rules(me={"ada@example.org"})).register(api)

    import datetime as dt
    import tempfile
    from pathlib import Path
    from zoneinfo import ZoneInfo

    from ultra.day import Day
    from ultra.store import Store

    store = Store(Path(tempfile.mkdtemp(prefix="ultra-demo-day-")) / "day.db")
    api.demo_day_store = store  # type: ignore[attr-defined]
    now = dt.datetime.now(ZoneInfo("America/New_York")).timestamp()
    for i, (action, target, detail) in enumerate(
        [
            ("archive", "t1", {"subject": "Newsletter"}),
            ("archive", "t2", {"subject": "Weekly digest"}),
            ("sent", "1", {"subject": "Re: Lab project handover", "to": ["ben@example.org"]}),
            ("ledger_log", "i-1", {"links": 2}),
            ("ledger_task_status", "t-1", {"status": "DONE"}),
            ("slack_done", "s-D0DEMO1", None),
            ("cal_block", "b1", {"summary": "Focus: budget alert check"}),
        ]
    ):
        store.journal(action, target, True, detail)
        with store._conn() as c:  # spread them over the morning
            c.execute(
                "UPDATE journal SET ts=? WHERE id=(SELECT MAX(id) FROM journal)",
                (now - 3600 * (7 - i) / 2,),
            )

    def stream_rows() -> list[dict[str, Any]]:
        return [dict(s) for s in STREAM]

    def tasks() -> dict[str, dict[str, Any]]:
        return {t["id"]: t for t in DEMO_TASKS if t.get("status") != "DONE"}

    Day(
        store,
        ZoneInfo("America/New_York"),
        cal.day,
        stream_rows,
        tasks,
        DemoAI(),
        "Ada",
        (9, 17),
    ).register(api)


# ---------------------------------------------------------------- demo ledger
DEMO_IDS = {
    "ben@example.org": "11111111-1111-4111-8111-111111111111",
    "cy@example.org": "22222222-2222-4222-8222-222222222222",
    "lab": "33333333-3333-4333-8333-333333333333",
    "project": "44444444-4444-4444-8444-444444444444",
}


class DemoLedger:
    """Invented people only. Same method names as ultra.ledger.Ledger."""

    enabled = True

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}
        self.added: list[dict[str, Any]] = []

    def resolve(self, addr: str, name: str = "") -> dict[str, Any] | None:
        if addr == "ben@example.org":
            return {
                "id": DEMO_IDS[addr],
                "name": "Ben Carter (bcarter)",
                "netid": "bcarter",
                "title": "Deputy Director",
                "matched_by": "address",
            }
        if addr == "cy@example.org":
            return {
                "id": DEMO_IDS[addr],
                "name": "Cy Dunn (cdunn)",
                "netid": "cdunn",
                "title": "Analyst",
                "matched_by": "address",
            }
        return None

    # -- v0.8 item context (invented data only)
    PEOPLE: ClassVar[list[dict[str, Any]]] = [
        {"netid": "bcarter", "name": "Ben Carter", "title": "Deputy Director"},
        {"netid": "cdunn", "name": "Cy Dunn", "title": "Analyst"},
        {"netid": "devans", "name": "Dee Evans", "title": "Budget Officer"},
        {"netid": "efox", "name": "Eli Fox", "title": "Storage Engineer"},
        {"netid": "ghopper", "name": "Grace Hopper", "title": "Professor"},
        {"netid": "gwu", "name": "Grace Wu", "title": "Postdoc"},
    ]

    def catalog(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "people": list(self.PEOPLE),
            "labs": [
                {"name": "Lovelace Lab (adal)"},
                {"name": "Hopper Lab (ghopper)"},
                {"name": "Research Computing"},
            ],
            "gcp": [{"project_id": "ada-lab", "name": "Ada Lab", "status": "ACTIVE"}],
            "projects": [{"name": "Campus Storage", "status": "ACTIVE"}],
            "grants": [{"c_number": "NSF-2400001", "title": "Campus storage upgrade"}],
            "assets": [{"name": "lab-share", "type": "STORAGE"}],
        }

    def person_show(self, netid: str) -> dict[str, Any] | None:
        p = next((x for x in self.PEOPLE + self.added if x["netid"] == netid), None)
        return (
            {**p, "id": DEMO_IDS.get("ben@example.org") if netid == "bcarter" else ""}
            if p
            else None
        )

    def _run(self, args: list[str], timeout: int = 0) -> Any:  # people show only
        if args[:2] == ["people", "show"]:
            return self.person_show(args[2])
        raise RuntimeError("demo ledger: read not supported")

    def dossier(self, netid: str) -> dict[str, Any] | None:
        p = self.person_show(netid)
        if not p:
            return None
        ix = {
            "bcarter": [
                {
                    "id": "11111111-aaaa-4aaa-8aaa-000000000001",
                    "date": "2026-09-14T10:00:00",
                    "summary": "Kickoff for the ada-lab cloud project; Ben is the admin contact.",
                },
                {
                    "id": "11111111-aaaa-4aaa-8aaa-000000000002",
                    "date": "2026-09-28T09:30:00",
                    "summary": "Ben asked for the ada-lab handover plan: APIs first, then owner, "
                    "then billing.",
                },
            ],
            "devans": [
                {
                    "id": "11111111-aaaa-4aaa-8aaa-000000000003",
                    "date": "2026-09-22T15:00:00",
                    "summary": "Dee approved the storage budget under NSF-2400001.",
                },
            ],
            "efox": [
                {
                    "id": "11111111-aaaa-4aaa-8aaa-000000000004",
                    "date": "2026-09-25T11:00:00",
                    "summary": "Eli sized the lab-share move at 6 TB; waiting on the ada-lab APIs.",
                },
            ],
        }.get(netid, [])
        return {
            "researcher": {
                "id": p.get("id") or "",
                "netid": netid,
                "name": p["name"],
                "title": p.get("title"),
                "dept": "Research Computing",
            },
            "labs": [{"id": DEMO_IDS["lab"], "name": "Lovelace Lab (adal)", "entity_type": "Lab"}]
            if netid in ("bcarter", "efox")
            else [],
            "grants": [],
            "projects": [],
            "assets": [],
            "interactions": ix,
            "connections": [
                {"id": DEMO_IDS["project"], "name": "ada-lab", "entity_type": "GCPProject"},
                {
                    "id": "55555555-5555-4555-8555-555555555552",
                    "name": "budget alert",
                    "entity_type": "Task",
                },
            ]
            if netid == "bcarter"
            else [],
            "external_state": [],
        }

    def tree(self, ident: str) -> dict[str, Any] | None:
        roots = {
            "ada-lab": {"id": DEMO_IDS["project"], "type": "GCPProject", "name": "ada-lab"},
            "NSF-2400001": {
                "id": "22222222-bbbb-4bbb-8bbb-000000000001",
                "type": "Grant",
                "name": "Campus storage upgrade",
            },
            "Hopper Lab (ghopper)": {
                "id": "22222222-bbbb-4bbb-8bbb-000000000002",
                "type": "Lab",
                "name": "Hopper Lab (ghopper)",
            },
        }
        r = roots.get(ident)
        if not r:
            return None
        conns = (
            [
                {
                    "id": "11111111-aaaa-4aaa-8aaa-000000000005",
                    "entity_type": "Interaction",
                    "name": "Budget alert set for ada-lab at 80 percent.",
                }
            ]
            if ident == "ada-lab"
            else []
        )
        return {"root": r, "connections": conns}

    def search_raw(self, term: str, limit: int = 25) -> list[dict[str, Any]]:
        return [
            {"id": DEMO_IDS["lab"], "name": "Lovelace Lab (adal)", "type": "Lab", "score": 0.8},
            {
                "id": "33333333-cccc-4ccc-8ccc-000000000001",
                "name": "Campus Storage",
                "type": "ResearchProject",
                "score": 0.7,
            },
        ]

    def open_tasks(self) -> dict[str, dict[str, Any]]:
        return {t["id"]: t for t in DEMO_TASKS if t.get("status") != "DONE"}

    def context(self, addr: str, name: str = "") -> dict[str, Any]:
        p = self.resolve(addr, name)
        if not p:
            return {"unresolved": True, "addr": addr}
        base = CONTEXT.get(addr, {})
        return {
            **base,
            **p,
            "labs": ["Lovelace Lab"],
            "projects": ["ada-lab"],
            "lab_refs": [{"id": DEMO_IDS["lab"], "name": "Lovelace Lab", "edge": "MEMBER_OF"}],
            "project_refs": [{"id": DEMO_IDS["project"], "name": "ada-lab", "edge": "OPERATES"}],
            "open_tasks": base.get("open_tasks", []),
            "interactions": base.get("interactions", []),
            "interaction_count": base.get("interaction_count", 0),
        }

    def search(self, term: str) -> list[dict[str, Any]]:
        t = term.lower()
        out = []
        if "ben" in t or "carter" in t:
            out.append(
                {
                    "id": DEMO_IDS["ben@example.org"],
                    "name": "Ben Carter (bcarter)",
                    "type": "Researcher",
                    "score": 0.9,
                }
            )
        if "lovelace" in t or "lab" in t:
            out.append({"id": DEMO_IDS["lab"], "name": "Lovelace Lab", "type": "Lab", "score": 0.9})
        if "ada-lab" in t or "project" in t:
            out.append(
                {"id": DEMO_IDS["project"], "name": "ada-lab", "type": "GCPProject", "score": 0.9}
            )
        return out

    def full_context(self, addr: str, name: str = "", fresh: bool = False) -> dict[str, Any]:
        p = self.resolve(addr, name)
        if not p:
            return {"unresolved": True, "addr": addr}
        long_log = (
            "Ben asked for the ada-lab handover plan. Agreed: turn on the APIs first, then "
            "add Ada as owner, then move billing. Ben will confirm the budget alert covers "
            "the new account before Friday. Storage quota for the lab share stays at 10 TB "
            "until the review in November."
        )
        ix = [
            {"id": "i1", "date": "2026-09-28", "summary": long_log, "linked": True},
            {
                "id": "i2",
                "date": "2026-09-14",
                "summary": "Kickoff for the Lovelace Lab cloud project; Ben is the admin contact.",
                "linked": True,
            },
        ]
        return {
            "id": p["id"],
            "netid": p["netid"],
            "name": p["name"],
            "title": p["title"],
            "dept": "Research Computing",
            "email": addr,
            "labs": [{"id": DEMO_IDS["lab"], "name": "Lovelace Lab", "entity_type": "Lab"}],
            "projects": [],
            "grants": [],
            "gcp_projects": [
                {"id": DEMO_IDS["project"], "name": "ada-lab", "entity_type": "GCPProject"}
            ],
            "assets": [{"id": "a1", "name": "lab-share (10 TB)", "entity_type": "Asset"}],
            "connections": [],
            "tasks": [
                {
                    "id": DEMO_TASKS[0]["id"],
                    "summary": DEMO_TASKS[0]["summary"],
                    "status": "TODO",
                    "priority": "CRITICAL",
                    "why": "linked",
                }
            ],
            "interactions": ix if addr == "ben@example.org" else [],
            "mentions": [
                {
                    "id": "m1",
                    "date": "",
                    "summary": "Weekly sync notes: Ben to review the storage plan.",
                    "linked": False,
                    "reason": "Name/Summary Match",
                }
            ]
            if addr == "ben@example.org"
            else [],
            "cloud": [{"project": "ada-lab", "scanned": "2026-09-20"}],
            "counts": {
                "linked": 2 if addr == "ben@example.org" else 0,
                "mentions": 1 if addr == "ben@example.org" else 0,
                "tasks": 1,
            },
            "matched_by": "address",
            "age": 0,
        }

    def interaction(self, iid: str) -> dict[str, Any] | None:
        return self.records.get(iid)

    def tree_fresh(self, ident: str) -> dict[str, Any]:
        r = self.records.get(ident) or {}
        return {"connections": [{"id": x["id"]} for x in r.get("links", [])]}


class DemoWriter:
    """Records ledger writes in memory. Nothing leaves the process."""

    def __init__(self, ledger: DemoLedger) -> None:
        self.ledger = ledger
        self.calls: list[tuple[str, Any]] = []
        self.n = 0

    def _id(self) -> str:
        self.n += 1
        return f"{self.n:08x}-demo-4000-8000-{self.n:012x}"

    def log(self, text: str, date: str, links: list[str]) -> dict[str, Any]:
        self.calls.append(("log", {"text": text, "date": date, "links": links}))
        iid = self._id()
        self.ledger.records[iid] = {
            "id": iid,
            "summary": text[:120],
            "links": [{"id": x} for x in links],
        }
        return {"id": iid, "explicit": links, "unresolved": [], "ai_error": False, "rc": 0}

    def task_add(self, summary: str, priority: str, due: str = "") -> dict[str, Any]:
        from ultra.ledger_write import check_due

        due = check_due(due) if due else ""
        self.calls.append(("task_add", {"summary": summary, "priority": priority, "due": due}))
        tid = self._id()
        self.ledger.records[tid] = {"id": tid, "summary": summary, "links": []}
        return {"id": tid, "rc": 0, "summary": summary, "due_date": due or None}

    def task_due(self, task_id: str, due: str) -> dict[str, Any]:
        from ultra.ledger_write import check_due, check_uuid

        tid = check_uuid(task_id, "task")
        due = check_due(due) if due else ""
        self.calls.append(("task_due", {"id": tid, "due": due}))
        return {"ok": True, "rc": 0}

    def link(self, source: str, target: str, kind: str) -> dict[str, Any]:
        self.calls.append(("link", {"source": source, "target": target, "type": kind}))
        for a, b in ((source, target), (target, source)):
            if a in self.ledger.records:
                self.ledger.records[a]["links"].append({"id": b})
        return {"ok": True, "rc": 0}

    def unlink(self, a: str, b: str) -> dict[str, Any]:
        self.calls.append(("unlink", {"a": a, "b": b}))
        return {"ok": True, "rc": 0}

    def task_status(self, task_id: str, status: str) -> dict[str, Any]:
        self.calls.append(("task_status", {"id": task_id, "status": status}))
        return {"ok": True, "rc": 0}

    def person_add(self, fields: dict[str, str], exists: Any) -> dict[str, Any]:
        from ultra.ledger_write import WriteError, check_person

        f = check_person(
            fields.get("netid", ""),
            fields.get("name", ""),
            fields.get("title", ""),
            fields.get("dept", ""),
        )
        if exists(f["netid"]):
            raise WriteError(f"{f['netid']} is already in the ledger")
        self.calls.append(("person_add", dict(f)))
        self.ledger.added.append({**f, "id": self._id()})
        after = exists(f["netid"])
        return {"ok": bool(after), "rc": 0, "record": after}


def _register_desk(api: Api) -> None:
    import tempfile
    from pathlib import Path

    from ultra.desk import Desk
    from ultra.rules import Rules
    from ultra.store import Store

    store = Store(Path(tempfile.mkdtemp(prefix="ultra-demo-desk-")) / "desk.db")
    ledger = DemoLedger()
    writer = DemoWriter(ledger)
    api.demo_writer = writer  # type: ignore[attr-defined]

    def thread_fn(key: str) -> dict[str, Any]:
        row = next((s for s in STREAM if s["key"] == key), {})
        msgs = [
            {
                **m,
                "subject": row.get("subject", ""),
                "mine": "ada@example.org" in m["from"],
                "to": m.get("to", "ada@example.org, Cy Dunn <cy@example.org>"),
                "ts": m["ts"].replace(" ", "T") + ":00-04:00" if len(m["ts"]) == 16 else m["ts"],
            }
            for m in THREADS.get(key, [])
        ]
        if not msgs and row:
            msgs = [
                {
                    "from": f"{row['from']} <{row.get('addr') or 'desk@example.org'}>",
                    "to": "ada@example.org",
                    "subject": row["subject"],
                    "ts": row["ts"],
                    "body": row["snippet"],
                    "mine": False,
                }
            ]
        return {"key": key, "messages": msgs}

    Desk(
        store,
        Rules(me={"ada@example.org"}),
        ledger,
        writer,
        thread_fn,
        "America/New_York",
        me_netid="adal",
    ).register(api)

    from ultra.itemctx import ItemContext
    from ultra.itemdesk import ItemDesk

    def task_fn(tid: str) -> dict[str, Any] | None:
        t = next((x for x in DEMO_TASKS if x["id"] == tid), None)
        return {"task": t, "links": []} if t else None

    rules = Rules(me={"ada@example.org"})
    ictx = ItemContext(ledger, store, rules, {"adal"})
    api.demo_ictx = ictx  # type: ignore[attr-defined]
    ItemDesk(ictx, thread_fn, task_fn, writer, DemoAI(), store, "Ada").register(api)
    _register_studio(api, store)


class DemoStudioAI:
    """Draft Studio in demo mode: canned brief / draft / check JSON, invented people."""

    enabled = True
    model = "demo"

    def _gen(self, prompt: str, system: str, max_tokens: int = 0, **kw: Any) -> Any:
        import json

        from ultra.ai import Result

        if '"picks"' in system:
            out: dict[str, Any] = {"picks": [{"n": 1, "why": "same kind of handover question"}]}
        elif '"asks"' in system:
            out = {
                "asks": ["When will the account be ready for my students?"],
                "constraints": ["The ada-lab APIs must be on before the data move."],
                "audience": "faculty",
                "audience_note": "Short and direct, as in your past replies to Ada.",
                "known": [
                    {"fact": "Ben says the project is ready.", "source": "M1"},
                    {"fact": "Eli moves the lab-share data once the APIs are on.", "source": "M1"},
                ],
                "unknown": [
                    {
                        "question": "Are the ada-lab APIs turned on yet?",
                        "why": "The account date depends on it.",
                        "options": ["Yes, on", "Not yet", "Today"],
                    }
                ],
                "need_from_sender": ["NetIDs for the students who need access."],
                "risks": ["Dee's budget sign-off covers storage only."],
                "plan": ["Say what happens first (APIs, then the move) and ask for NetIDs."],
                "precedent_shape": "Short answer first, then what I need from you.",
            }
        elif '"claims"' in system and "verdict" in system:
            out = {
                "claims": [
                    {
                        "text": "Ben has the project ready on his side.",
                        "verdict": "supported",
                        "sources": ["M1"],
                        "note": "Ben says so in message 1.",
                    },
                    {
                        "text": "It usually takes about a day.",
                        "verdict": "unclear",
                        "sources": ["P1"],
                        "note": "Only in a past reply; may be stale.",
                    },
                ]
            }
        else:
            out = {
                "body": "Hi Ada,\n\nBen has the project ready on his side. Once the ada-lab "
                "APIs are on, Eli will move the lab-share data, and then your students can "
                "log in. It usually takes about a day.\n\nCould you send me the NetIDs for "
                "the students who need access?\n\nAda",
                "claims": [
                    {"text": "Ben has the project ready on his side.", "sources": ["M1"]},
                    {"text": "It usually takes about a day.", "sources": ["P1"]},
                ],
            }
        return Result(json.dumps(out), "demo", 0, 0.0)


def _register_studio(api: Api, store: Any) -> None:
    from ultra.sources import HouseFacts, Sources
    from ultra.studio import Studio

    def thread_fn(key: str) -> dict[str, Any]:
        msgs = [
            {"id": f"m{i}", "to": "Ada Lovelace <ada@example.org>", "cc": "", "mine": False, **m}
            for i, m in enumerate(THREADS.get(key, []), 1)
        ]
        return {"messages": msgs}

    def search_fn(q: str, limit: int) -> list[dict[str, Any]]:
        if "in:sent" not in q:
            return []
        return [
            {
                "id": "p1",
                "thread_id": "old1",
                "from": "Ada Lovelace <ada@example.org>",
                "mine": True,
                "to": "Cy Dunn <cy@example.org>",
                "cc": "",
                "subject": "Re: account for the Dunn lab students",
                "date": "2026-09-10T15:00:00+00:00",
                "body": "Hi Cy,\n\nShort answer: the lab project is ready and the students "
                "can log in once the APIs are on. It usually takes about a day after that. "
                "Could you send me their NetIDs, and I will add them to the project and the "
                "storage share? The budget alerts go to you and to me.\n\nAda",
            }
        ]

    facts = HouseFacts(store)
    if not facts.all():
        facts.add(
            "Student accounts are added to the PI's lab project, not made separately.",
            "account students netid access",
            "demo",
        )
    Studio(
        store,
        DemoStudioAI(),
        thread_fn,
        search_fn,
        lambda key, msgs: (
            "Ben Carter (bcarter): admin contact for ada-lab. Open task: hand over ada-lab to Ada."
        ),
        Sources([], store),
        facts,
        {"ada@example.org"},
        "Ada",
        signature=lambda: "Ada",
    ).register(api)


DEMO_SLACK_CH = "C0DEMO0001"


class DemoOutbox:
    """Stands in for Gmail send in demo mode and tests: records, never sends."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def __call__(self, d: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(
            {
                "draft": d["id"],
                "kind": d["kind"],
                **{k: v[k] for k in ("to_addrs", "subject", "body")},
            }
        )
        if d["kind"] == "slack":  # the demo "posts" and verifies trivially
            ts = f"1790000{len(self.sent):03d}.000100"
            return {"id": ts, "ts": ts, "permalink": "", "verified": True, "verify_note": ""}
        return {"id": f"demo-{len(self.sent)}", "threadId": d.get("thread_id")}


def _register_composer(api: Api) -> None:
    """Demo composer: real state machine on a temp store, fake outbox, demo threads."""
    import tempfile
    from pathlib import Path

    from ultra.compose import ComposeError, Composer
    from ultra.config import Config
    from ultra.lint import ascii_fix
    from ultra.server import ApiError
    from ultra.store import Store

    tmp = Path(tempfile.mkdtemp(prefix="ultra-demo-"))
    store = Store(tmp / "demo.db")
    cfg = Config({"mail": {"send_delay_seconds": 3}})
    me = {"ada@example.org"}
    comp = Composer(cfg, store, me)
    outbox = DemoOutbox()
    api.demo_outbox = outbox  # type: ignore[attr-defined]

    def wrap(fn: Any, *a: Any) -> Any:
        try:
            return fn(*a)
        except ComposeError as e:
            raise ApiError(e.status, str(e)) from e

    def demo_thread(key: str) -> dict[str, Any]:
        msgs = THREADS.get(key, [])
        return {
            "key": key,
            "messages": [
                {
                    **m,
                    "id": f"{key}-{i}",
                    "message_id": f"<{key}-{i}@example.org>",
                    "references": "",
                    "subject": next((s["subject"] for s in STREAM if s["key"] == key), ""),
                    "to": m.get("to", "ada@example.org"),
                }
                for i, m in enumerate(msgs)
            ],
        }

    def new(q: dict, body: Any, m: re.Match[str]) -> dict:
        body = body or {}
        kind = body.get("kind", "reply")
        th = None if kind == "new" else demo_thread(str(body.get("thread", "")))
        if th is not None:
            th["key"] = "g-" + th["key"].removeprefix("g-")
        return wrap(comp.create, kind, th, "ada@example.org")

    did = lambda m: int(m.group(1))  # noqa: E731
    api.add("POST", r"/api/drafts", new)
    api.add(
        "GET", r"/api/drafts/(\d+)", lambda q, b, m: {**wrap(comp.get, did(m)), "send_error": ""}
    )
    api.add(
        "GET",
        r"/api/drafts/thread/([A-Za-z0-9_-]+)",
        lambda q, b, m: {"drafts": comp.for_thread(m.group(1))},
    )
    api.add(
        "POST",
        r"/api/drafts/(\d+)/versions",
        lambda q, b, m: wrap(comp.save, did(m), b or {}, "me", "edit"),
    )
    api.add(
        "POST",
        r"/api/drafts/(\d+)/fix-ascii",
        lambda q, b, m: wrap(
            comp.save,
            did(m),
            {
                k: ascii_fix((comp.get(did(m))["current"] or {}).get(k) or "")
                for k in ("subject", "body")
            },
            "me",
            "fix ASCII",
        ),
    )
    api.add(
        "POST",
        r"/api/drafts/(\d+)/restore",
        lambda q, b, m: wrap(comp.restore, did(m), int((b or {})["version"])),
    )
    api.add(
        "POST",
        r"/api/drafts/(\d+)/studio",
        lambda q, b, m: wrap(
            comp.save,
            did(m),
            {"body": ascii_fix(str((b or {}).get("body", "")))},
            "ai",
            str((b or {}).get("label", "Draft Studio"))[:200],
        ),
    )
    api.add("POST", r"/api/drafts/(\d+)/approve", lambda q, b, m: wrap(comp.approve, did(m)))
    api.add("POST", r"/api/drafts/(\d+)/unapprove", lambda q, b, m: wrap(comp.unapprove, did(m)))
    api.add("POST", r"/api/drafts/(\d+)/review", lambda q, b, m: wrap(comp.review, did(m)))
    api.add("POST", r"/api/drafts/(\d+)/discard", lambda q, b, m: wrap(comp.discard, did(m)))

    def ai(q: dict, b: Any, m: re.Match[str]) -> dict:
        text = "Hi,\n\nThanks for the note. (Demo mode: this is canned text, not AI.)\n\nAda"
        return {
            **wrap(comp.save, did(m), {"body": text}, "ai", "demo draft"),
            "ai": {"model": "demo", "tokens": 0, "seconds": 0},
        }

    api.add("POST", r"/api/drafts/(\d+)/ai", ai)
    api.add("POST", r"/api/drafts/(\d+)/gmail", lambda q, b, m: {"gmail_draft_id": None})

    def send(q: dict, b: Any, m: re.Match[str]) -> dict:
        b = b or {}
        try:
            d, t, v = int(b["draft"]), str(b["token"]), int(b["version"])
        except (KeyError, ValueError, TypeError) as e:
            raise ApiError(400, "draft, token and version are required") from e
        return wrap(comp.confirm, d, t, v, outbox)

    api.add("POST", r"/api/send", send)
    api.add("POST", r"/api/send/(\d+)/cancel", lambda q, b, m: wrap(comp.cancel, did(m)))

    # Slack replies: the demo Slack row is a group DM in channel DEMO_SLACK_CH
    def slack_target(key: str) -> dict[str, str] | None:
        row = next((s for s in STREAM if s["key"] == key and s["source"] == "slack"), None)
        return (
            {"channel_id": DEMO_SLACK_CH, "thread_ts": "", "label": row["subject"]} if row else None
        )

    def slack_draft(q: dict, b: Any, m: re.Match[str]) -> dict:
        tgt = slack_target(str((b or {}).get("key", "")))
        if not tgt:
            raise ApiError(404, "That Slack conversation is not in the stream")
        ex = comp.for_slack(tgt["channel_id"], tgt["thread_ts"])
        return ex[0] if ex else wrap(comp.create_slack, tgt["channel_id"], "", tgt["label"])

    def slack_find(q: dict, b: Any, m: re.Match[str]) -> dict:
        tgt = slack_target(m.group(1))
        ex = comp.for_slack(tgt["channel_id"], "") if tgt else []
        return {"draft": ex[0]["id"] if ex else None}

    api.add("POST", r"/api/slack/draft", slack_draft)
    api.add("GET", r"/api/slack/draft/(s-[A-Za-z0-9_-]+)", slack_find)
    api.add("POST", r"/api/mail/archive", lambda q, b, m: {"archived": [], "demo": True})
    api.add("POST", r"/api/mail/unarchive", lambda q, b, m: {"unarchived": [], "demo": True})
    api.add(
        "POST",
        r"/api/ai/summary",
        lambda q, b, m: {
            "text": "Demo: Ben asks for the handover plan by Friday.",
            "model": "demo",
            "tokens": 0,
            "seconds": 0,
        },
    )
