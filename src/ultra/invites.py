"""Meeting invitations with two approvals (SPEC 7.7). Also RSVP with confirmation.

A meeting invite emails other people, so it gets the same care as sending mail:

1. Draft: title, time, length, guests, notes, video on/off. Editing is free. Server
   checks: guests are real addresses, at most 40, time inside 5 min to 8 h, the start
   is in the future, and each guest's free/busy is looked up (shown, not blocking).
2. Approval 1 locks the exact invite (hash of every field).
3. Review screen (approval 2): the exact invite, conflicts, anyone outside the org; a
   single-use token that expires in 10 minutes.
4. Confirm: the server checks the token, its expiry, single use and that the stored
   invite still hashes to what was approved, then creates the event (Google emails the
   guests) and reads it back.

Nothing a model returns can create a meeting: AI can only fill the draft fields.

RSVP (accept / maybe / decline an invitation) changes only the operator's own response
and needs one confirmation in the page plus a server token that ties the confirmation
to that event and response.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import secrets
import threading
import time
from typing import Any

from ultra.calendar import MAX_GUESTS, RESPONSES, CalendarError

TOKEN_TTL = 600
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
FIELDS = ("summary", "start", "minutes", "guests", "description", "video", "location")

SCHEMA = """
CREATE TABLE IF NOT EXISTS invites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL,             -- DRAFT | APPROVED | SENT | DISCARDED
    data TEXT NOT NULL,              -- JSON of FIELDS
    approved_hash TEXT,
    event_id TEXT,
    source TEXT,                     -- stream key or task it came from
    created REAL NOT NULL,
    updated REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS invite_tokens (
    token TEXT PRIMARY KEY,
    invite_id INTEGER NOT NULL,
    hash TEXT NOT NULL,
    expires REAL NOT NULL,
    used REAL
);
"""


class InviteError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def invite_hash(data: dict[str, Any]) -> str:
    canon = {k: data.get(k) for k in FIELDS}
    canon["guests"] = sorted(canon.get("guests") or [])
    return hashlib.sha256(json.dumps(canon, sort_keys=True, default=str).encode()).hexdigest()


class Invites:
    def __init__(self, store: Any, cal: Any, org_domain: str = "", me: set[str] | None = None):
        self.store = store
        self.cal = cal
        self.org = (org_domain or "").lower()
        self.me = {a.lower() for a in me or set()}
        self.lock = threading.Lock()
        with store._conn() as c:
            c.executescript(SCHEMA)

    def _db(self) -> Any:
        return self.store._conn()

    # ---------------------------------------------------------------- read
    def get(self, iid: int) -> dict[str, Any]:
        row = (
            self._db()
            .execute(
                "SELECT id, state, data, approved_hash, event_id, source, created, updated "
                "FROM invites WHERE id=?",
                (iid,),
            )
            .fetchone()
        )
        if not row:
            raise InviteError(404, "Invite not found")
        data = json.loads(row[2])
        return {
            "id": row[0],
            "state": row[1],
            "data": data,
            "approved": bool(row[3]) and row[3] == invite_hash(data),
            "event_id": row[4] or "",
            "source": row[5] or "",
        }

    # ---------------------------------------------------------------- draft
    def clean(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Validate and normalise the draft fields. Raises InviteError(400)."""
        summary = " ".join(str(raw.get("summary", "")).split())[:200]
        if not summary:
            raise InviteError(400, "The meeting needs a title.")
        try:
            start = self.cal.parse_local(str(raw.get("start", "")))
        except CalendarError as e:
            raise InviteError(400, str(e)) from e
        try:
            minutes = int(raw.get("minutes") or 30)
        except (TypeError, ValueError) as e:
            raise InviteError(400, "Length must be a number of minutes.") from e
        if not 5 <= minutes <= 480:
            raise InviteError(400, "A meeting must be between 5 minutes and 8 hours.")
        vals = raw.get("guests") or []
        if isinstance(vals, str):
            vals = re.split(r"[,;\n]+", vals)
        cands: list[str] = []
        for raw_c in vals:
            chunk = str(raw_c).strip()
            if not chunk:
                continue
            m = re.search(r"<([^<>]+)>", chunk)
            # "Name <addr>" -> addr; otherwise every word must itself be an address
            cands += [m.group(1)] if m else chunk.split()
        guests: list[str] = []
        for c in cands:
            g = c.strip().strip("\"'").lower()
            if not EMAIL_RE.match(g):
                raise InviteError(400, f"Not an email address: {g[:80]}")
            if g in self.me:
                continue  # you are the organizer
            if g not in guests:
                guests.append(g)
        if not guests:
            raise InviteError(400, "Invite at least one person.")
        if len(guests) > MAX_GUESTS:
            raise InviteError(400, f"At most {MAX_GUESTS} guests.")
        return {
            "summary": summary,
            "start": start.isoformat(),
            "minutes": minutes,
            "guests": guests,
            "description": str(raw.get("description", ""))[:7000],
            "video": bool(raw.get("video", True)),
            "location": " ".join(str(raw.get("location", "")).split())[:300],
        }

    def create(self, raw: dict[str, Any], source: str = "") -> dict[str, Any]:
        data = self.clean(raw)
        now = time.time()
        with self._db() as c:
            cur = c.execute(
                "INSERT INTO invites (state, data, source, created, updated) VALUES "
                "('DRAFT', ?, ?, ?, ?)",
                (json.dumps(data), source[:200], now, now),
            )
        return self.get(int(cur.lastrowid or 0))

    def update(self, iid: int, raw: dict[str, Any]) -> dict[str, Any]:
        inv = self.get(iid)
        if inv["state"] not in ("DRAFT", "APPROVED"):
            raise InviteError(409, f"Invite is {inv['state']}; it can't be edited.")
        data = self.clean({**inv["data"], **raw})
        with self._db() as c:
            # any edit voids approval and any issued token
            c.execute(
                "UPDATE invites SET data=?, state='DRAFT', approved_hash=NULL, updated=? "
                "WHERE id=?",
                (json.dumps(data), time.time(), iid),
            )
            c.execute(
                "UPDATE invite_tokens SET used=? WHERE invite_id=? AND used IS NULL",
                (time.time(), iid),
            )
        return self.get(iid)

    def discard(self, iid: int) -> dict[str, Any]:
        inv = self.get(iid)
        if inv["state"] == "SENT":
            raise InviteError(409, "Already sent; cancel it in Google Calendar.")
        with self._db() as c:
            c.execute(
                "UPDATE invites SET state='DISCARDED', updated=? WHERE id=?", (time.time(), iid)
            )
        return self.get(iid)

    # ---------------------------------------------------------------- checks shown on review
    def checks(self, data: dict[str, Any]) -> dict[str, Any]:
        start = dt.datetime.fromisoformat(data["start"])
        end = start + dt.timedelta(minutes=int(data["minutes"]))
        out: dict[str, Any] = {
            "past": start <= dt.datetime.now(start.tzinfo),
            "busy": [],
            "unknown": [],
            "external": [],
        }
        if self.org:
            out["external"] = [g for g in data["guests"] if not g.endswith("@" + self.org)]
        try:
            fb = self.cal.busy_for(data["guests"], start, end)
            out["busy"], out["unknown"] = fb["busy"], fb["unknown"]
        except Exception as e:  # noqa: BLE001 - shown as unknown, never blocks
            out["unknown"] = list(data["guests"])
            out["fb_error"] = str(e)[:200]
        return out

    # ---------------------------------------------------------------- approval 1
    def approve(self, iid: int) -> dict[str, Any]:
        inv = self.get(iid)
        if inv["state"] != "DRAFT":
            raise InviteError(409, f"Invite is {inv['state']}; only a draft can be approved.")
        if self.checks(inv["data"])["past"]:
            raise InviteError(422, "That time is in the past.")
        with self._db() as c:
            c.execute(
                "UPDATE invites SET state='APPROVED', approved_hash=?, updated=? WHERE id=?",
                (invite_hash(inv["data"]), time.time(), iid),
            )
        return self.get(iid)

    def unapprove(self, iid: int) -> dict[str, Any]:
        with self._db() as c:
            c.execute(
                "UPDATE invites SET state='DRAFT', approved_hash=NULL "
                "WHERE id=? AND state='APPROVED'",
                (iid,),
            )
            c.execute(
                "UPDATE invite_tokens SET used=? WHERE invite_id=? AND used IS NULL",
                (time.time(), iid),
            )
        return self.get(iid)

    # ---------------------------------------------------------------- approval 2
    def review(self, iid: int) -> dict[str, Any]:
        inv = self.get(iid)
        if inv["state"] != "APPROVED":
            raise InviteError(409, "Approve the invite first.")
        if not inv["approved"]:
            raise InviteError(409, "The invite changed after approval; approve it again.")
        h = invite_hash(inv["data"])
        token = secrets.token_urlsafe(24)
        with self._db() as c:
            c.execute(
                "INSERT INTO invite_tokens (token, invite_id, hash, expires) VALUES (?,?,?,?)",
                (token, iid, h, time.time() + TOKEN_TTL),
            )
        return {
            "token": token,
            "expires_in": TOKEN_TTL,
            "invite": inv,
            "checks": self.checks(inv["data"]),
        }

    def confirm(self, iid: int, token: str) -> dict[str, Any]:
        with self.lock:
            inv = self.get(iid)
            row = (
                self._db()
                .execute(
                    "SELECT invite_id, hash, expires, used FROM invite_tokens WHERE token=?",
                    (token,),
                )
                .fetchone()
            )
            if not row:
                raise InviteError(403, "Unknown approval token")
            t_iid, t_hash, t_exp, t_used = row
            if t_used is not None:
                raise InviteError(409, "This approval was already used")
            if time.time() > t_exp:
                raise InviteError(409, "Approval expired; review again")
            if t_iid != iid or inv["state"] != "APPROVED":
                raise InviteError(409, "Approval does not match this invite")
            if invite_hash(inv["data"]) != t_hash:
                raise InviteError(409, "The invite changed after approval; nothing was sent")
            with self._db() as c:
                c.execute("UPDATE invite_tokens SET used=? WHERE token=?", (time.time(), token))
            d = inv["data"]
            start = dt.datetime.fromisoformat(d["start"])
            if start <= dt.datetime.now(start.tzinfo):
                raise InviteError(422, "That time is now in the past; nothing was sent")
            try:
                res = self.cal.create_meeting(
                    d["summary"],
                    start,
                    start + dt.timedelta(minutes=int(d["minutes"])),
                    d["guests"],
                    description=d["description"],
                    video=d["video"],
                    location=d["location"],
                )
            except CalendarError as e:
                raise InviteError(400, str(e)) from e
            with self._db() as c:
                c.execute(
                    "UPDATE invites SET state='SENT', event_id=?, updated=? WHERE id=?",
                    ((res.get("event") or {}).get("id", ""), time.time(), iid),
                )
        self.store.journal(
            "invite_sent",
            str(iid),
            bool(res.get("ok")),
            {
                "summary": d["summary"][:80],
                "guests": len(d["guests"]),
                "problems": res.get("problems"),
            },
        )
        return {**res, "invite": self.get(iid)}


class Rsvp:
    """RSVP needs a server token that names the event and the response, issued when the
    page shows the confirmation. A replayed or altered request is refused."""

    def __init__(self, store: Any, cal: Any):
        self.store = store
        self.cal = cal
        self.tokens: dict[str, tuple[str, str, str, float]] = {}
        self.lock = threading.Lock()

    def ask(self, cal: str, eid: str, response: str) -> dict[str, Any]:
        if response not in RESPONSES:
            raise InviteError(400, "Response must be accepted, tentative or declined.")
        e = self.cal.event(cal, eid)
        if e.get("organizer_self"):
            raise InviteError(409, "You organise this event; there is nothing to answer.")
        if not any(a.get("self") for a in e.get("attendees") or []):
            raise InviteError(409, "You are not on the guest list of this event.")
        tok = secrets.token_urlsafe(18)
        with self.lock:
            now = time.time()
            self.tokens = {k: v for k, v in self.tokens.items() if v[3] > now}
            self.tokens[tok] = (cal, eid, response, now + 300)
        return {
            "token": tok,
            "event": {
                k: e.get(k)
                for k in ("summary", "start", "end", "organizer", "my_response", "recurring")
            },
            "response": response,
        }

    def confirm(
        self, token: str, cal: str, eid: str, response: str, note: str = ""
    ) -> dict[str, Any]:
        with self.lock:
            t = self.tokens.pop(token, None)
        if not t:
            raise InviteError(403, "Confirmation expired or already used; try again.")
        if t[3] < time.time() or t[:3] != (cal, eid, response):
            raise InviteError(409, "Confirmation does not match this event and response.")
        try:
            res = self.cal.rsvp(cal, eid, response, note)
        except CalendarError as e:
            raise InviteError(409, str(e)) from e
        self.store.journal("cal_rsvp", eid, bool(res.get("ok")), {"response": response})
        return res
