"""Inbox Tidy (v1.1, SPEC 8.1): a rule-based bulk archive, previewed before it runs.

Rule (deterministic, from the same stream the Desk shows; no AI):

    archive  automated or bulk mail (court LOW)
    archive  anything else older than N days (default 7) that is not one of the keeps
    keep     your move (court MINE), VIP, READY, tickets assigned to you, items on the
             Board's Watching list, anything with activity today, and everything
             newer than N days

Flow: preview (the full kept and archived lists with a reason for each) -> the operator
unticks anything -> run with the preview's single-use token (the server archives only
threads that were in that preview and are still ticked) -> one Undo puts every thread
back in INBOX. Archive removes the INBOX label only: nothing is deleted or marked read.
Slack and ledger tasks are never touched.
"""

from __future__ import annotations

import datetime as dt
import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any

DEFAULT_DAYS = 7
MAX_DAYS = 365
TOKEN_TTL = 15 * 60
UNDO_TTL = 24 * 3600
MAX_ARCHIVE = 500
SUGGEST_AT = 10  # calm stream: offer Tidy as a one-line suggestion at this many threads
TID_RE = re.compile(r"^[A-Za-z0-9]{1,40}$")


def _bad(msg: str, status: int = 400) -> Exception:
    from ultra.server import ApiError

    return ApiError(status, msg)


def _threads(it: dict[str, Any]) -> list[str]:
    """Gmail thread ids behind a stream row (a ticket row can hold several)."""
    if it.get("source") == "ticket":
        return [t for t in it.get("threads") or [] if TID_RE.match(str(t))]
    key = str(it.get("key") or "")
    tid = key[2:] if key.startswith("g-") else ""
    return [tid] if TID_RE.match(tid) else []


def _ts_ms(it: dict[str, Any]) -> float:
    """Last-message time in ms: ts_ms from Gmail rows, else the ISO ts."""
    if it.get("ts_ms"):
        return float(it["ts_ms"])
    try:
        return dt.datetime.fromisoformat(str(it.get("ts") or "")).timestamp() * 1000
    except ValueError:
        return 0.0


def plan(
    items: list[dict[str, Any]],
    days: int,
    now_ms: float,
    watched: set[str],
    today_start_ms: float,
) -> dict[str, list[dict[str, Any]]]:
    """Split the mail rows into archive / keep, each with a reason. Pure."""
    archive: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    cutoff = now_ms - days * 86400_000
    for it in items:
        if it.get("source") not in ("email", "ticket"):
            continue
        tids = _threads(it)
        if not tids:
            continue
        row: dict[str, Any] = {
            "key": it.get("key"),
            "source": it.get("source"),
            "from": it.get("from") or "",
            "subject": it.get("subject") or "",
            "ts": it.get("ts") or "",
            "court": it.get("court") or "",
            "badges": list(it.get("badges") or []),
            "threads": tids,
        }
        badges = set(it.get("badges") or [])
        ts = _ts_ms(it)
        court = row["court"]
        why_keep = ""
        if it.get("key") in watched:
            why_keep = "on your Board's Watching list"
        elif "VIP" in badges:
            why_keep = "VIP"
        elif "READY" in badges:
            why_keep = "a peer said it is ready"
        elif "ASSIGNED" in badges:
            why_keep = "ticket assigned to you"
        elif court == "MINE":
            why_keep = "your move"
        elif ts >= today_start_ms:
            why_keep = "activity today"
        if why_keep:
            keep.append({**row, "reason": why_keep})
            continue
        if court == "LOW":
            archive.append({**row, "reason": "automated or bulk sender"})
        elif ts and ts < cutoff:
            age = int((now_ms - ts) // 86400_000)
            what = "waiting on them" if court == "WAITING" else "no action"
            archive.append({**row, "reason": f"{what}, {age} days old"})
        else:
            keep.append({**row, "reason": f"newer than {days} days"})
    return {"archive": archive, "keep": keep}


class Tidy:
    def __init__(
        self,
        store: Any,
        items_fn: Callable[[], list[dict[str, Any]]],
        archive_fn: Callable[[list[str]], list[str]],
        unarchive_fn: Callable[[list[str]], list[str]],
        watched_fn: Callable[[], set[str]],
        tz: Any,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.store = store
        self.items_fn = items_fn
        self.archive_fn = archive_fn
        self.unarchive_fn = unarchive_fn
        self.watched_fn = watched_fn
        self.tz = tz
        self.now = now or (lambda: dt.datetime.now(tz))
        self.lock = threading.Lock()
        self.previews: dict[str, dict[str, Any]] = {}  # token -> {threads, expires}
        self.undo: dict[str, dict[str, Any]] = {}  # token -> {threads, expires}

    def register(self, api: Any) -> None:
        api.add("GET", r"/api/mail/tidy/count", self.r_count)
        api.add("POST", r"/api/mail/tidy/preview", self.r_preview)
        api.add("POST", r"/api/mail/tidy/run", self.r_run)
        api.add("POST", r"/api/mail/tidy/undo", self.r_undo)

    def _days(self, body: Any) -> int:
        raw = (body or {}).get("days", DEFAULT_DAYS)
        try:
            d = int(raw)
        except (TypeError, ValueError) as e:
            raise _bad("days must be a whole number") from e
        if not 1 <= d <= MAX_DAYS:
            raise _bad(f"days must be 1 to {MAX_DAYS}")
        return d

    def _prune(self) -> None:
        t = time.time()
        for d in (self.previews, self.undo):
            for k in [k for k, v in d.items() if v["expires"] < t]:
                del d[k]

    def r_count(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """How many threads the default rule would tidy (read only, no token). The
        calm stream shows a one-line suggestion when this reaches SUGGEST_AT."""
        now = self.now()
        start = dt.datetime.combine(now.date(), dt.time(0, 0), self.tz)
        p = plan(
            self.items_fn(),
            DEFAULT_DAYS,
            now.timestamp() * 1000,
            self.watched_fn(),
            start.timestamp() * 1000,
        )
        threads = {t for r in p["archive"] for t in r["threads"]}
        return {"count": len(threads), "days": DEFAULT_DAYS, "suggest_at": SUGGEST_AT}

    def r_preview(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        days = self._days(body)
        now = self.now()
        start = dt.datetime.combine(now.date(), dt.time(0, 0), self.tz)
        p = plan(
            self.items_fn(),
            days,
            now.timestamp() * 1000,
            self.watched_fn(),
            start.timestamp() * 1000,
        )
        threads = sorted({t for r in p["archive"] for t in r["threads"]})
        token = secrets.token_urlsafe(18)
        with self.lock:
            self._prune()
            self.previews[token] = {"threads": set(threads), "expires": time.time() + TOKEN_TTL}
        return {"token": token, "days": days, **p, "thread_count": len(threads)}

    def r_run(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        token = str(b.get("token") or "")
        want = [str(t) for t in (b.get("threads") or [])]
        if not want:
            raise _bad("nothing selected")
        if len(want) > MAX_ARCHIVE or not all(TID_RE.match(t) for t in want):
            raise _bad("threads must be Gmail thread ids")
        with self.lock:
            self._prune()
            prev = self.previews.pop(token, None)  # single use, even if the run fails
        if prev is None:
            raise _bad("this preview has expired or was already used; preview again", 409)
        outside = sorted(set(want) - prev["threads"])
        if outside:
            raise _bad(f"{len(outside)} thread(s) were not in the preview", 409)
        done = self.archive_fn(sorted(set(want)))
        undo = secrets.token_urlsafe(18)
        with self.lock:
            self.undo[undo] = {"threads": list(done), "expires": time.time() + UNDO_TTL}
        self.store.journal("tidy", f"{len(done)} threads", True, {"count": len(done)})
        return {"archived": done, "undo": undo}

    def r_undo(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        token = str((body or {}).get("undo") or "")
        with self.lock:
            self._prune()
            u = self.undo.pop(token, None)
        if u is None:
            raise _bad("nothing to undo (it expired or was already undone)", 409)
        back = self.unarchive_fn(u["threads"])
        self.store.journal("tidy_undo", f"{len(back)} threads", True, {"count": len(back)})
        return {"unarchived": back}
