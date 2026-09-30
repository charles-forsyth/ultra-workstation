"""Calendar adapter (SPEC 7.4, 7.7, 8.2): Today and Week views, focus blocks,
free/busy, meeting prep, invitations and replies.

Reads the operator's calendars. Writes are narrow:
- Create a block: summary, start, end, description, color, visibility, no attendees.
  Every block Ultra creates carries the private extended property `ultra=1`.
- Move, resize or delete only events that carry `ultra=1` and are organised by the
  operator. Other events (meetings, invites, anything made elsewhere) are read-only,
  enforced here on the server, not only in the page.
- Recurring focus blocks: a weekly series of blocks, also tagged `ultra=1`.
- Meetings with invitees (v0.10): created only through the two-approval invite flow
  in ``invites.py``; they carry `ultra=1` and `ultra_kind=meeting`. Guests are emailed
  by Google when the invite is created (sendUpdates=all).
- RSVP: the operator's own response on an invitation (accepted, tentative, declined),
  after a confirmation; nothing else on someone else's event changes.
- After every write, re-read the event and check what was asked.

Times cross the API as RFC3339 with offsets; the page works in the operator's
configured timezone.
"""

from __future__ import annotations

import datetime as dt
import html
import re
import threading
from typing import Any
from zoneinfo import ZoneInfo

from ultra import google_auth
from ultra.config import Config
from ultra.store import Store

TAG_KEY = "ultra"
TAG_VAL = "1"
COLORS = {  # Google Calendar event colour ids
    "lavender": "1",
    "sage": "2",
    "grape": "3",
    "flamingo": "4",
    "banana": "5",
    "tangerine": "6",
    "peacock": "7",
    "graphite": "8",
    "blueberry": "9",
    "basil": "10",
    "tomato": "11",
}
MAX_BLOCK_MIN = 8 * 60
MAX_MEETING_MIN = 8 * 60
MAX_GUESTS = 40
MAX_REPEAT_WEEKS = 26
RESPONSES = ("accepted", "tentative", "declined")
WEEKDAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")
MAX_DESC = 7800
CACHE_TTL = 120


class CalendarError(Exception):
    pass


def _strip_html(s: str) -> str:
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", s or "")
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(s)).strip()


def _meet_link(e: dict[str, Any]) -> str:
    if e.get("hangoutLink"):
        return str(e["hangoutLink"])
    for ep in (e.get("conferenceData") or {}).get("entryPoints") or []:
        if ep.get("entryPointType") == "video" and ep.get("uri"):
            return str(ep["uri"])
    m = re.search(
        r"https://[\w.-]*(zoom\.us|teams\.microsoft\.com|meet\.google\.com)/[^\s\"<>]+",
        (e.get("location") or "") + " " + (e.get("description") or ""),
    )
    return m.group(0) if m else ""


def has_ultra_tag(e: dict[str, Any]) -> bool:
    priv = ((e.get("extendedProperties") or {}).get("private")) or {}
    return priv.get(TAG_KEY) == TAG_VAL and bool((e.get("organizer") or {}).get("self", True))


def is_ultra_block(e: dict[str, Any]) -> bool:
    """A block Ultra may move or delete: tagged, organised by the operator, and with
    nobody else on it. A meeting Ultra sent (it has guests) is NOT a block: changing it
    would need to notify people, so it is read-only here like any other meeting."""
    priv = ((e.get("extendedProperties") or {}).get("private")) or {}
    others = [a for a in e.get("attendees") or [] if not a.get("self") and not a.get("resource")]
    return has_ultra_tag(e) and priv.get("ultra_kind") != "meeting" and not others


def slim_event(e: dict[str, Any], cal_id: str) -> dict[str, Any]:
    """What the page needs; attendee emails kept for meeting prep."""
    start = e.get("start") or {}
    end = e.get("end") or {}
    me = next((a for a in e.get("attendees") or [] if a.get("self")), None)
    return {
        "id": e.get("id", ""),
        "cal": cal_id,
        "summary": e.get("summary") or "(no title)",
        "start": start.get("dateTime") or start.get("date") or "",
        "end": end.get("dateTime") or end.get("date") or "",
        "all_day": "date" in start and "dateTime" not in start,
        "visibility": e.get("visibility") or "default",
        "color": e.get("colorId") or "",
        "status": e.get("status") or "",
        "type": e.get("eventType") or "default",
        "organizer_self": bool((e.get("organizer") or {}).get("self")),
        "organizer": (e.get("organizer") or {}).get("email", ""),
        "my_response": (me or {}).get("responseStatus", ""),
        "attendees": [
            {
                "email": a.get("email", ""),
                "name": a.get("displayName", ""),
                "response": a.get("responseStatus", ""),
                "self": bool(a.get("self")),
                "organizer": bool(a.get("organizer")),
                "resource": bool(a.get("resource")),
            }
            for a in (e.get("attendees") or [])[:200]
        ],
        "attendee_count": len(e.get("attendees") or []),
        "link": _meet_link(e),
        "html_link": e.get("htmlLink", ""),
        "location": e.get("location") or "",
        "has_description": bool(e.get("description")),
        "ultra": is_ultra_block(e),
        "ultra_kind": (((e.get("extendedProperties") or {}).get("private")) or {}).get(
            "ultra_kind", ""
        ),
        "recurring": bool(e.get("recurringEventId") or e.get("recurrence")),
    }


def _days_touched(e: dict[str, Any], tz: ZoneInfo) -> list[str]:
    """Local dates an event covers (all-day end dates are exclusive)."""
    s, en = e.get("start") or "", e.get("end") or ""
    try:
        if e.get("all_day"):
            d0, d1 = dt.date.fromisoformat(s[:10]), dt.date.fromisoformat(en[:10])
            d1 = max(d1, d0 + dt.timedelta(days=1))
        else:
            a = dt.datetime.fromisoformat(s).astimezone(tz)
            b = dt.datetime.fromisoformat(en).astimezone(tz)
            d0 = a.date()
            # an event ending exactly at midnight does not touch the next day
            d1 = (b - dt.timedelta(microseconds=1)).date() + dt.timedelta(days=1)
            d1 = max(d1, d0 + dt.timedelta(days=1))
    except ValueError:
        return []
    out: list[str] = []
    while d0 < d1 and len(out) < 60:
        out.append(d0.isoformat())
        d0 += dt.timedelta(days=1)
    return out


def free_windows(
    busy: list[tuple[dt.datetime, dt.datetime]],
    start: dt.datetime,
    end: dt.datetime,
    min_minutes: int,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Gaps of at least min_minutes between merged busy intervals inside [start, end]."""
    spans = sorted((max(s, start), min(e, end)) for s, e in busy if e > start and s < end)
    merged: list[list[dt.datetime]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    out, cur = [], start
    for s, e in merged:
        if (s - cur).total_seconds() >= min_minutes * 60:
            out.append((cur, s))
        cur = max(cur, e)
    if (end - cur).total_seconds() >= min_minutes * 60:
        out.append((cur, end))
    return out


class Calendar:
    def __init__(self, cfg: Config, store: Store) -> None:
        self.cfg = cfg
        self.store = store
        self.tz = ZoneInfo(cfg.timezone)
        self.calendars = list(cfg.get("google", "calendars", ["primary"]) or ["primary"])
        self.work_color = str(cfg.get("google", "work_block_color", "tomato"))
        self.personal_visibility = str(cfg.get("google", "personal_block_visibility", "private"))
        self.work_hours = (
            str(cfg.get("calendar", "day_start", "08:00")),
            str(cfg.get("calendar", "day_end", "17:00")),
        )
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"ok": None, "error": ""}

    # ------------------------------------------------------------ helpers
    def _svc(self) -> Any:
        return google_auth.service(self.cfg, "calendar", "v3", "calendar")

    def _day_bounds(self, day: str) -> tuple[dt.datetime, dt.datetime]:
        try:
            d = dt.date.fromisoformat(day)
        except ValueError as e:
            raise CalendarError("day must be YYYY-MM-DD") from e
        start = dt.datetime.combine(d, dt.time(0, 0), self.tz)
        return start, start + dt.timedelta(days=1)

    def parse_local(self, s: str) -> dt.datetime:
        """ISO datetime; a naive value is read in the operator's timezone."""
        try:
            t = dt.datetime.fromisoformat(s)
        except (TypeError, ValueError) as e:
            raise CalendarError(f"bad time: {s!r}") from e
        return t.replace(tzinfo=self.tz) if t.tzinfo is None else t.astimezone(self.tz)

    # ------------------------------------------------------------ reads
    def day(self, day: str, fresh: bool = False) -> dict[str, Any]:
        start, end = self._day_bounds(day)
        key = f"cal:day:{day}"
        if not fresh:
            hit = self.store.cache_get(key, CACHE_TTL)
            if hit:
                return {**hit[0], "age": hit[1]}
        events = self._list(start, end)
        events.sort(key=lambda x: (not x["all_day"], x["start"]))
        out = {
            "day": day,
            "tz": self.cfg.timezone,
            "events": events,
            "work_hours": list(self.work_hours),
        }
        self.store.cache_put(key, out)
        return {**out, "age": 0.0}

    def week(self, day: str, fresh: bool = False) -> dict[str, Any]:
        """Monday-to-Sunday week containing ``day``: one read, events grouped by day.
        An event spanning days appears on each day it touches."""
        try:
            d = dt.date.fromisoformat(day)
        except ValueError as err:
            raise CalendarError("day must be YYYY-MM-DD") from err
        monday = d - dt.timedelta(days=d.weekday())
        key = f"cal:week:{monday.isoformat()}"
        if not fresh:
            hit = self.store.cache_get(key, CACHE_TTL)
            if hit:
                return {**hit[0], "pending": self._pending(hit[0]), "age": hit[1]}
        start = dt.datetime.combine(monday, dt.time(0), self.tz)
        end = start + dt.timedelta(days=7)
        events = self._list(start, end)
        days: dict[str, list[dict[str, Any]]] = {
            (monday + dt.timedelta(days=i)).isoformat(): [] for i in range(7)
        }
        for ev in events:
            for dd in _days_touched(ev, self.tz):
                if dd in days:
                    days[dd].append(ev)
        for v in days.values():
            v.sort(key=lambda x: (not x["all_day"], x["start"]))
        out = {
            "week_start": monday.isoformat(),
            "tz": self.cfg.timezone,
            "days": days,
            "work_hours": list(self.work_hours),
        }  # "pending" is computed on every call (not cached) so ended ones drop out
        self.store.cache_put(key, out)
        return {**out, "pending": self._pending(out), "age": 0.0}

    def _pending(self, week: dict[str, Any]) -> list[dict[str, Any]]:
        """Invitations still waiting for the operator's answer that have not ended yet,
        soonest first, each once (multi-day events appear on several days)."""
        now = dt.datetime.now(self.tz)
        seen: dict[str, dict[str, Any]] = {}
        for evs in week.get("days", {}).values():
            for e in evs:
                if e["my_response"] != "needsAction" or e["organizer_self"] or e["all_day"]:
                    continue
                try:
                    end = dt.datetime.fromisoformat(e["end"])
                except ValueError:
                    continue
                if end > now:
                    seen.setdefault(e["id"], e)
        return sorted(seen.values(), key=lambda x: x["start"])

    def _list(self, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        try:
            svc = self._svc()
            for cal in self.calendars:
                page = None
                while True:
                    r = (
                        svc.events()
                        .list(
                            calendarId=cal,
                            timeMin=start.isoformat(),
                            timeMax=end.isoformat(),
                            singleEvents=True,
                            orderBy="startTime",
                            maxResults=250,
                            pageToken=page,
                        )
                        .execute()
                    )
                    events += [
                        slim_event(e, cal)
                        for e in r.get("items", [])
                        if e.get("status") != "cancelled"
                    ]
                    page = r.get("nextPageToken")
                    if not page:
                        break
            self.state = {"ok": True, "error": ""}
        except google_auth.AuthNeeded:
            raise
        except Exception as e:
            self.state = {"ok": False, "error": str(e)[:300]}
            raise CalendarError(f"Calendar read failed: {e}") from e
        return events

    def event(self, cal: str, eid: str) -> dict[str, Any]:
        e = self._svc().events().get(calendarId=cal, eventId=eid).execute()
        out = slim_event(e, cal)
        out["description"] = _strip_html(e.get("description") or "")[:20000]
        return out

    def freebusy(
        self, emails: list[str], start: dt.datetime, end: dt.datetime, min_minutes: int
    ) -> dict[str, Any]:
        emails = [x.strip().lower() for x in emails if x and "@" in x][:20]
        items = [{"id": "primary"}] + [{"id": x} for x in emails]
        r = (
            self._svc()
            .freebusy()
            .query(
                body={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "items": items,
                    "timeZone": self.cfg.timezone,
                }
            )
            .execute()
        )
        busy: list[tuple[dt.datetime, dt.datetime]] = []
        unknown: list[str] = []
        for cid, c in (r.get("calendars") or {}).items():
            if c.get("errors"):
                unknown.append(cid)
                continue
            for b in c.get("busy") or []:
                busy.append(
                    (
                        dt.datetime.fromisoformat(b["start"].replace("Z", "+00:00")),
                        dt.datetime.fromisoformat(b["end"].replace("Z", "+00:00")),
                    )
                )
        # only inside working hours, each day
        wins: list[tuple[dt.datetime, dt.datetime]] = []
        d = start.astimezone(self.tz).date()
        h0, h1 = (dt.time.fromisoformat(x) for x in self.work_hours)
        while dt.datetime.combine(d, dt.time(0), self.tz) < end:
            ws = max(start, dt.datetime.combine(d, h0, self.tz))
            we = min(end, dt.datetime.combine(d, h1, self.tz))
            if we > ws and d.weekday() < 5:
                wins += free_windows(busy, ws, we, min_minutes)
            d += dt.timedelta(days=1)
        return {
            "windows": [{"start": s.isoformat(), "end": e.isoformat()} for s, e in wins[:40]],
            "unknown": unknown,
            "checked": emails,
        }

    def busy_for(self, emails: list[str], start: dt.datetime, end: dt.datetime) -> dict[str, Any]:
        """Which of these people are busy at [start, end) (free/busy; no event details).
        Calendars Google won't show (outside the org, private) come back as unknown."""
        emails = [x.strip().lower() for x in emails if x and "@" in x][:MAX_GUESTS]
        r = (
            self._svc()
            .freebusy()
            .query(
                body={
                    "timeMin": start.isoformat(),
                    "timeMax": end.isoformat(),
                    "items": [{"id": "primary"}] + [{"id": x} for x in emails],
                    "timeZone": self.cfg.timezone,
                }
            )
            .execute()
        )
        busy, unknown = [], []
        for cid, c in (r.get("calendars") or {}).items():
            if c.get("errors"):
                unknown.append(cid)
            elif c.get("busy"):
                busy.append("you" if cid == "primary" else cid)
        return {"busy": sorted(busy), "unknown": sorted(unknown)}

    # ------------------------------------------------------------ writes
    def _check_block(self, summary: str, start: dt.datetime, end: dt.datetime) -> None:
        if not summary.strip():
            raise CalendarError("A block needs a title.")
        mins = (end - start).total_seconds() / 60
        if mins < 5 or mins > MAX_BLOCK_MIN:
            raise CalendarError("A block must be between 5 minutes and 8 hours.")

    def create_block(
        self,
        summary: str,
        start: dt.datetime,
        end: dt.datetime,
        description: str = "",
        personal: bool = False,
        color: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        self._check_block(summary, start, end)
        visibility = self.personal_visibility if personal else "default"
        cname = (color or self.work_color).lower()
        body: dict[str, Any] = {
            "summary": summary.strip()[:200],
            "start": {"dateTime": start.isoformat(), "timeZone": self.cfg.timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self.cfg.timezone},
            "description": description[:MAX_DESC],
            "visibility": visibility,
            "extendedProperties": {"private": {TAG_KEY: TAG_VAL, "ultra_src": source[:200]}},
            "reminders": {"useDefault": True},
        }
        if cname in COLORS:
            body["colorId"] = COLORS[cname]
        cal = self.calendars[0]
        with self.lock:
            e = self._svc().events().insert(calendarId=cal, body=body).execute()
        return self._verify(cal, e["id"], start, end, visibility)

    def create_recurring_block(
        self,
        summary: str,
        start: dt.datetime,
        end: dt.datetime,
        weekdays: list[str],
        weeks: int,
        description: str = "",
        personal: bool = False,
        color: str = "",
    ) -> dict[str, Any]:
        """A weekly series of Ultra blocks (RRULE, with an end so it never runs on
        forever). Same tag and rules as a single block; no attendees."""
        self._check_block(summary, start, end)
        days = [d for d in WEEKDAYS if d in {x.upper() for x in weekdays}]
        if not days:
            raise CalendarError("Pick at least one weekday.")
        if not 1 <= weeks <= MAX_REPEAT_WEEKS:
            raise CalendarError(f"Repeat for 1 to {MAX_REPEAT_WEEKS} weeks.")
        until = (start + dt.timedelta(weeks=weeks)).astimezone(dt.UTC)
        rule = f"RRULE:FREQ=WEEKLY;BYDAY={','.join(days)};UNTIL={until.strftime('%Y%m%dT%H%M%SZ')}"
        visibility = self.personal_visibility if personal else "default"
        cname = (color or self.work_color).lower()
        body: dict[str, Any] = {
            "summary": summary.strip()[:200],
            "start": {"dateTime": start.isoformat(), "timeZone": self.cfg.timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self.cfg.timezone},
            "recurrence": [rule],
            "description": description[:MAX_DESC],
            "visibility": visibility,
            "extendedProperties": {"private": {TAG_KEY: TAG_VAL, "ultra_kind": "series"}},
            "reminders": {"useDefault": True},
        }
        if cname in COLORS:
            body["colorId"] = COLORS[cname]
        cal = self.calendars[0]
        with self.lock:
            e = self._svc().events().insert(calendarId=cal, body=body).execute()
        got = self._svc().events().get(calendarId=cal, eventId=e["id"]).execute()
        problems = []
        if (got.get("recurrence") or [None])[0] != rule:
            problems.append("repeat rule differs")
        if not is_ultra_block(got):
            problems.append("ultra tag missing")
        if got.get("attendees"):
            problems.append("has attendees")
        self.invalidate()
        return {
            "ok": not problems,
            "problems": problems,
            "event": slim_event(got, cal),
            "rule": rule,
        }

    def delete_series(self, cal: str, eid: str) -> dict[str, Any]:
        """Delete a whole Ultra block series (from any one of its occurrences)."""
        e = self._owned(cal, eid)
        master = e.get("recurringEventId") or eid
        m = self._owned(cal, master)
        if not m.get("recurrence"):
            raise CalendarError("That block is not a series.")
        return self.delete_block(cal, master)

    def rsvp(self, cal: str, eid: str, response: str, note: str = "") -> dict[str, Any]:
        """Set the operator's own response on an invitation. Only the operator's
        attendee entry changes; the organizer is told by Google (sendUpdates=all)."""
        if response not in RESPONSES:
            raise CalendarError("Response must be accepted, tentative or declined.")
        e = self._svc().events().get(calendarId=cal, eventId=eid).execute()
        if (e.get("organizer") or {}).get("self"):
            raise CalendarError("You organise this event; there is nothing to answer.")
        atts = e.get("attendees") or []
        mine = [a for a in atts if a.get("self")]
        if not mine:
            raise CalendarError("You are not on the guest list of this event.")
        new_atts = []
        for a in atts:
            b = dict(a)
            if a.get("self"):
                b["responseStatus"] = response
                if note.strip():
                    b["comment"] = note.strip()[:500]
            new_atts.append(b)
        with self.lock:
            self._svc().events().patch(
                calendarId=cal, eventId=eid, body={"attendees": new_atts}, sendUpdates="all"
            ).execute()
        got = self._svc().events().get(calendarId=cal, eventId=eid).execute()
        me: dict[str, Any] = next((a for a in got.get("attendees") or [] if a.get("self")), {})
        others_before = {a.get("email"): a.get("responseStatus") for a in atts if not a.get("self")}
        others_after = {
            a.get("email"): a.get("responseStatus")
            for a in got.get("attendees") or []
            if not a.get("self")
        }
        problems = []
        if me.get("responseStatus") != response:
            problems.append("your response did not take")
        if others_before != others_after:
            problems.append("other guests changed")  # should never happen; reported, not hidden
        self.invalidate()
        return {"ok": not problems, "problems": problems, "event": slim_event(got, cal)}

    def create_meeting(
        self,
        summary: str,
        start: dt.datetime,
        end: dt.datetime,
        guests: list[str],
        description: str = "",
        video: bool = True,
        location: str = "",
    ) -> dict[str, Any]:
        """Insert a meeting with guests. Only called from the two-approval invite flow;
        Google emails the guests. Re-read afterwards: time, guests, tag."""
        if not summary.strip():
            raise CalendarError("A meeting needs a title.")
        mins = (end - start).total_seconds() / 60
        if mins < 5 or mins > MAX_MEETING_MIN:
            raise CalendarError("A meeting must be between 5 minutes and 8 hours.")
        guests = sorted({g.strip().lower() for g in guests if g and "@" in g})
        if not guests or len(guests) > MAX_GUESTS:
            raise CalendarError(f"Invite between 1 and {MAX_GUESTS} people.")
        body: dict[str, Any] = {
            "summary": summary.strip()[:200],
            "start": {"dateTime": start.isoformat(), "timeZone": self.cfg.timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self.cfg.timezone},
            "description": description[:MAX_DESC],
            "location": location[:500],
            "attendees": [{"email": g} for g in guests],
            "extendedProperties": {"private": {TAG_KEY: TAG_VAL, "ultra_kind": "meeting"}},
            "reminders": {"useDefault": True},
            "guestsCanModify": False,
        }
        kw: dict[str, Any] = {"sendUpdates": "all"}
        if video:
            import secrets

            body["conferenceData"] = {
                "createRequest": {
                    "requestId": secrets.token_hex(8),
                    "conferenceSolutionKey": {"type": "hangoutsMeet"},
                }
            }
            kw["conferenceDataVersion"] = 1
        cal = self.calendars[0]
        with self.lock:
            e = self._svc().events().insert(calendarId=cal, body=body, **kw).execute()
        got = self._svc().events().get(calendarId=cal, eventId=e["id"]).execute()
        problems = []
        gs = dt.datetime.fromisoformat(got["start"]["dateTime"])
        ge = dt.datetime.fromisoformat(got["end"]["dateTime"])
        if gs != start or ge != end:
            problems.append("time differs from what was approved")
        have = sorted(
            a.get("email", "").lower() for a in got.get("attendees") or [] if not a.get("self")
        )
        if have != guests:
            problems.append("guest list differs from what was approved")
        if not has_ultra_tag(got):
            problems.append("ultra tag missing")
        self.invalidate()
        return {"ok": not problems, "problems": problems, "event": slim_event(got, cal)}

    def _owned(self, cal: str, eid: str) -> dict[str, Any]:
        e = self._svc().events().get(calendarId=cal, eventId=eid).execute()
        if not is_ultra_block(e):
            raise CalendarError(
                "Ultra only changes blocks it created. Edit this event in Google Calendar."
            )
        return e

    def move_block(
        self, cal: str, eid: str, start: dt.datetime, end: dt.datetime, summary: str | None = None
    ) -> dict[str, Any]:
        e = self._owned(cal, eid)
        self._check_block(summary if summary is not None else e.get("summary", "x"), start, end)
        patch: dict[str, Any] = {
            "start": {"dateTime": start.isoformat(), "timeZone": self.cfg.timezone},
            "end": {"dateTime": end.isoformat(), "timeZone": self.cfg.timezone},
        }
        if summary is not None:
            patch["summary"] = summary.strip()[:200]
        with self.lock:
            self._svc().events().patch(calendarId=cal, eventId=eid, body=patch).execute()
        return self._verify(cal, eid, start, end, e.get("visibility") or "default")

    def delete_block(self, cal: str, eid: str) -> dict[str, Any]:
        self._owned(cal, eid)
        with self.lock:
            self._svc().events().delete(calendarId=cal, eventId=eid).execute()
        try:
            e = self._svc().events().get(calendarId=cal, eventId=eid).execute()
            gone = e.get("status") == "cancelled"
        except Exception:  # noqa: BLE001 - 404/410 means gone
            gone = True
        if not gone:
            raise CalendarError("Delete did not take; check Google Calendar.")
        return {"ok": True, "deleted": eid}

    def _verify(
        self, cal: str, eid: str, start: dt.datetime, end: dt.datetime, visibility: str
    ) -> dict[str, Any]:
        e = self._svc().events().get(calendarId=cal, eventId=eid).execute()
        got_s = dt.datetime.fromisoformat(e["start"]["dateTime"])
        got_e = dt.datetime.fromisoformat(e["end"]["dateTime"])
        problems = []
        if got_s != start or got_e != end:
            problems.append("time differs from what was asked")
        if (e.get("visibility") or "default") != visibility:
            problems.append("visibility differs")
        if not is_ultra_block(e):
            problems.append("ultra tag missing")
        self.invalidate(start)
        return {"ok": not problems, "problems": problems, "event": slim_event(e, cal)}

    def invalidate(self, when: dt.datetime | None = None) -> None:
        self.store.cache_del_prefix("cal:day:")
        self.store.cache_del_prefix("cal:week:")
