"""Mail extras (SPEC 8.1, v0.11): search, labels, attachments, send-as addresses, and
local highlights / notes. Shared by live (Gmail) and demo (a canned backend).

Safety:
- Search and attachment reads use the read token. Nothing is fetched until clicked.
- Labels: list with the read token; apply / remove / create with the modify token.
  System labels that change what the operator sees or what is sent (INBOX, SPAM,
  TRASH, SENT, DRAFT, UNREAD, STARRED, IMPORTANT, CATEGORY_*) are refused: archive has
  its own path and Ultra never deletes or marks read. Every label change is journaled
  with an undo (the reverse change).
- Attachments saved to disk go to a private folder (mode 700, files 600) with a safe
  name; previews are served only for text, images and PDF, as a download otherwise,
  with the server's strict CSP and nosniff. Size cap 25 MB.
- Attaching to a draft: files are stored under the draft (private folder), and their
  name, size and SHA-256 are part of the approval hash, so a change voids approval.
- Send-as: only addresses Gmail lists as verified "Send mail as" aliases; the From of a
  draft is checked against that list on save and again at send.
- Highlights / notes are local only (annotations table), never sent or logged unless
  the operator adds them to the bucket.
"""

from __future__ import annotations

import base64
import builtins
import hashlib
import json
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any

MAX_ATT = 25 * 1024 * 1024
MAX_DRAFT_ATTS = 10
PREVIEW_TYPES = (
    "text/plain",
    "text/csv",
    "image/png",
    "image/jpeg",
    "image/gif",
    "application/pdf",
)
PROTECTED_LABELS = {
    "INBOX",
    "SPAM",
    "TRASH",
    "SENT",
    "DRAFT",
    "UNREAD",
    "STARRED",
    "IMPORTANT",
    "CHAT",
}
LABEL_NAME_RE = re.compile(r"^[A-Za-z0-9 _./&()+-]{1,60}$")
PART_ID_RE = re.compile(r"^[0-9]{1,3}(\.[0-9]{1,3}){0,6}$")  # MIME part position, e.g. 1.2
MSG_ID_RE = re.compile(r"^[A-Za-z0-9]{6,40}$")
COLORS = ("amber", "cyan", "magenta", "green")

SCHEMA = """
CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_key TEXT NOT NULL,
    message_id TEXT NOT NULL,
    quote TEXT NOT NULL,
    prefix TEXT NOT NULL DEFAULT '',   -- up to 40 chars before, to find the same spot again
    color TEXT NOT NULL DEFAULT 'amber',
    note TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS annotations_thread ON annotations (thread_key);
CREATE TABLE IF NOT EXISTS saved_searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    query TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_attachments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    mime TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    path TEXT NOT NULL,
    added_at REAL NOT NULL
);
"""


class MailXError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def safe_name(name: str) -> str:
    """A file name safe on disk and in a header: no paths, no control characters."""
    base = os.path.basename(str(name or "").replace("\\", "/")).strip()
    base = re.sub(r"[^A-Za-z0-9._ ()+-]", "_", base)[:120].strip(" .")
    return base or "attachment"


def is_system_label(lid: str) -> bool:
    return lid in PROTECTED_LABELS or lid.startswith("CATEGORY_")


def check_query(q: str) -> str:
    q = " ".join(str(q or "").split())
    if not q:
        raise MailXError(400, "Type something to search for.")
    if len(q) > 300:
        raise MailXError(400, "Search is limited to 300 characters.")
    return q


class Annotations:
    """Local highlights and notes. Addressed by (thread key, message id, quote)."""

    def __init__(self, store: Any):
        self.store = store
        with store._conn() as c:
            c.executescript(SCHEMA)

    def _db(self) -> Any:
        return self.store._conn()

    def list(self, thread_key: str) -> list[dict[str, Any]]:
        rows = (
            self._db()
            .execute(
                "SELECT id, thread_key, message_id, quote, prefix, color, note, created_at "
                "FROM annotations WHERE thread_key=? ORDER BY id",
                (thread_key,),
            )
            .fetchall()
        )
        keys = ("id", "thread_key", "message_id", "quote", "prefix", "color", "note", "created_at")
        return [dict(zip(keys, r, strict=True)) for r in rows]

    def add(self, b: dict[str, Any]) -> dict[str, Any]:
        tk = str(b.get("thread_key", ""))
        if not re.fullmatch(r"(g|k|s)-[A-Za-z0-9_.:-]{1,80}", tk):
            raise MailXError(400, "bad thread key")
        quote = str(b.get("quote", "")).strip()
        if not 1 <= len(quote) <= 2000:
            raise MailXError(400, "Highlight between 1 and 2000 characters.")
        color = str(b.get("color") or "amber")
        if color not in COLORS:
            raise MailXError(400, "unknown colour")
        mid = str(b.get("message_id", ""))[:80]
        now = time.time()
        with self._db() as c:
            cur = c.execute(
                "INSERT INTO annotations (thread_key, message_id, quote, prefix, color, note, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    tk,
                    mid,
                    quote,
                    str(b.get("prefix", ""))[-40:],
                    color,
                    str(b.get("note", ""))[:2000],
                    now,
                    now,
                ),
            )
        return self.get(int(cur.lastrowid or 0))

    def get(self, aid: int) -> dict[str, Any]:
        row = (
            self._db()
            .execute(
                "SELECT id, thread_key, message_id, quote, prefix, color, note, created_at "
                "FROM annotations WHERE id=?",
                (aid,),
            )
            .fetchone()
        )
        if not row:
            raise MailXError(404, "Highlight not found")
        keys = ("id", "thread_key", "message_id", "quote", "prefix", "color", "note", "created_at")
        return dict(zip(keys, row, strict=True))

    def update(self, aid: int, b: dict[str, Any]) -> dict[str, Any]:
        a = self.get(aid)
        color = str(b.get("color", a["color"]))
        if color not in COLORS:
            raise MailXError(400, "unknown colour")
        note = str(b.get("note", a["note"]))[:2000]
        with self._db() as c:
            c.execute(
                "UPDATE annotations SET color=?, note=?, updated_at=? WHERE id=?",
                (color, note, time.time(), aid),
            )
        return self.get(aid)

    def delete(self, aid: int) -> dict[str, Any]:
        self.get(aid)
        with self._db() as c:
            c.execute("DELETE FROM annotations WHERE id=?", (aid,))
        return {"ok": True, "deleted": aid}


class SavedSearches:
    def __init__(self, store: Any):
        self.store = store
        with store._conn() as c:
            c.executescript(SCHEMA)

    def list(self) -> list[dict[str, Any]]:
        rows = (
            self.store._conn()
            .execute("SELECT id, name, query FROM saved_searches ORDER BY name")
            .fetchall()
        )
        return [{"id": r[0], "name": r[1], "query": r[2]} for r in rows]

    def add(self, name: str, query: str) -> dict[str, Any]:
        name = " ".join(str(name or "").split())[:60]
        if not name:
            raise MailXError(400, "Give the search a name.")
        query = check_query(query)
        with self.store._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO saved_searches (name, query, created_at) VALUES (?,?,?)",
                (name, query, time.time()),
            )
        return {"ok": True, "searches": self.list()}

    def delete(self, sid: int) -> dict[str, Any]:
        with self.store._conn() as c:
            c.execute("DELETE FROM saved_searches WHERE id=?", (sid,))
        return {"ok": True, "searches": self.list()}


class DraftFiles:
    """Files attached to a draft, kept in a private folder. Part of the approval hash."""

    def __init__(self, store: Any, folder: Path):
        self.store = store
        self.folder = folder
        with store._conn() as c:
            c.executescript(SCHEMA)

    def _dir(self, did: int) -> Path:
        p = self.folder / f"draft-{int(did)}"
        p.mkdir(parents=True, exist_ok=True)
        os.chmod(self.folder, 0o700)
        os.chmod(p, 0o700)
        return p

    def list(self, did: int) -> list[dict[str, Any]]:
        rows = (
            self.store._conn()
            .execute(
                "SELECT id, name, mime, size, sha256 FROM draft_attachments WHERE draft_id=? "
                "ORDER BY id",
                (int(did),),
            )
            .fetchall()
        )
        return [
            {"id": r[0], "name": r[1], "mime": r[2], "size": r[3], "sha256": r[4]} for r in rows
        ]

    def manifest(self, did: int) -> builtins.list[builtins.list[Any]]:
        """What goes into the approval hash: name, size, sha256 of each file."""
        return [[a["name"], a["size"], a["sha256"]] for a in self.list(did)]

    def add(self, did: int, name: str, data: bytes, mime: str = "") -> dict[str, Any]:
        if not data:
            raise MailXError(400, "That file is empty.")
        if len(data) > MAX_ATT:
            raise MailXError(413, "Attachments are limited to 25 MB each.")
        have = self.list(did)
        if len(have) >= MAX_DRAFT_ATTS:
            raise MailXError(400, f"At most {MAX_DRAFT_ATTS} attachments per email.")
        if sum(a["size"] for a in have) + len(data) > MAX_ATT:
            raise MailXError(413, "All attachments together must stay under 25 MB.")
        name = safe_name(name)
        mime = (mime or mimetypes.guess_type(name)[0] or "application/octet-stream")[:100]
        if not re.fullmatch(r"[a-z]+/[A-Za-z0-9.+_-]+", mime):
            mime = "application/octet-stream"
        digest = hashlib.sha256(data).hexdigest()
        path = self._dir(did) / f"{digest[:16]}-{name}"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        with self.store._conn() as c:
            c.execute(
                "INSERT INTO draft_attachments "
                "(draft_id, name, mime, size, sha256, path, added_at) VALUES (?,?,?,?,?,?,?)",
                (int(did), name, mime, len(data), digest, str(path), time.time()),
            )
        return {"ok": True, "attachments": self.list(did)}

    def remove(self, did: int, aid: int) -> dict[str, Any]:
        row = (
            self.store._conn()
            .execute(
                "SELECT path FROM draft_attachments WHERE id=? AND draft_id=?", (aid, int(did))
            )
            .fetchone()
        )
        if not row:
            raise MailXError(404, "No such attachment on this draft")
        with self.store._conn() as c:
            c.execute("DELETE FROM draft_attachments WHERE id=?", (aid,))
        Path(row[0]).unlink(missing_ok=True)
        return {"ok": True, "attachments": self.list(did)}

    def files(self, did: int) -> builtins.list[tuple[str, str, bytes]]:
        """(name, mime, bytes) for building the message; re-hashed so a file changed on
        disk after approval is caught (the caller compares the manifest)."""
        rows = (
            self.store._conn()
            .execute(
                "SELECT name, mime, path, sha256 FROM draft_attachments WHERE draft_id=? "
                "ORDER BY id",
                (int(did),),
            )
            .fetchall()
        )
        out = []
        for name, mime, path, sha in rows:
            data = Path(path).read_bytes()
            if hashlib.sha256(data).hexdigest() != sha:
                raise MailXError(409, f"Attachment {name} changed on disk; approve again")
            out.append((name, mime, data))
        return out

    def purge(self) -> int:
        n = 0
        if self.folder.exists():
            for d in self.folder.iterdir():
                if d.is_dir() and d.name.startswith("draft-"):
                    for f in d.iterdir():
                        f.unlink(missing_ok=True)
                        n += 1
                    d.rmdir()
        with self.store._conn() as c:
            c.execute("DELETE FROM draft_attachments")
        return n


def decode_upload(b64: str) -> bytes:
    try:
        return base64.b64decode(str(b64 or ""), validate=True)
    except (ValueError, TypeError) as e:
        raise MailXError(400, "The file did not arrive intact.") from e


def label_view(labels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Labels for the page: user labels first (alphabetical), then the system ones that
    are useful as filters. Protected ones are marked so the page never offers to apply
    them."""
    out = []
    for lab in labels:
        lid = str(lab.get("id", ""))
        out.append(
            {
                "id": lid,
                "name": str(lab.get("name", lid)),
                "type": lab.get("type", "user"),
                "protected": is_system_label(lid) or lab.get("type") == "system",
                "threads": lab.get("threadsTotal"),
                "unread": lab.get("threadsUnread"),
            }
        )
    out.sort(key=lambda x: (x["protected"], x["name"].lower()))
    return out


def dumps(x: Any) -> str:
    return json.dumps(x, sort_keys=True, default=str)
