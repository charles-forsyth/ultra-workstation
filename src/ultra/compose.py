"""Composer and double approval (SPEC 9).

States: DRAFT -> (approve 1) -> APPROVED -> (approve 2, token) -> QUEUED ->
(delay) -> SENT. Any edit returns to DRAFT and voids approval. The server checks the
approval token, its expiry, single use, and that the stored version still hashes to
the approved hash before anything is sent. Nothing a model returns can reach send().
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import secrets
import threading
import time
from email.message import EmailMessage
from typing import Any

from ultra import google_auth
from ultra.config import Config
from ultra.lint import Issue, blocking, lint, load_style
from ultra.store import Store

TOKEN_TTL = 600  # seconds an approval-2 token stays valid
FIELDS = ("from_addr", "to_addrs", "cc", "bcc", "subject", "body")


class ComposeError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def content_hash(draft: dict[str, Any], version: dict[str, Any]) -> str:
    canon = {k: (version.get(k) or "").strip() for k in FIELDS}
    canon["thread_id"] = draft.get("thread_id") or ""
    canon["in_reply_to"] = draft.get("in_reply_to") or ""
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


def build_mime(draft: dict[str, Any], v: dict[str, Any]) -> EmailMessage:
    """Plain-text message. Threading headers keep replies in the same thread."""
    m = EmailMessage()
    m["From"] = v.get("from_addr") or "me"
    m["To"] = v.get("to_addrs") or ""
    if v.get("cc"):
        m["Cc"] = v["cc"]
    if v.get("bcc"):
        m["Bcc"] = v["bcc"]
    m["Subject"] = v.get("subject") or ""
    if draft.get("in_reply_to"):
        m["In-Reply-To"] = draft["in_reply_to"]
        m["References"] = ((draft.get("refs") or "") + " " + draft["in_reply_to"]).strip()
    m["Date"] = email.utils.formatdate(localtime=True)
    m.set_content(v.get("body") or "")
    return m


def raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


def _reply_subject(s: str) -> str:
    return s if s.lower().startswith("re:") else f"Re: {s}"


def _fwd_subject(s: str) -> str:
    return s if s.lower().startswith(("fwd:", "fw:")) else f"Fwd: {s}"


class Composer:
    def __init__(self, cfg: Config, store: Store, me: set[str]):
        self.cfg = cfg
        self.store = store
        self.me = me
        self.delay = int(cfg.get("mail", "send_delay_seconds", 15))
        self.org = str(cfg.get("ledger", "org_email_domain", "")).lower()
        self._timers: dict[int, threading.Timer] = {}
        self._lock = threading.Lock()

    def _db(self):
        return self.store._conn()

    # ---------------------------------------------------------------- read
    def get(self, did: int) -> dict[str, Any]:
        c = self._db()
        c.row_factory = None
        row = c.execute("SELECT * FROM drafts WHERE id = ?", (did,)).fetchone()
        if not row:
            raise ComposeError(404, "Draft not found")
        cols = [d[0] for d in c.execute("SELECT * FROM drafts LIMIT 0").description]
        d = dict(zip(cols, row, strict=True))
        vcols = [x[0] for x in c.execute("SELECT * FROM draft_versions LIMIT 0").description]
        d["versions"] = [
            dict(zip(vcols, r, strict=True))
            for r in c.execute(
                "SELECT * FROM draft_versions WHERE draft_id = ? ORDER BY version",
                (did,),
            ).fetchall()
        ]
        for v in d["versions"]:
            v["lint"] = json.loads(v["lint"] or "[]")
        d["current"] = d["versions"][-1] if d["versions"] else None
        if d["kind"] == "slack" and d["state"] == "SENT":
            hit = self.store.cache_get(f"draft:{did}:sent_result")
            d["sent_result"] = hit[0] if hit else None
        return d

    def for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        rows = (
            self._db()
            .execute(
                "SELECT id FROM drafts WHERE thread_id = ? AND state NOT IN ('SENT','DISCARDED') "
                "ORDER BY id DESC",
                (thread_id,),
            )
            .fetchall()
        )
        return [self.get(r[0]) for r in rows]

    # ---------------------------------------------------------------- create
    def create(self, kind: str, thread: dict[str, Any] | None, from_addr: str) -> dict[str, Any]:
        """New draft from a thread (reply / reply_all / forward) or blank (new)."""
        now = time.time()
        to, cc, subject, body = "", "", "", ""
        thread_id = reply_to = in_reply_to = refs = None
        participants: set[str] = set()
        if thread and thread.get("messages"):
            msgs = thread["messages"]

            def mine(m: dict[str, Any]) -> bool:
                addrs = {a.lower() for _, a in email.utils.getaddresses([m.get("from") or ""])}
                return bool(m.get("mine")) or bool(addrs & self.me)

            target = next((m for m in reversed(msgs) if not mine(m)), msgs[-1])
            thread_id = thread["key"].removeprefix("g-")
            reply_to = target["id"]
            in_reply_to = target.get("message_id") or None
            refs = target.get("references") or None
            base = next((m["subject"] for m in msgs if m.get("subject")), "")
            for m in msgs:
                for f in ("from", "to", "cc"):
                    for _, a in email.utils.getaddresses([m.get(f) or ""]):
                        if a and a.lower() not in self.me:
                            participants.add(a.lower())
            if kind in ("reply", "reply_all"):
                subject = _reply_subject(base)
                to = target["from"] if not mine(target) else target.get("to", "")
                if kind == "reply_all":
                    first = {a.lower() for _, a in email.utils.getaddresses([to])}
                    rest = [
                        f"{n} <{a}>" if n else a
                        for n, a in email.utils.getaddresses(
                            [target.get("to") or "", target.get("cc") or ""]
                        )
                        if a and a.lower() not in self.me and a.lower() not in first
                    ]
                    cc = ", ".join(dict.fromkeys(rest))
            elif kind == "forward":
                subject = _fwd_subject(base)
                thread_id = None  # a forward starts a new thread
                in_reply_to = refs = None
                body = (
                    "\n\n---------- Forwarded message ----------\n"
                    f"From: {target['from']}\nDate: {target.get('ts', '')}\n"
                    f"Subject: {base}\nTo: {target.get('to', '')}\n\n"
                    f"{target.get('body', '')}"
                )
        c = self._db()
        with c:
            cur = c.execute(
                "INSERT INTO drafts (kind, thread_id, reply_to_msg, in_reply_to, refs, "
                "state, created_at, updated_at) VALUES (?,?,?,?,?, 'DRAFT', ?, ?)",
                (kind, thread_id, reply_to, in_reply_to, refs, now, now),
            )
            did = int(cur.lastrowid or 0)
        self.store.cache_put(f"draft:{did}:participants", sorted(participants))
        return self.save(
            did,
            {
                "from_addr": from_addr,
                "to_addrs": to,
                "cc": cc,
                "bcc": "",
                "subject": subject,
                "body": body,
            },
            author="me",
            instruction="created",
        )

    def create_slack(self, channel_id: str, thread_ts: str, label: str) -> dict[str, Any]:
        """A Slack reply draft. The target (channel id, thread ts) is fixed at creation and
        is part of the approval hash (thread_id, in_reply_to), so it cannot be changed
        after approval. Kept locally, never synced to Gmail."""
        now = time.time()
        c = self._db()
        with c:
            cur = c.execute(
                "INSERT INTO drafts (kind, thread_id, reply_to_msg, in_reply_to, refs, "
                "state, created_at, updated_at) VALUES ('slack',?,?,?,NULL,'DRAFT',?,?)",
                (f"slack:{channel_id}", thread_ts or None, thread_ts or "", now, now),
            )
            did = int(cur.lastrowid or 0)
        return self.save(
            did,
            {
                "from_addr": "slack",
                "to_addrs": label[:200],
                "cc": "",
                "bcc": "",
                "subject": "",
                "body": "",
            },
            author="me",
            instruction="created",
        )

    def for_slack(self, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
        rows = (
            self._db()
            .execute(
                "SELECT id FROM drafts WHERE kind='slack' AND thread_id = ? AND "
                "COALESCE(in_reply_to,'') = ? AND state NOT IN ('SENT','DISCARDED') "
                "ORDER BY id DESC",
                (f"slack:{channel_id}", thread_ts or ""),
            )
            .fetchall()
        )
        return [self.get(r[0]) for r in rows]

    # ---------------------------------------------------------------- versions
    def _lint(self, did: int, d: dict[str, Any], v: dict[str, Any]) -> list[Issue]:
        hit = self.store.cache_get(f"draft:{did}:participants")
        return lint(
            v,
            load_style(),
            thread_participants=set(hit[0]) if hit else set(),
            me=self.me,
            org_domain=self.org,
            kind=d["kind"],
        )

    def save(
        self, did: int, fields: dict[str, Any], author: str, instruction: str = ""
    ) -> dict[str, Any]:
        d = self.get(did)
        if d["state"] in ("QUEUED", "SENT", "DISCARDED"):
            raise ComposeError(409, f"Draft is {d['state']}; it can't be edited")
        cur = d["current"] or {}
        v = {k: fields.get(k, cur.get(k, "")) or "" for k in FIELDS}
        if cur and all(v[k] == (cur.get(k) or "") for k in FIELDS) and author == "me":
            return d  # nothing changed
        issues = self._lint(did, d, v)
        n = (cur.get("version") or 0) + 1
        c = self._db()
        with c:
            c.execute(
                "INSERT INTO draft_versions (draft_id, version, from_addr, to_addrs, cc, "
                "bcc, subject, body, author, instruction, lint, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    did,
                    n,
                    v["from_addr"],
                    v["to_addrs"],
                    v["cc"],
                    v["bcc"],
                    v["subject"],
                    v["body"],
                    author,
                    instruction,
                    json.dumps([i.to_dict() for i in issues]),
                    time.time(),
                ),
            )
            # any edit voids approval
            c.execute(
                "UPDATE drafts SET state='DRAFT', approved_version=NULL, approved_hash=NULL, "
                "updated_at=? WHERE id=?",
                (time.time(), did),
            )
            c.execute(
                "UPDATE approvals SET used_at=? WHERE draft_id=? AND used_at IS NULL",
                (time.time(), did),
            )
        return self.get(did)

    def restore(self, did: int, version: int) -> dict[str, Any]:
        d = self.get(did)
        old = next((v for v in d["versions"] if v["version"] == version), None)
        if not old:
            raise ComposeError(404, "No such version")
        return self.save(did, old, author="me", instruction=f"restored v{version}")

    def discard(self, did: int) -> dict[str, Any]:
        d = self.get(did)
        if d["state"] in ("QUEUED", "SENT"):
            raise ComposeError(409, f"Draft is {d['state']}")
        with self._db() as c:
            c.execute(
                "UPDATE drafts SET state='DISCARDED', updated_at=? WHERE id=?", (time.time(), did)
            )
        self._delete_gmail_draft(d)
        return self.get(did)

    # ---------------------------------------------------------------- approval 1
    def approve(self, did: int) -> dict[str, Any]:
        d = self.get(did)
        if d["state"] != "DRAFT" or not d["current"]:
            raise ComposeError(409, f"Draft is {d['state']}; only a DRAFT can be approved")
        v = d["current"]
        if blocking([Issue(**i) for i in v["lint"]]):
            raise ComposeError(422, "Fix the errors shown before approving")
        h = content_hash(d, v)
        with self._db() as c:
            c.execute(
                "UPDATE drafts SET state='APPROVED', approved_version=?, approved_hash=?, "
                "updated_at=? WHERE id=?",
                (v["version"], h, time.time(), did),
            )
        return self.get(did)

    def unapprove(self, did: int) -> dict[str, Any]:
        d = self.get(did)
        if d["state"] != "APPROVED":
            raise ComposeError(409, "Not approved")
        with self._db() as c:
            c.execute(
                "UPDATE drafts SET state='DRAFT', approved_version=NULL, "
                "approved_hash=NULL WHERE id=?",
                (did,),
            )
            c.execute(
                "UPDATE approvals SET used_at=? WHERE draft_id=? AND used_at IS NULL",
                (time.time(), did),
            )
        return self.get(did)

    # ---------------------------------------------------------------- approval 2
    def review(self, did: int) -> dict[str, Any]:
        """Open the send dialog: returns the exact message and a one-time token."""
        d = self.get(did)
        if d["state"] != "APPROVED":
            raise ComposeError(409, "Approve the draft first")
        v = next(x for x in d["versions"] if x["version"] == d["approved_version"])
        if content_hash(d, v) != d["approved_hash"]:
            raise ComposeError(409, "The approved text changed; approve it again")
        token = secrets.token_urlsafe(24)
        now = time.time()
        with self._db() as c:
            c.execute(
                "INSERT INTO approvals (token, draft_id, version, content_hash, issued_at, "
                "expires_at) VALUES (?,?,?,?,?,?)",
                (token, did, v["version"], d["approved_hash"], now, now + TOKEN_TTL),
            )
        hit = self.store.cache_get(f"draft:{did}:participants")
        present = {
            a.lower()
            for _, a in email.utils.getaddresses(
                [v["to_addrs"] or "", v["cc"] or "", v["bcc"] or ""]
            )
        }
        dropped = (
            sorted(set(hit[0]) - present - self.me) if hit and d["kind"] == "reply_all" else []
        )
        out = {
            "token": token,
            "expires_in": TOKEN_TTL,
            "delay": self.delay,
            "message": {k: v[k] for k in FIELDS},
            "version": v["version"],
            "dropped": dropped,
            "thread_id": d["thread_id"],
            "lint": v["lint"],
            "kind": d["kind"],
        }
        if d["kind"] == "slack":
            out["slack"] = {
                "channel_id": (d["thread_id"] or "").removeprefix("slack:"),
                "thread_ts": d["in_reply_to"] or "",
                "label": v["to_addrs"],
                "notice": "Slack messages are posted by Claude through the Slack connector "
                "(a model is in the send path). Ultra checks the posted text against this "
                "approved text afterwards and never resends on its own.",
            }
        return out

    def confirm(self, did: int, token: str, version: int, send_fn: Any) -> dict[str, Any]:
        """Check everything, then queue the send after the delay."""
        with self._lock:
            d = self.get(did)
            row = (
                self._db()
                .execute(
                    "SELECT draft_id, version, content_hash, expires_at, used_at FROM approvals "
                    "WHERE token = ?",
                    (token,),
                )
                .fetchone()
            )
            if not row:
                raise ComposeError(403, "Unknown approval token")
            t_did, t_ver, t_hash, t_exp, t_used = row
            if t_used is not None:
                raise ComposeError(409, "This approval was already used")
            if time.time() > t_exp:
                raise ComposeError(409, "Approval expired; review again")
            if t_did != did or t_ver != version or d["approved_version"] != version:
                raise ComposeError(409, "Approval does not match this draft version")
            if d["state"] != "APPROVED":
                raise ComposeError(409, f"Draft is {d['state']}")
            v = next(x for x in d["versions"] if x["version"] == version)
            if content_hash(d, v) != t_hash or t_hash != d["approved_hash"]:
                raise ComposeError(409, "Content changed after approval; nothing sent")
            with self._db() as c:
                c.execute("UPDATE approvals SET used_at=? WHERE token=?", (time.time(), token))
                c.execute(
                    "UPDATE drafts SET state='QUEUED', updated_at=? WHERE id=?", (time.time(), did)
                )
            timer = threading.Timer(self.delay, self._fire, args=(did, t_hash, send_fn))
            timer.daemon = True
            self._timers[did] = timer
            timer.start()
        return {"queued": True, "send_in": self.delay}

    def cancel(self, did: int) -> dict[str, Any]:
        with self._lock:
            t = self._timers.pop(did, None)
            d = self.get(did)
            if d["state"] != "QUEUED" or t is None:
                raise ComposeError(
                    409, "Too late to cancel" if d["state"] == "SENT" else "Not queued"
                )
            t.cancel()
            with self._db() as c:
                c.execute(
                    "UPDATE drafts SET state='APPROVED', updated_at=? WHERE id=?",
                    (time.time(), did),
                )
        self.store.journal("send_cancelled", str(did), True)
        return self.get(did)

    def _fire(self, did: int, approved_hash: str, send_fn: Any) -> None:
        with self._lock:
            self._timers.pop(did, None)
            d = self.get(did)
            if d["state"] != "QUEUED" or d["approved_hash"] != approved_hash:
                return
            v = next(x for x in d["versions"] if x["version"] == d["approved_version"])
            if content_hash(d, v) != approved_hash:
                self.store.journal("send_refused", str(did), False, "hash changed")
                return
        try:
            result = send_fn(d, v)
        except Exception as e:  # noqa: BLE001 - recorded, draft returns to APPROVED
            with self._db() as c:
                c.execute(
                    "UPDATE drafts SET state='APPROVED', updated_at=? WHERE id=?",
                    (time.time(), did),
                )
            self.store.cache_put(f"draft:{did}:send_error", str(e)[:300])
            self.store.journal("send_failed", str(did), False, str(e)[:300])
            return
        with self._db() as c:
            c.execute(
                "UPDATE drafts SET state='SENT', sent_message_id=?, updated_at=? WHERE id=?",
                (result.get("id"), time.time(), did),
            )
        self.store.journal(
            "sent",
            str(did),
            True,
            {
                "message_id": result.get("id"),
                "thread_id": result.get("threadId"),
                "to": v["to_addrs"],
                "subject": v["subject"][:80],
                "kind": d["kind"],
                "verified": result.get("verified", True),
                "verify_note": result.get("verify_note", ""),
                "permalink": result.get("permalink", ""),
            },
        )
        if d["kind"] == "slack":
            self.store.cache_put(f"draft:{did}:sent_result", result)
        self._delete_gmail_draft(d)

    # ---------------------------------------------------------------- gmail drafts
    def sync_gmail_draft(self, did: int) -> str | None:
        """Mirror the current version into a Gmail draft (visible on the phone)."""
        d = self.get(did)
        v = d["current"]
        if not v or d["state"] in ("SENT", "DISCARDED") or d["kind"] == "slack":
            return None
        g = google_auth.service(self.cfg, "gmail", "v1", "modify")
        body: dict[str, Any] = {"message": {"raw": raw(build_mime(d, v))}}
        if d["thread_id"]:
            body["message"]["threadId"] = d["thread_id"]
        if d["gmail_draft_id"]:
            r = g.users().drafts().update(userId="me", id=d["gmail_draft_id"], body=body).execute()
        else:
            r = g.users().drafts().create(userId="me", body=body).execute()
        with self._db() as c:
            c.execute("UPDATE drafts SET gmail_draft_id=? WHERE id=?", (r["id"], did))
        return str(r["id"])

    def _delete_gmail_draft(self, d: dict[str, Any]) -> None:
        if not d.get("gmail_draft_id"):
            return
        try:
            g = google_auth.service(self.cfg, "gmail", "v1", "modify")
            g.users().drafts().delete(userId="me", id=d["gmail_draft_id"]).execute()
        except Exception:  # noqa: BLE001, S110 - a stale Gmail draft is harmless
            pass


def gmail_send(cfg: Config):
    """send_fn for Composer.confirm: sends and re-reads to verify it landed."""

    def send(d: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
        g = google_auth.service(cfg, "gmail", "v1", "send")
        body: dict[str, Any] = {"raw": raw(build_mime(d, v))}
        if d.get("thread_id"):
            body["threadId"] = d["thread_id"]
        r = g.users().messages().send(userId="me", body=body).execute()
        check = google_auth.service(cfg, "gmail", "v1", "read")
        got = check.users().messages().get(userId="me", id=r["id"], format="minimal").execute()
        if "SENT" not in (got.get("labelIds") or []):
            raise RuntimeError("Sent message not found in Sent")
        return {"id": r["id"], "threadId": r.get("threadId")}

    return send
