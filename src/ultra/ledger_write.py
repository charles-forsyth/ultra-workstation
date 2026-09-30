"""Ledger writes (SPEC 8.4), only through the `nexus` CLI, only after a click.

Rules this module enforces:
- A fixed allow-list of write subcommands; anything else raises.
- argv lists, no shell. Free text goes on stdin (`nexus log -`), never argv, so `$`,
  quotes and `&` reach the ledger unchanged.
- Every entity argument must be a full UUID, so nothing typed by a person or a model
  can become a CLI option (a leading "-") or a fuzzy name match.
- Output is parsed with colour off and a very wide terminal so Rich never wraps an id.
- Never auto-retry: a retry of `log` makes a duplicate record.
- Every write is journaled (without the text) and invalidates the affected caches.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from typing import Any

from ultra.config import Config
from ultra.store import Store

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LINK_TYPES = {"PARTICIPATED_IN", "REFERENCED_IN"}
PRIORITIES = {"LOW", "MEDIUM", "HIGH", "CRITICAL"}
STATUSES = {"TODO", "IN_PROGRESS", "BLOCKED", "DONE"}
WRITE_COMMANDS = {("log",), ("tasks", "add"), ("tasks", "update"), ("link",), ("unlink",)}
MAX_TEXT = 20000


class WriteError(Exception):
    pass


def check_uuid(v: str, what: str = "id") -> str:
    v = str(v or "").strip().lower()
    if not UUID.match(v):
        raise WriteError(f"{what} must be a full ledger UUID")
    return v


def clean(out: str) -> str:
    return ANSI.sub("", out or "")


class LedgerWriter:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.binary = str(cfg.get("ledger", "binary", "nexus"))
        self.enabled = bool(cfg.get("ledger", "enabled", True)) and bool(shutil.which(self.binary))
        self.lock = threading.Lock()  # one write at a time: keeps ordering obvious

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update({"NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "4000", "LINES": "200"})
        return env

    def _run(
        self, args: list[str], stdin: str | None = None, timeout: int = 240
    ) -> tuple[int, str]:
        key = tuple(a for a in args[:2] if not a.startswith("-"))
        if key not in WRITE_COMMANDS and key[:1] not in WRITE_COMMANDS:
            raise WriteError(f"not an allowed write command: {' '.join(args[:2])}")
        if not self.enabled:
            raise WriteError("ledger CLI not found")
        with self.lock:
            r = subprocess.run(
                [self.binary, *args],
                input=stdin if stdin is not None else "",
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=self._env(),
            )
        return r.returncode, clean((r.stdout or "") + "\n" + (r.stderr or ""))

    def _invalidate(self) -> None:
        # Edges and task lists changed; trees and task lists are cheap to rebuild.
        self.store.cache_del_prefix("tasks:")
        self.store.cache_del_prefix("tree:")
        self.store.cache_del_prefix("search:")

    # ---------------------------------------------------------------- log
    def log(self, text: str, date: str, links: list[str]) -> dict[str, Any]:
        """`nexus log --date D --link U... --strict-links --yes -` with text on stdin.

        Returns {id, explicit, unresolved, ai_error, rc, output_tail}. id is None if
        nothing was saved. A non-zero rc with an id means the record exists but a
        link failed (strict links fail after the commit).
        """
        text = (text or "").strip()
        if not text:
            raise WriteError("log text is empty")
        if len(text) > MAX_TEXT:
            raise WriteError(f"log text is over {MAX_TEXT} characters")
        if not DATE.match(date or ""):
            raise WriteError("date must be YYYY-MM-DD HH:MM")
        ids = [check_uuid(u, "link") for u in dict.fromkeys(links)]
        args = ["log", "--date", date]
        for u in ids:
            args += ["--link", u]
        args += ["--strict-links", "--yes", "-"]
        rc, out = self._run(args, stdin=text + "\n", timeout=300)
        m = re.search(r"Logged \(ID: ([0-9a-f-]{36})\)", out)
        iid = m.group(1) if m else None
        res: dict[str, Any] = {
            "id": iid,
            "explicit": re.findall(r"Linked \(explicit\): (.+)$", out, re.M),
            "unresolved": re.findall(r"--link '([^']+)' did not resolve", out),
            "ai_error": "AI Error" in out,
            "rc": rc,
            "output_tail": _tail(out),
        }
        self.store.journal(
            "ledger_log",
            iid or "-",
            bool(iid) and rc == 0,
            {"date": date, "links": ids, "rc": rc, "chars": len(text)},
        )
        self._invalidate()
        return res

    # ---------------------------------------------------------------- tasks
    def task_add(self, summary: str, priority: str) -> dict[str, Any]:
        # The ledger prints the summary through Rich markup; a "[/x]" sequence makes
        # that print raise and the whole add rolls back. Square brackets become
        # parentheses so a task can never fail that way.
        summary = " ".join((summary or "").split())
        summary = summary.replace("[", "(").replace("]", ")")
        if not summary:
            raise WriteError("task text is empty")
        if len(summary) > 1000:
            raise WriteError("task text is over 1000 characters")
        if priority not in PRIORITIES:
            raise WriteError("priority must be LOW, MEDIUM, HIGH or CRITICAL")
        # "--" ends option parsing, so text that starts with "-" stays text.
        rc, out = self._run(["tasks", "add", "--priority", priority, "--", summary], timeout=180)
        m = re.search(r"\(ID: ([0-9a-f-]{36})\)", out)
        tid = m.group(1) if m else None
        res = {
            "id": tid,
            "rc": rc,
            "summary": summary,
            "output_tail": _tail(out),
        }
        self.store.journal(
            "ledger_task_add", tid or "-", bool(tid), {"priority": priority, "rc": rc}
        )
        self._invalidate()
        return res

    def task_status(self, task_id: str, status: str) -> dict[str, Any]:
        tid = check_uuid(task_id, "task")
        if status not in STATUSES:
            raise WriteError("bad status")
        rc, out = self._run(["tasks", "update", tid, "--status", status], timeout=180)
        ok = rc == 0 and "not found" not in out.lower()
        self.store.journal("ledger_task_status", tid, ok, {"status": status, "rc": rc})
        self._invalidate()
        return {"ok": ok, "rc": rc, "output_tail": _tail(out)}

    # ---------------------------------------------------------------- links
    def link(self, source: str, target: str, kind: str) -> dict[str, Any]:
        s, t = check_uuid(source, "source"), check_uuid(target, "target")
        if kind not in LINK_TYPES:
            raise WriteError("link type must be PARTICIPATED_IN or REFERENCED_IN")
        rc, out = self._run(["link", s, t, "--type", kind], timeout=180)
        # `nexus link` exits 0 even when an end is not found; read the words.
        ok = rc == 0 and "Linked " in out and "not found" not in out
        self.store.journal("ledger_link", f"{s}->{t}", ok, {"type": kind, "rc": rc})
        self._invalidate()
        return {"ok": ok, "rc": rc, "output_tail": _tail(out)}

    def unlink(self, a: str, b: str) -> dict[str, Any]:
        """Remove every edge between two nodes. Only called after a confirm click."""
        x, y = check_uuid(a, "source"), check_uuid(b, "target")
        rc, out = self._run(["unlink", x, y, "--force"], timeout=180)
        ok = rc == 0 and "Unlinked" in out
        self.store.journal("ledger_unlink", f"{x}-{y}", ok, {"rc": rc})
        self._invalidate()
        return {"ok": ok, "rc": rc, "output_tail": _tail(out)}


def _tail(out: str, n: int = 12) -> str:
    """Last lines of CLI output, minus SQL echo noise, for error display."""
    keep = [
        ln
        for ln in out.splitlines()
        if ln.strip() and "sqlalchemy" not in ln and not ln.startswith(("FROM ", "WHERE "))
    ]
    return "\n".join(keep[-n:])[:2000]
