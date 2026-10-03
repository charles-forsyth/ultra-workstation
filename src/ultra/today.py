"""Calendar routes: Today and Week views, focus blocks (single and weekly), slot
finder, meeting prep, RSVP and meeting invitations (SPEC 7.4, 7.7).

Shared by live and demo. Block creation is one click (it only touches the operator's
own calendar, carries the ultra tag, and can be moved or deleted from the same view);
events Ultra did not create are read-only on the server side, except the operator's
own RSVP (one confirmation, server token). Invitations email other people, so they go
through two approvals (invites.py).
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from ultra import google_auth
from ultra.calendar import COLORS, WEEKDAYS, CalendarError
from ultra.invites import InviteError, Invites, Rsvp

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
        store: Any = None,
        org_domain: str = "",
        me: set[str] | None = None,
    ) -> None:
        self.cal = cal
        self.item_text = item_text  # key -> {"subject", "summary", "draft", "task_id", "url"}
        self.rules = rules
        st = store if store is not None else getattr(cal, "store", None)
        self.invites = Invites(st, cal, org_domain, me) if st is not None else None
        self.rsvp = Rsvp(st, cal) if st is not None else None

    def register(self, api: Any) -> None:
        api.add("GET", r"/api/cal/day", self.r_day)
        api.add("GET", r"/api/cal/event", self.r_event)
        api.add("POST", r"/api/cal/block", self.r_block)
        api.add("POST", r"/api/cal/block/move", self.r_move)
        api.add("POST", r"/api/cal/block/delete", self.r_delete)
        api.add("POST", r"/api/cal/slots", self.r_slots)
        api.add("POST", r"/api/cal/block-text", self.r_block_text)
        api.add("GET", r"/api/cal/week", self.r_week)
        api.add("POST", r"/api/cal/series", self.r_series)
        api.add("POST", r"/api/cal/series/delete", self.r_series_delete)
        api.add("POST", r"/api/cal/rsvp/ask", self.r_rsvp_ask)
        api.add("POST", r"/api/cal/rsvp", self.r_rsvp)
        api.add("POST", r"/api/invites", self.r_inv_new)
        api.add("GET", r"/api/invites/(\d+)", self.r_inv_get)
        api.add("POST", r"/api/invites/(\d+)", self.r_inv_update)
        api.add("POST", r"/api/invites/(\d+)/approve", self.r_inv_approve)
        api.add("POST", r"/api/invites/(\d+)/unapprove", self.r_inv_unapprove)
        api.add("POST", r"/api/invites/(\d+)/review", self.r_inv_review)
        api.add("POST", r"/api/invites/(\d+)/send", self.r_inv_send)
        api.add("POST", r"/api/invites/(\d+)/discard", self.r_inv_discard)

    # ------------------------------------------------------------ reads
    def r_day(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        day = (q.get("day") or [""])[0] or dt.datetime.now(self.cal.tz).date().isoformat()
        if not DAY_RE.fullmatch(day):
            raise _bad("day must be YYYY-MM-DD")
        fresh = (q.get("fresh") or ["0"])[0] == "1"
        try:
            return self.cal.day(day, fresh=fresh)
        except (CalendarError, google_auth.AuthNeeded) as e:
            # the page still draws the day (and the Home line) around the message
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

    def r_week(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        day = (q.get("day") or [""])[0] or dt.datetime.now(self.cal.tz).date().isoformat()
        if not DAY_RE.fullmatch(day):
            raise _bad("day must be YYYY-MM-DD")
        fresh = (q.get("fresh") or ["0"])[0] == "1"
        try:
            return self.cal.week(day, fresh=fresh)
        except (CalendarError, google_auth.AuthNeeded) as e:
            return {"week_start": day, "days": {}, "pending": [], "error": str(e)}

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
            res = self.cal.create_block(
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
        self._journal("cal_block", res, {"summary": str(b.get("summary", ""))[:80]})
        return res

    def r_move(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        cal, eid = str(b.get("cal", "primary")), str(b.get("id", ""))
        if not CAL_RE.fullmatch(cal) or not EID_RE.fullmatch(eid):
            raise _bad("bad event id")
        start, end = self._times(b)
        try:
            res = self.cal.move_block(
                cal, eid, start, end, summary=b.get("summary") if "summary" in b else None
            )
        except CalendarError as e:
            raise _bad(str(e), 409) from e
        self._journal("cal_move", res, {"id": eid})
        return res

    def r_delete(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        cal, eid = str(b.get("cal", "primary")), str(b.get("id", ""))
        if not CAL_RE.fullmatch(cal) or not EID_RE.fullmatch(eid):
            raise _bad("bad event id")
        try:
            res = self.cal.delete_block(cal, eid)
        except CalendarError as e:
            raise _bad(str(e), 409) from e
        self._journal("cal_delete", res, {"id": eid})
        return res

    def r_series(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """A weekly series of focus blocks (Ultra-tagged, no attendees, ends after at
        most 26 weeks)."""
        b = body or {}
        start, end = self._times(b)
        days = [str(x).upper()[:2] for x in (b.get("weekdays") or [])]
        if any(d not in WEEKDAYS for d in days):
            raise _bad("weekdays must be MO..SU")
        color = str(b.get("color") or "")
        if color and color.lower() not in COLORS:
            raise _bad("unknown colour")
        try:
            res = self.cal.create_recurring_block(
                str(b.get("summary", "")),
                start,
                end,
                days,
                int(b.get("weeks") or 4),
                description=str(b.get("description", "")),
                personal=bool(b.get("personal")),
                color=color,
            )
        except (CalendarError, ValueError) as e:
            raise _bad(str(e)) from e
        self._journal("cal_series", res, {"summary": str(b.get("summary", ""))[:80]})
        return res

    def r_series_delete(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        cal, eid = str(b.get("cal", "primary")), str(b.get("id", ""))
        if not CAL_RE.fullmatch(cal) or not EID_RE.fullmatch(eid):
            raise _bad("bad event id")
        try:
            res = self.cal.delete_series(cal, eid)
        except CalendarError as e:
            raise _bad(str(e), 409) from e
        self._journal("cal_series_delete", res, {"id": eid})
        return res

    # ------------------------------------------------------------ RSVP
    def _need(self, x: Any) -> Any:
        if x is None:
            raise _bad("not available")
        return x

    def _ev_ids(self, b: dict[str, Any]) -> tuple[str, str]:
        cal, eid = str(b.get("cal", "primary")), str(b.get("id", ""))
        if not CAL_RE.fullmatch(cal) or not EID_RE.fullmatch(eid):
            raise _bad("bad event id")
        return cal, eid

    def r_rsvp_ask(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        cal, eid = self._ev_ids(b)
        try:
            return self._need(self.rsvp).ask(cal, eid, str(b.get("response", "")))
        except InviteError as e:
            raise _bad(str(e), e.status) from e

    def r_rsvp(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        cal, eid = self._ev_ids(b)
        try:
            return self._need(self.rsvp).confirm(
                str(b.get("token", "")),
                cal,
                eid,
                str(b.get("response", "")),
                str(b.get("note", "")),
            )
        except InviteError as e:
            raise _bad(str(e), e.status) from e

    # ------------------------------------------------------------ invites (two approvals)
    def _inv(self, fn: Callable[..., Any], *a: Any) -> Any:
        try:
            return fn(*a)
        except InviteError as e:
            raise _bad(str(e), e.status) from e

    def r_inv_new(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        return self._inv(self._need(self.invites).create, b, str(b.get("source", ""))[:200])

    def r_inv_get(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._inv(self._need(self.invites).get, int(m.group(1)))

    def r_inv_update(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._inv(self._need(self.invites).update, int(m.group(1)), body or {})

    def r_inv_approve(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._inv(self._need(self.invites).approve, int(m.group(1)))

    def r_inv_unapprove(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._inv(self._need(self.invites).unapprove, int(m.group(1)))

    def r_inv_review(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._inv(self._need(self.invites).review, int(m.group(1)))

    def r_inv_send(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._inv(
            self._need(self.invites).confirm, int(m.group(1)), str((body or {}).get("token", ""))
        )

    def r_inv_discard(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._inv(self._need(self.invites).discard, int(m.group(1)))

    def _journal(self, action: str, res: Any, detail: dict[str, Any]) -> None:
        store = getattr(self.cal, "store", None)
        if store is not None and hasattr(store, "journal"):
            ok = bool((res or {}).get("ok", True)) if isinstance(res, dict) else True
            ev = (res or {}).get("event") or {} if isinstance(res, dict) else {}
            store.journal(action, str(ev.get("id") or detail.get("id") or ""), ok, detail)

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
