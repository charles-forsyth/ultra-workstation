"""Local SQLite store: caches and (later) drafts, approvals, journal. Mode 600."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from ultra.config import data_dir, private_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- reply | reply_all | forward | new
    thread_id TEXT,
    reply_to_msg TEXT,               -- Gmail message id being answered
    in_reply_to TEXT,                -- RFC Message-ID header of that message
    refs TEXT,                       -- References header
    gmail_draft_id TEXT,
    state TEXT NOT NULL DEFAULT 'DRAFT',   -- DRAFT | APPROVED | QUEUED | SENT | DISCARDED
    approved_version INTEGER,
    approved_hash TEXT,
    sent_message_id TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS draft_versions (
    draft_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    from_addr TEXT, to_addrs TEXT, cc TEXT, bcc TEXT,
    subject TEXT, body TEXT,
    author TEXT NOT NULL,            -- me | ai
    instruction TEXT,
    lint TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (draft_id, version)
);
CREATE TABLE IF NOT EXISTS approvals (
    token TEXT PRIMARY KEY,
    draft_id INTEGER NOT NULL,
    version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL
);
CREATE TABLE IF NOT EXISTS bucket (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,              -- email | ticket | slack | snippet | entity
    ref TEXT NOT NULL,               -- thread key, entity uuid, ...
    data TEXT NOT NULL,              -- JSON snapshot shown in the tray
    added_at REAL NOT NULL,
    UNIQUE (kind, ref)
);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    ok INTEGER NOT NULL,
    detail TEXT
);
"""


class Store:
    def __init__(self, path: Path | None = None):
        if path is None:
            path = private_dir(data_dir()) / "state.db"
        self.path = path
        new = not path.exists()
        self._local = threading.local()
        with self._conn() as c:
            c.executescript(SCHEMA)
        if new:
            os.chmod(path, 0o600)

    def _conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=10)
            c.execute("PRAGMA journal_mode=WAL")
            self._local.conn = c
        return c

    # ---------------------------------------------------------------- cache
    def cache_get(self, key: str, ttl: float | None = None) -> tuple[Any, float] | None:
        """(value, age_seconds) or None. With ttl, stale entries return None."""
        row = (
            self._conn()
            .execute("SELECT value, fetched_at FROM kv_cache WHERE key = ?", (key,))
            .fetchone()
        )
        if not row:
            return None
        age = time.time() - row[1]
        if ttl is not None and age > ttl:
            return None
        return json.loads(row[0]), age

    def cache_put(self, key: str, value: Any) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO kv_cache (key, value, fetched_at) VALUES (?,?,?)",
                (key, json.dumps(value, default=str), time.time()),
            )

    def cache_del_prefix(self, prefix: str) -> None:
        with self._conn() as c:
            c.execute(
                "DELETE FROM kv_cache WHERE key >= ? AND key < ?", (prefix, prefix + "\uffff")
            )

    def cache_prune(self, max_age: float = 30 * 86400) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM kv_cache WHERE fetched_at < ?", (time.time() - max_age,))

    # ---------------------------------------------------------------- bucket
    def bucket_list(self) -> list[dict[str, Any]]:
        rows = (
            self._conn()
            .execute("SELECT id, kind, ref, data, added_at FROM bucket ORDER BY id")
            .fetchall()
        )
        return [
            {"id": r[0], "kind": r[1], "ref": r[2], **json.loads(r[3]), "added_at": r[4]}
            for r in rows
        ]

    def bucket_add(self, kind: str, ref: str, data: dict[str, Any]) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT OR REPLACE INTO bucket (kind, ref, data, added_at) VALUES (?,?,?,?)",
                (kind, ref, json.dumps(data, default=str), time.time()),
            )

    def bucket_remove(self, item_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM bucket WHERE id = ?", (item_id,))

    def bucket_clear(self) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM bucket")

    def journal_recent(self, limit: int = 50, prefix: str = "") -> list[dict[str, Any]]:
        rows = (
            self._conn()
            .execute(
                "SELECT ts, action, target, ok, detail FROM journal WHERE action LIKE ? "
                "ORDER BY id DESC LIMIT ?",
                (prefix + "%", limit),
            )
            .fetchall()
        )
        return [
            {
                "ts": r[0],
                "action": r[1],
                "target": r[2],
                "ok": bool(r[3]),
                "detail": json.loads(r[4] or "null"),
            }
            for r in rows
        ]

    # ---------------------------------------------------------------- journal
    def journal(self, action: str, target: str, ok: bool, detail: Any = None) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO journal (ts, action, target, ok, detail) VALUES (?,?,?,?,?)",
                (time.time(), action, target, int(ok), json.dumps(detail, default=str)),
            )
