"""v0.6 routes: Today view, focus blocks, slot finder, meeting prep (SPEC 7.4).

Shared by live and demo. Block creation is one click (it only touches the operator's
own calendar, carries the ultra tag, and can be moved or deleted from the same view);
events Ultra did not create are read-only on the server side.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from ultra.calendar import COLORS, CalendarError

DAY_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
EID_RE = re.compile(r"[A-Za-z0-9_-]{5,1024}")
CAL_RE = re.compile(r"[A-Za-z0-9_.@#-]{1,200}")
EMAIL_RE = re.compile(r"[^@\s<>,;]+@[^@\s<>,;]+\.[A-Za-z]{2,}")


def _bad(msg: str, status: int = 400) -> Exception:
    from ultra.server import ApiError

    return ApiError(status, msg)


class Today:
    def __init__(
        self,
        cal: Any,  # ultra.calendar.Calendar or a demo double with the same methods
        item_text: Callable[[str], dict[str, Any]],
        rules: Any,
    ) -> None:
        self.cal = cal
        self.item_text = item_text  # key -> {"subject", "summary", "draft", "task_id", "url"}
        self.rules = rules

    def register(self, api: Any) -> None:
        api.add("GET", r"/api/cal/day", self.r_day)
        api.add("GET", r"/api/cal/event", self.r_event)
        api.add("POST", r"/api/cal/block", self.r_block)
        api.add("POST", r"/api/cal/block/move", self.r_move)
        api.add("POST", r"/api/cal/block/delete", self.r_delete)
        api.add("POST", r"/api/cal/slots", self.r_slots)
        api.add("POST", r"/api/cal/block-text", self.r_block_text)

    # ------------------------------------------------------------ reads
    def r_day(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        day = (q.get("day") or [""])[0] or dt.datetime.now(self.cal.tz).date().isoformat()
        if not DAY_RE.fullmatch(day):
            raise _bad("day must be YYYY-MM-DD")
        fresh = (q.get("fresh") or ["0"])[0] == "1"
        try:
            return self.cal.day(day, fresh=fresh)
        except CalendarError as e:
            return {"day": day, "events": [], "error": str(e)}

    def r_event(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        cal = (q.get("cal") or ["primary"])[0]
        eid = (q.get("id") or [""])[0]
        if not CAL_RE.fullmatch(cal) or not EID_RE.fullmatch(eid):
            raise _bad("bad event id")
        e = self.cal.event(cal, eid)
        # attendees to resolve for prep: people only, not rooms, not me, max 12
        people = [a for a in e.get("attendees", []) if not a["self"] and not a["resource"]]
        e["prep_people"] = [
            {"addr": a["email"].lower(), "name": a["name"], "response": a["response"]}
            for a in people
            if a["email"] and not self.rules.is_me(a["email"])
        ][:12]
        return e

    # ------------------------------------------------------------ writes
    def _times(self, b: dict[str, Any]) -> tuple[dt.datetime, dt.datetime]:
        try:
            start = self.cal.parse_local(str(b.get("start", "")))
            if b.get("end"):
                end = self.cal.parse_local(str(b["end"]))
            else:
                end = start + dt.timedelta(minutes=int(b.get("minutes") or 30))
        except (CalendarError, ValueError, TypeError) as e:
            raise _bad(str(e)) from e
        return start, end

    def r_block_text(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """Default title and description for a block from a stream item (no write)."""
        key = str((body or {}).get("key", ""))
        if not re.fullmatch(r"[gkst]-[A-Za-z0-9_.:-]{1,80}", key):
            raise _bad("bad item key")
        info = self.item_text(key)
        subj = (info.get("subject") or "follow-up").strip()
        lines = []
        if info.get("summary"):
            lines += ["Context:", info["summary"].strip(), ""]
        if info.get("draft"):
            lines += ["Draft reply (not sent):", info["draft"].strip(), ""]
        if info.get("task_id"):
            lines.append(f"Ledger task: {info['task_id']}")
        if info.get("url"):
            lines.append(info["url"])
        lines.append(f"Ultra item: {key}")
        return {
            "summary": f"Focus: {subj}"[:200],
            "description": "\n".join(lines)[:7000],
            "personal": False,
        }

    def r_block(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        start, end = self._times(b)
        color = str(b.get("color") or "")
        if color and color.lower() not in COLORS:
            raise _bad("unknown colour")
        try:
            return self.cal.create_block(
                str(b.get("summary", "")),
                start,
                end,
                description=str(b.get("description", "")),
                personal=bool(b.get("personal")),
                color=color,
                source=str(b.get("key", ""))[:200],
            )
        except CalendarError as e:
            raise _bad(str(e)) from e

    def r_move(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        cal, eid = str(b.get("cal", "primary")), str(b.get("id", ""))
        if not CAL_RE.fullmatch(cal) or not EID_RE.fullmatch(eid):
            raise _bad("bad event id")
        start, end = self._times(b)
        try:
            return self.cal.move_block(
                cal, eid, start, end, summary=b.get("summary") if "summary" in b else None
            )
        except CalendarError as e:
            raise _bad(str(e), 409) from e

    def r_delete(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        cal, eid = str(b.get("cal", "primary")), str(b.get("id", ""))
        if not CAL_RE.fullmatch(cal) or not EID_RE.fullmatch(eid):
            raise _bad("bad event id")
        try:
            return self.cal.delete_block(cal, eid)
        except CalendarError as e:
            raise _bad(str(e), 409) from e

    def r_slots(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        emails = EMAIL_RE.findall(" ".join(str(x) for x in (b.get("emails") or [])))
        days = max(1, min(int(b.get("days") or 5), 14))
        mins = max(15, min(int(b.get("minutes") or 30), 240))
        start = dt.datetime.now(self.cal.tz)
        start = start.replace(minute=(start.minute // 15) * 15, second=0, microsecond=0)
        start += dt.timedelta(minutes=15)
        end = dt.datetime.combine(start.date() + dt.timedelta(days=days), dt.time(0), self.cal.tz)
        try:
            return self.cal.freebusy(emails, start, end, mins)
        except CalendarError as e:
            raise _bad(str(e)) from e
