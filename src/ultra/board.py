"""v0.15 Board (SPEC 7.3): the stream as court columns, plus Watching flags, Nudge and Done.

Columns (deterministic, from the same merged stream the Desk shows):

    MINE      court MINE (email, tickets, Slack that needs the operator)
    WAITING   court WAITING (the operator sent last), grouped by the person waited on;
              ledger tasks the operator filed as BLOCKED join the group "Blocked tasks"
    WATCHING  items the operator flagged (local only), optionally with a date; on or
              after the date the flag lapses and the item returns to its rule column
    DONE      today's journal: archived, Slack marked done, tasks completed, logs saved

Writes from the Board are only these, and each goes through an existing path:
- Watch / Unwatch: a local flag in Ultra's own store; nothing external is written.
- Nudge: one *draft* (a new email to the person, listing every thread waiting on them).
  It is a normal composer draft and needs both approvals to send.
- Done: the page runs the existing archive / log card / task complete actions the
  operator ticked on the Done card; the Board itself has no write route for them.
"""

from __future__ import annotations

import datetime as dt
import email.utils
import json
import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ultra.server import Api

KEY_RE = re.compile(r"^(g|k|s)-[A-Za-z0-9_-]{1,80}$|^t-[0-9a-f-]{36}$")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
EMAIL_RE = re.compile(r"^[^@\s<>,;]+@[^@\s<>,;]+\.[A-Za-z]{2,}$")
WATCH_PREFIX = "watch:"
MAX_NUDGE_ITEMS = 25

# journal actions that mean "finished today", and the label shown
DONE_ACTIONS = {
    "archive": "Archived",
    "slack_done": "Slack marked done",
    "ledger_task_status": "Task completed",
    "ledger_log": "Logged",
    "sent": "Sent",
    "slack_sent": "Slack posted",
}


def _bad(msg: str, status: int = 400) -> Exception:
    from ultra.server import ApiError

    return ApiError(status, msg)


def slim(it: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "key",
        "source",
        "from",
        "addr",
        "subject",
        "snippet",
        "ts",
        "court",
        "badges",
        "waiting_days",
        "priority",
        "status",
        "due_date",
        "threads",
    )
    return {k: it[k] for k in keep if k in it}


class Board:
    def __init__(
        self,
        store: Any,
        items_fn: Callable[[], list[dict[str, Any]]],
        tz: Any,
        operator: str = "",
        compose: Any = None,
        from_addr: str = "",
        my_addrs: set[str] | None = None,
        signature_fn: Callable[[], str] | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ):
        self.store = store
        self.items_fn = items_fn
        self.tz = tz
        self.operator = operator
        self.compose = compose
        self.from_addr = from_addr
        self.me = {a.lower() for a in (my_addrs or set())}
        self.signature_fn = signature_fn or (lambda: "")
        self.now = now or (lambda: dt.datetime.now(self.tz))

    def register(self, api: Api) -> None:
        api.add("GET", r"/api/board", self.r_board)
        api.add("POST", r"/api/board/watch", self.r_watch)
        api.add("POST", r"/api/board/unwatch", self.r_unwatch)
        api.add("POST", r"/api/board/nudge", self.r_nudge)

    # ---------------------------------------------------------------- watching
    def watches(self) -> dict[str, dict[str, Any]]:
        """Active Watching flags: key -> {until, note, since}. A flag whose date has come
        is dropped here (the item goes back to its rule column)."""
        today = self.now().date().isoformat()
        out: dict[str, dict[str, Any]] = {}
        for key, val in self._watch_rows():
            until = str(val.get("until") or "")
            if until and until <= today:
                continue
            out[key] = val
        return out

    def _watch_rows(self) -> list[tuple[str, dict[str, Any]]]:
        rows = (
            self.store._conn()
            .execute("SELECT key, value FROM kv_cache WHERE key LIKE ?", (WATCH_PREFIX + "%",))
            .fetchall()
        )
        out = []
        for k, v in rows:
            try:
                out.append((str(k)[len(WATCH_PREFIX) :], json.loads(v)))
            except ValueError:
                continue
        return out

    def r_watch(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        key = str(b.get("key") or "")
        if not KEY_RE.match(key):
            raise _bad("bad item key")
        until = str(b.get("until") or "")
        if until:
            if not DAY_RE.match(until):
                raise _bad("until must be YYYY-MM-DD")
            if until <= self.now().date().isoformat():
                raise _bad("pick a date after today (or none)")
        note = " ".join(str(b.get("note") or "").split())[:200]
        val = {"until": until, "note": note, "since": self.now().isoformat(timespec="minutes")}
        self.store.cache_put(WATCH_PREFIX + key, val)
        self.store.journal("board_watch", key, True, {"until": until or None})
        return {"key": key, **val}

    def r_unwatch(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        key = str((body or {}).get("key") or "")
        if not KEY_RE.match(key):
            raise _bad("bad item key")
        self.store.cache_del_prefix(WATCH_PREFIX + key)
        self.store.journal("board_unwatch", key, True)
        return {"key": key, "watching": False}

    # ---------------------------------------------------------------- columns
    def build(self) -> dict[str, Any]:
        items = self.items_fn()
        watch = self.watches()
        mine: list[dict[str, Any]] = []
        waiting: dict[str, dict[str, Any]] = {}
        watching: list[dict[str, Any]] = []
        for it in items:
            row = slim(it)
            key = row["key"]
            if key in watch:
                watching.append({**row, "watch": watch[key]})
                continue
            court = it.get("court")
            if court == "MINE":
                mine.append(row)
            elif court == "WAITING":
                addr = str(it.get("addr") or "").lower()
                gk = addr or f"name:{it.get('from', '')}"
                g = waiting.setdefault(
                    gk,
                    {
                        "who": it.get("from") or addr or "(unknown)",
                        "addr": addr,
                        "items": [],
                        "oldest_days": 0,
                    },
                )
                g["items"].append(row)
                g["oldest_days"] = max(g["oldest_days"], int(it.get("waiting_days") or 0))
            elif it.get("source") == "task" and it.get("status") == "BLOCKED":
                g = waiting.setdefault(
                    "blocked-tasks",
                    {"who": "Blocked tasks", "addr": "", "items": [], "oldest_days": 0},
                )
                g["items"].append(row)

        # My Court: READY and VIP first, then oldest waiting first
        def mine_rank(r: dict[str, Any]) -> tuple[int, int]:
            b = r.get("badges") or []
            top = 0 if "READY" in b else 1 if "VIP" in b or "ASSIGNED" in b else 2
            return (top, -int(r.get("waiting_days") or 0))

        mine.sort(key=mine_rank)
        groups = sorted(
            waiting.values(), key=lambda g: (g["addr"] == "", -g["oldest_days"], g["who"])
        )
        for g in groups:
            g["items"].sort(key=lambda r: -int(r.get("waiting_days") or 0))
            g["can_nudge"] = bool(
                g["addr"]
                and EMAIL_RE.match(g["addr"])
                and any(r["key"].startswith("g-") for r in g["items"])
            )
        watching.sort(key=lambda r: (r["watch"].get("until") or "9999", r.get("subject") or ""))
        done = self.done_today()
        return {
            "columns": {
                "mine": mine,
                "waiting": groups,
                "watching": watching,
                "done": done,
            },
            "counts": {
                "mine": len(mine),
                "waiting": sum(len(g["items"]) for g in groups),
                "watching": len(watching),
                "done": len(done),
            },
            "today": self.now().date().isoformat(),
        }

    def done_today(self) -> list[dict[str, Any]]:
        n = self.now()
        start = dt.datetime.combine(n.date(), dt.time(0), n.tzinfo).timestamp()
        rows = self.store.journal_between(start, start + 86400)
        out: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for r in rows:
            act = r["action"]
            if act not in DONE_ACTIONS or not r["ok"]:
                continue
            try:
                detail = json.loads(r.get("detail") or "null") or {}
            except ValueError:
                detail = {}
            if act == "ledger_task_status" and (detail or {}).get("status") != "DONE":
                continue
            label = DONE_ACTIONS[act]
            subj = str((detail or {}).get("subject") or "") if isinstance(detail, dict) else ""
            sig = (act, str(r.get("target") or ""))
            if sig in seen:
                continue
            seen.add(sig)
            out.append(
                {
                    "time": dt.datetime.fromtimestamp(r["ts"], n.tzinfo).strftime("%H:%M"),
                    "what": label,
                    "subject": subj,
                    "target": str(r.get("target") or "")[:80],
                }
            )
        out.reverse()  # newest first
        return out

    def r_board(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self.build()

    # ---------------------------------------------------------------- nudge
    def nudge_text(self, who: str, items: list[dict[str, Any]]) -> tuple[str, str]:
        """(subject, body) of one follow-up listing every thread waiting on the person.
        Deterministic; the operator edits it in the composer."""
        first = (who or "").split()[0] if who else ""
        first = re.sub(r"[^A-Za-z.'-]", "", first) or "there"
        lines = [f"Hi {first},", ""]
        if len(items) == 1:
            it = items[0]
            lines.append(
                f'Just checking in on "{it.get("subject", "")}" (my last note was '
                f"{_ago(it)}). Any update?"
            )
        else:
            lines.append("Checking in on a few open items with you:")
            lines.append("")
            for it in items:
                lines.append(f"- {it.get('subject', '')} (my last note {_ago(it)})")
            lines.append("")
            lines.append("Any updates on these? Happy to take them one at a time.")
        sig = self.signature_fn().strip()
        lines += ["", sig or f"{self.operator}".strip()]
        subject = (
            f"Following up: {items[0].get('subject', '')}"
            if len(items) == 1
            else f"Following up on {len(items)} open items"
        )
        return subject[:200], "\n".join(lines).rstrip() + "\n"

    def r_nudge(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """Create ONE new-email draft to the person. Never sends: the composer's two
        approvals still apply. The person and the threads come from the current board,
        not from the page, so the page cannot aim a nudge at an arbitrary address."""
        if self.compose is None:
            raise _bad("the composer is not available", 503)
        addr = str((body or {}).get("addr") or "").lower()
        if not EMAIL_RE.match(addr):
            raise _bad("pick a person from the Waiting On column")
        if addr in self.me:
            raise _bad("that is your own address")
        group = next(
            (g for g in self.build()["columns"]["waiting"] if g["addr"] == addr),
            None,
        )
        if not group:
            raise _bad("nothing is waiting on that person now", 404)
        threads = [r for r in group["items"] if r["key"].startswith("g-")][:MAX_NUDGE_ITEMS]
        if not threads:
            raise _bad("no email threads to nudge about")
        subject, text = self.nudge_text(group["who"], threads)
        name = str(group["who"] or "")
        to = email.utils.formataddr((name, addr)) if name and name != addr else addr
        d = self.compose.create("new", None, self.from_addr)
        d = self.compose.save(
            d["id"],
            {"to_addrs": to, "cc": "", "subject": subject, "body": text},
            "ai",
            "Board nudge",
        )
        self.store.journal(
            "board_nudge_draft", str(d["id"]), True, {"to": addr, "threads": len(threads)}
        )
        return {"draft": d, "threads": [r["key"] for r in threads]}


def _ago(it: dict[str, Any]) -> str:
    d = int(it.get("waiting_days") or 0)
    return "today" if d == 0 else "yesterday" if d == 1 else f"{d} days ago"
