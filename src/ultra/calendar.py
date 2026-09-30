"""Calendar adapter (SPEC 7.4, 8.2): Today view, focus blocks, free/busy, meeting prep.

Reads the operator's calendars for a day. Writes are narrow:
- Create a block: summary, start, end, description, color, visibility, no attendees.
  Every block Ultra creates carries the private extended property `ultra=1`.
- Move, resize or delete only events that carry `ultra=1` and are organised by the
  operator. Other events (meetings, invites, anything made elsewhere) are read-only,
  enforced here on the server, not only in the page.
- After every write, re-read the event and check start, end and visibility.

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


def is_ultra_block(e: dict[str, Any]) -> bool:
    priv = ((e.get("extendedProperties") or {}).get("private")) or {}
    return priv.get(TAG_KEY) == TAG_VAL and bool((e.get("organizer") or {}).get("self", True))


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
        "recurring": bool(e.get("recurringEventId")),
    }


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
        events.sort(key=lambda x: (not x["all_day"], x["start"]))
        out = {
            "day": day,
            "tz": self.cfg.timezone,
            "events": events,
            "work_hours": list(self.work_hours),
        }
        self.store.cache_put(key, out)
        return {**out, "age": 0.0}

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
