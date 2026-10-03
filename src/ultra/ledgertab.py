"""Ledger tab (v0.12): a dashboard for the whole ledger (SPEC 7.11).

Reads go through ``Ledger._run`` (its read allow-list, `nexus serve` when running,
the CLI otherwise). Writes go through ``LedgerTabWriter``, which is separate from the
mail desk's writer and has its own, larger allow-list:

- Every write is two steps. ``/api/lt/write/review`` validates the request, builds the
  exact argv, and returns a plain-language review card plus a single-use token bound to
  that argv (and stdin), valid 5 minutes. ``/api/lt/write/commit`` runs it only with
  that token. Nothing the browser sends at commit time can change the command.
- Destructive writes (delete, unlink, mark a task deleted) need a second token: the
  review returns ``confirm_1``; ``/api/lt/write/confirm`` exchanges it for
  ``confirm_2`` after a separate click; commit needs ``confirm_2``. Each is single use.
- Every entity argument is a full UUID or an exact key (netid, project id, grant
  number, exact name) that the server re-resolves to the record shown on the card.
- After the write, the record is read back and the card says what the ledger holds.
- Never retried. Every write is journaled.
- Never: db-reset, init, doctor fixes, sync, bulk tagging, email, anything in the
  ledger's own serve DENIED list. Those are not in WRITES, so they cannot be built.
- Never ``people add``: the operator's rule is that the Add to ledger button (desk,
  ``ledger_write``) is the only way a person enters the ledger.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from collections.abc import Callable
from typing import Any, ClassVar

from ultra import ledger_mcp_write
from ultra.ledger import Ledger, LedgerError
from ultra.ledger_serve import ServeError, ServeUnavailable
from ultra.ledger_write import ANSI, UUID, WriteError, check_due

TOKEN_TTL = 300


def _oneline(s: Any, n: int = 160) -> str:
    """One-line summary for a review card; the full text is on the record."""
    t = " ".join(str(s or "").split())
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."


KINDS = ("people", "labs", "gcp", "projects", "grants", "assets")
ENTITY_TYPES = {
    "Researcher": "people",
    "Lab": "labs",
    "GCPProject": "gcp",
    "ResearchProject": "projects",
    "Grant": "grants",
    "Asset": "assets",
    "Task": "tasks",
    "Interaction": "interactions",
}
# The ledger's own ConnectionType values (models.py), relationship kinds only.
CONN_TYPES = (
    "MEMBER_OF",
    "PI_OF",
    "LEADS",
    "SUPPORTS",
    "OVERSIGHT_BY",
    "FUNDS",
    "OPERATES",
    "USES_ASSET",
    "USES_RESOURCE",
    "HOSTED_ON",
    "COLLABORATES_WITH",
    "LINKS_TO",
    "PARTICIPATED_IN",
    "REFERENCED_IN",
    "LOCATED_IN",
    "PART_OF",
    "ASSIGNED_TO",
    "BLOCKS",
    "DEPENDS_ON",
)
STATUSES = ("TODO", "IN_PROGRESS", "BLOCKED", "DONE")
PRIORITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
NETID_RE = re.compile(r"^[a-z][a-z0-9]{1,15}$")
KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 &,.'()/_:+-]{0,159}$")
SET_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
SET_FORBIDDEN = {"id", "netid", "details", "vector", "bio_vector", "name", "admin_role"}
TEXT_MAX = 20_000
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}( \d{2}:\d{2})?$")
URL_RE = re.compile(r"^https://[A-Za-z0-9.-]+(/[^\s]*)?$")


def _bad(msg: str, status: int = 400) -> Exception:
    from ultra.server import ApiError

    return ApiError(status, msg)


def _ascii(s: str) -> str:
    return ANSI.sub("", s or "")


def _q(q: dict[str, list[str]], k: str, d: str = "") -> str:
    return (q.get(k) or [d])[0]


def _int(v: Any, lo: int, hi: int, d: int) -> int:
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return d


def _key(v: Any, what: str) -> str:
    s = " ".join(str(v or "").split())
    if not KEY_RE.match(s):
        raise WriteError(f"{what} is missing or has characters the ledger key cannot have")
    return s


def _uuid(v: Any, what: str) -> str:
    s = str(v or "").strip().lower()
    if not UUID.match(s):
        raise WriteError(f"{what} must be a full ledger UUID")
    return s


def _text(v: Any, what: str, n: int = TEXT_MAX) -> str:
    s = str(v or "").strip()
    if not s:
        raise WriteError(f"{what} is empty")
    if len(s) > n:
        raise WriteError(f"{what} is over {n} characters")
    # Rich markup in a printed value can make the ledger's own print raise and roll back
    return s.replace("[", "(").replace("]", ")")


def _short(v: Any, what: str, n: int = 200) -> str:
    s = " ".join(str(v or "").split())
    if len(s) > n:
        raise WriteError(f"{what} is over {n} characters")
    return s.replace("[", "(").replace("]", ")")


# ---------------------------------------------------------------------- reads
class LedgerTab:
    def __init__(
        self,
        ledger: Ledger,
        writer: LedgerTabWriter,
        store: Any,
        ai: Any = None,
        operator: str = "",
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.ledger = ledger
        self.writer = writer
        self.store = store
        self.ai = ai
        self.operator = operator
        self.now = now or dt.datetime.now
        self._slow: dict[str, dict[str, Any]] = {}
        self._slow_lock = threading.Lock()

    def register(self, api: Any) -> None:
        api.add("GET", r"/api/lt/home", self.r_home)
        api.add("GET", r"/api/lt/list/(people|labs|gcp|projects|grants|assets)", self.r_list)
        api.add("GET", r"/api/lt/search", self.r_search)
        api.add("GET", r"/api/lt/entity", self.r_entity)
        api.add("POST", r"/api/lt/brief", self.r_brief)
        api.add("GET", r"/api/lt/tasks", self.r_tasks)
        api.add("GET", r"/api/lt/interactions", self.r_interactions)
        api.add("GET", r"/api/lt/interaction/([0-9a-f-]{36})", self.r_interaction)
        api.add("GET", r"/api/lt/org", self.r_org)
        api.add("GET", r"/api/lt/report/(doctor|health|audit)", self.r_report)
        api.add("GET", r"/api/lt/status", self.r_status)
        api.add("POST", r"/api/lt/write/review", self.r_write_review)
        api.add("POST", r"/api/lt/write/confirm", self.r_write_confirm)
        api.add("POST", r"/api/lt/write/commit", self.r_write_commit)

    # -- helpers
    def _read(self, args: list[str], timeout: int = 180) -> Any:
        try:
            return self.ledger._run(args, timeout=timeout)
        except LedgerError as e:
            raise _bad(str(e), 502) from e

    def _cached(self, key: str, ttl: int, args: list[str], fresh: bool = False) -> Any:
        if not fresh:
            hit = self.store.cache_get(f"lt:{key}", ttl)
            if hit is not None:
                return hit[0]
        data = self._read(args)
        self.store.cache_put(f"lt:{key}", data)
        return data

    def r_status(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        h = self.ledger.serve.health() if self.ledger.serve.enabled else None
        return {
            "enabled": self.ledger.enabled,
            "mcp": getattr(self.ledger, "mcp", None) is not None,
            "serve": bool(h),
            "serve_env": (h or {}).get("env"),
            "serve_version": (h or {}).get("version"),
            "last": self.ledger.serve.last,
            "now": self.now().strftime("%Y-%m-%d %H:%M"),
        }

    # -- home
    def r_home(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        fresh = _q(q, "fresh") == "1"
        stats = self._cached("stats", 300, ["stats"], fresh) or {}
        today = self.now().date().isoformat()
        tasks = self._cached("tasks:global:", 120, ["tasks", "list", "--global"], fresh) or []
        due_soon = sorted(
            (t for t in tasks if t.get("due_date") and t.get("status") != "DONE"),
            key=lambda t: str(t.get("due_date")),
        )
        overdue = [t for t in due_soon if str(t["due_date"])[:10] < today]
        upcoming = [t for t in due_soon if str(t["due_date"])[:10] >= today][:12]
        recent = (
            self._cached("ix:recent", 120, ["interactions", "list", "--limit", "15"], fresh) or []
        )
        return {
            "now": self.now().strftime("%Y-%m-%d %H:%M"),
            "stats": stats,
            "open_tasks": len(tasks),
            "overdue": overdue[:30],
            "upcoming": upcoming,
            "recent": recent,
            "blocked": [t for t in tasks if t.get("status") == "BLOCKED"][:20],
        }

    # -- lists and search
    def r_list(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        kind = m.group(1)
        limit = _int(_q(q, "limit", "200"), 1, 5000, 200)
        args = [kind, "list", "--limit", str(limit)]
        if kind == "labs" and limit >= 1000:
            args = ["labs", "list", "--all"]
        rows = self._cached(f"list:{kind}:{limit}", 600, args, _q(q, "fresh") == "1") or []
        f = _q(q, "q").lower().strip()[:100]
        if f:
            rows = [r for r in rows if f in json.dumps(r).lower()]
        return {"kind": kind, "rows": rows, "count": len(rows)}

    def r_search(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        term = " ".join(_q(q, "q").split())[:200]
        if not term or term.startswith("-"):
            return {"results": []}
        res = self._cached(f"search:{term.lower()}", 600, ["search", term, "--limit", "30"]) or []
        return {"results": [r for r in res if isinstance(r, dict) and r.get("id")]}

    # -- entity page
    def _resolve(self, kind: str, key: str) -> dict[str, Any] | None:
        """Exact read of one record by its key. None if not found."""
        if kind == "people":
            if not NETID_RE.match(key):
                return None
            return self._read(["people", "show", key])
        if kind in ("labs", "gcp", "projects", "grants", "assets"):
            if not KEY_RE.match(key):
                return None
            return self._read([kind, "show", key])
        if kind == "tasks":
            return self._read(["tasks", "show", key]) if UUID.match(key) else None
        if kind == "interactions":
            return self._read(["interactions", "show", key]) if UUID.match(key) else None
        return None

    def r_entity(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        kind, key = _q(q, "kind"), _q(q, "key").strip()
        if kind not in (*KINDS, "tasks", "interactions"):
            raise _bad("unknown kind")
        rec = self._resolve(kind, key)
        if not isinstance(rec, dict) or not rec:
            raise _bad(f"no {kind} record for {key!r}", 404)
        rec.pop("vector", None)
        rec.pop("bio_vector", None)
        rid = str(rec.get("id") or "")
        tree: dict[str, Any] = {}
        if UUID.match(rid):
            tree = self._cached(f"tree:{rid}", 300, ["tree", rid], _q(q, "fresh") == "1") or {}
        conns = tree.get("connections") or []
        groups: dict[str, list[dict[str, Any]]] = {}
        for c in conns:
            groups.setdefault(str(c.get("entity_type") or "Other"), []).append(
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "type": c.get("type"),
                    "role": c.get("role"),
                    "kind": ENTITY_TYPES.get(str(c.get("entity_type")), ""),
                }
            )
        ix = groups.pop("Interaction", [])
        open_ids = {
            t["id"] for t in self._cached("tasks:global:", 120, ["tasks", "list", "--global"]) or []
        }
        tasks = groups.pop("Task", [])
        return {
            "kind": kind,
            "key": key,
            "record": rec,
            "groups": groups,
            "interactions": list(reversed(ix))[:200],
            "interaction_count": len(ix),
            "tasks_open": [t for t in tasks if t["id"] in open_ids],
            "tasks_done": len([t for t in tasks if t["id"] not in open_ids]),
            "write_actions": self.writer.actions_for(kind),
        }

    def _brief_context(self, page: dict[str, Any]) -> tuple[str, dict[str, str]]:
        """The data the briefing may use, each line with a citable id prefix.

        Bounded so a heavily linked record (hundreds of links, thousands of logs) still
        fits: 25 links per group, 30 open tasks, the newest 25 interactions at 700
        characters each. The briefing says how much it saw.
        """
        rec = {k: v for k, v in page["record"].items() if k not in ("links", "external_state")}
        cites: dict[str, str] = {}
        rid = str(rec.get("id") or "")
        if rid:
            cites[rid[:8]] = rid
        L = [f"[E:{rid[:8]}] Record ({page['kind']}): " + json.dumps(rec, default=str)[:3000]]
        for g, xs in page["groups"].items():
            L.append(
                f"Linked {g}: {len(xs)} in total" + (", first 25 listed" if len(xs) > 25 else "")
            )
            for x in xs[:25]:
                if x.get("id"):
                    cites[str(x["id"])[:8]] = str(x["id"])
                    L.append(f"[E:{str(x['id'])[:8]}] {x['name']} ({x.get('type') or ''})")
        L.append(f"Open tasks: {len(page['tasks_open'])}; done: {page.get('tasks_done', 0)}")
        for t in page["tasks_open"][:30]:
            cites[str(t["id"])[:8]] = str(t["id"])
            L.append(f"[T:{str(t['id'])[:8]}] {_oneline(t['name'], 300)}")
        n_ix = page.get("interaction_count", len(page["interactions"]))
        ids = [str(x["id"]) for x in page["interactions"][:25] if x.get("id")]
        L.append(f"Interactions: {n_ix} in total, newest {len(ids)} below")
        for iid in ids:
            d = self.store.cache_get(f"lt:ix:{iid}", 86400)
            rec_ix = d[0] if d else self._read(["interactions", "show", iid])
            if d is None:
                self.store.cache_put(f"lt:ix:{iid}", rec_ix)
            if isinstance(rec_ix, dict):
                cites[iid[:8]] = iid
                L.append(
                    f"[L:{iid[:8]}] {str(rec_ix.get('date', ''))[:10]}: "
                    + str(rec_ix.get("summary") or "")[:700]
                )
        return "\n".join(L)[:60_000], cites

    def r_brief(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        if self.ai is None or not getattr(self.ai, "enabled", False):
            raise _bad("AI is off")
        b = body or {}
        page = self.r_entity(
            {"kind": [str(b.get("kind", ""))], "key": [str(b.get("key", ""))]}, None, m
        )
        text, cites = self._brief_context(page)
        ck = "lt:brief:" + hashlib.sha256(text.encode()).hexdigest()[:24]
        if not b.get("fresh"):
            hit = self.store.cache_get(ck, 7 * 86400)
            if hit:
                return {**hit[0], "cached": True}
        try:
            r = self.ai.entity_brief(
                f"Today is {self.now().strftime('%A %Y-%m-%d %H:%M')}.\n{text}",
                page["kind"],
                self.operator,
            )
        except Exception as e:
            raise _bad(str(e)[:300], 502) from e
        md = r.text.strip()
        used = set(re.findall(r"\[(?:L|T|E):([0-9a-f]{8})\]", md))
        out = {
            "markdown": md,
            "cites": cites,
            "uncited_ids": sorted(used - set(cites)),  # model cited something not given
            "model": r.model,
            "seconds": round(r.seconds, 1),
        }
        self.store.cache_put(ck, out)
        return out

    # -- tasks, interactions, org
    def r_tasks(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        scope = _q(q, "scope", "global")
        args = ["tasks", "list", "--global"] if scope == "global" else ["tasks", "list"]
        if _q(q, "done") == "1":
            args.append("--all")
        rows = (
            self._cached(f"tasks:{scope}:{_q(q, 'done')}", 120, args, _q(q, "fresh") == "1") or []
        )
        today = self.now().date().isoformat()
        for t in rows:
            d = str(t.get("due_date") or "")[:10]
            t["overdue"] = bool(d) and d < today and t.get("status") != "DONE"
        return {"tasks": rows, "today": today}

    def r_interactions(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        args = ["interactions", "list", "--limit", str(_int(_q(q, "limit", "50"), 1, 500, 50))]
        since = _q(q, "since")
        if since:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
                raise _bad("since must be YYYY-MM-DD")
            args += ["--since", since]
        contains = " ".join(_q(q, "contains").split())[:100]
        if contains:
            if contains.startswith("-"):
                raise _bad("search text cannot start with '-'")
            args += ["--contains", contains]
        return {"interactions": self._read(args) or []}

    def r_interaction(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        d = self._read(["interactions", "show", m.group(1)])
        if not isinstance(d, dict):
            raise _bad("not found", 404)
        d.pop("vector", None)
        return d

    def r_org(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        unit = " ".join(_q(q, "unit").split())
        if not unit or not KEY_RE.match(unit):
            raise _bad("unit name needed")
        return {
            "tree": self._cached(
                f"org:{unit.lower()}", 900, ["org", "show", unit], _q(q, "fresh") == "1"
            )
        }

    # -- slow reports, run in the background, cached
    REPORTS: ClassVar[dict[str, tuple[list[str], int]]] = {
        "doctor": (["doctor", "--sample", "10"], 3600),
        "health": (["ship", "status", "--limit", "1000"], 3600),
        "audit": (["gcp", "audit-report"], 6 * 3600),
    }

    def r_report(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        name = m.group(1)
        args, ttl = self.REPORTS[name]
        hit = self.store.cache_get(f"lt:report:{name}", ttl)
        if hit is not None and _q(q, "fresh") != "1":
            return {"name": name, "state": "done", "data": hit[0], "cached": True}
        with self._slow_lock:
            job = self._slow.get(name)
            if job and job["state"] == "running":
                return {"name": name, "state": "running", "started": job["started"]}
            if job and job["state"] in ("done", "error") and _q(q, "fresh") != "1":
                return {"name": name, **job}
            self._slow[name] = job = {"state": "running", "started": time.time()}

        def work() -> None:
            try:
                data = self.ledger._run(args, timeout=1800)
                self.store.cache_put(f"lt:report:{name}", data)
                job.update(state="done", data=data)
            except Exception as e:  # noqa: BLE001 - shown on the card
                job.update(state="error", error=str(e)[:300])

        threading.Thread(target=work, daemon=True, name=f"lt-report-{name}").start()
        return {"name": name, "state": "running", "started": job["started"]}

    # -- writes
    def r_write_review(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            return self.writer.review(body or {})
        except WriteError as e:
            raise _bad(str(e)) from e

    def r_write_confirm(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            return self.writer.confirm(body or {})
        except WriteError as e:
            raise _bad(str(e)) from e

    def r_write_commit(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            out = self.writer.commit(body or {})
        except WriteError as e:
            raise _bad(str(e)) from e
        self.store.cache_del_prefix("lt:")
        for p in ("tasks:", "tree:", "search:", "catalog:", "person:"):
            self.store.cache_del_prefix(p)
        return out


# ---------------------------------------------------------------------- writes
class LedgerTabWriter:
    """Builds, reviews and runs allow-listed ledger writes for the Ledger tab."""

    # action -> (label, destructive)
    WRITES: ClassVar[dict[str, tuple[str, bool]]] = {
        "task_add": ("Add a task", False),
        "task_update": ("Update a task", False),
        "task_delete": ("Delete a task", True),
        "log": ("Log an interaction", False),
        "interaction_edit": ("Correct an interaction", False),
        "interaction_delete": ("Delete an interaction", True),
        "link": ("Link two records", False),
        "unlink": ("Unlink two records", True),
        "people_update": ("Update a person", False),
        "people_tag": ("Tag a person", False),
        "people_delete": ("Delete a person", True),
        "labs_add": ("Add a lab or unit", False),
        "labs_update": ("Update a lab or unit", False),
        "labs_delete": ("Delete a lab or unit", True),
        "projects_add": ("Add a project", False),
        "projects_delete": ("Delete a project", True),
        "projects_doc_add": ("Link a document to a project", False),
        "projects_doc_rm": ("Remove a document link", True),
        "gcp_add": ("Add a GCP project", False),
        "gcp_delete": ("Delete a GCP project", True),
        "grants_add": ("Add a grant", False),
        "grants_delete": ("Delete a grant", True),
        "assets_add": ("Add an asset", False),
        "assets_delete": ("Delete an asset", True),
    }
    BY_KIND: ClassVar[dict[str, tuple[str, ...]]] = {
        "people": ("people_update", "people_tag", "log", "link", "people_delete"),
        "labs": ("labs_update", "log", "link", "labs_delete"),
        "projects": ("projects_doc_add", "projects_doc_rm", "log", "link", "projects_delete"),
        "gcp": ("log", "link", "gcp_delete"),
        "grants": ("log", "link", "grants_delete"),
        "assets": ("log", "link", "assets_delete"),
        "tasks": ("task_update", "link", "task_delete"),
        "interactions": ("interaction_edit", "link", "interaction_delete"),
    }

    def __init__(
        self, ledger: Ledger, store: Any, run: Callable[..., tuple[int, str]] | None = None
    ):
        self.ledger = ledger
        self.store = store
        self._run_fn = run
        self.secret = secrets.token_bytes(32)
        self.lock = threading.Lock()
        self.pending: dict[str, dict[str, Any]] = {}

    def actions_for(self, kind: str) -> list[dict[str, Any]]:
        return [
            {"action": a, "label": self.WRITES[a][0], "destructive": self.WRITES[a][1]}
            for a in self.BY_KIND.get(kind, ())
        ]

    # -- build: request -> (argv, stdin, card, read_back)
    def _record(self, kind: str, key: str) -> dict[str, Any]:
        args = {
            "people": ["people", "show", key],
            "labs": ["labs", "show", key],
            "projects": ["projects", "show", key],
            "gcp": ["gcp", "show", key],
            "grants": ["grants", "show", key],
            "assets": ["assets", "show", key],
            "tasks": ["tasks", "show", key],
            "interactions": ["interactions", "show", key],
        }[kind]
        try:
            d = self.ledger._run(args)
        except LedgerError as e:
            raise WriteError(f"could not read the {kind} record: {e}") from e
        if not isinstance(d, dict) or not d:
            raise WriteError(f"no {kind} record for {key!r}")
        return d

    def _label(self, kind: str, rec: dict[str, Any]) -> str:
        for k in ("name", "summary", "project_id", "c_number", "title"):
            if rec.get(k):
                extra = f" ({rec['netid']})" if kind == "people" and rec.get("netid") else ""
                return f"{rec[k]}{extra}"[:160]
        return str(rec.get("id", ""))

    def build(self, b: dict[str, Any]) -> dict[str, Any]:
        a = str(b.get("action", ""))
        if a not in self.WRITES:
            raise WriteError("that ledger action is not available here")
        label, destructive = self.WRITES[a]
        stdin = ""
        lines: list[str] = []
        check: dict[str, Any] = {}

        if a == "task_add":
            summary = _short(b.get("summary"), "task text", 1000)
            if not summary:
                raise WriteError("task text is empty")
            pri = str(b.get("priority", "MEDIUM"))
            if pri not in PRIORITIES:
                raise WriteError("bad priority")
            argv = ["tasks", "add", "--priority", pri, "--json"]
            due = str(b.get("due") or "")
            if due:
                argv += ["--due", check_due(due)]
            argv += ["--", summary]
            lines = [f"Task: {summary}", f"Priority: {pri}", f"Due: {due or 'none'}"]
            check = {"kind": "task_add", "summary": summary}
        elif a == "task_update":
            tid = _uuid(b.get("id"), "task")
            rec = self._record("tasks", tid)
            argv = ["tasks", "update", tid]
            changes = []
            # Only real changes go on the command: a field set to what it already is
            # is dropped, so the card never shows "HIGH -> HIGH" as a write.
            if b.get("status") and b["status"] != rec.get("status"):
                if b["status"] not in STATUSES:
                    raise WriteError("bad status")
                argv += ["--status", b["status"]]
                changes.append(f"status {rec.get('status')} -> {b['status']}")
            if b.get("priority") and b["priority"] != rec.get("priority"):
                if b["priority"] not in PRIORITIES:
                    raise WriteError("bad priority")
                argv += ["--priority", b["priority"]]
                changes.append(f"priority {rec.get('priority')} -> {b['priority']}")
            old_due = str(rec.get("due_date") or "")[:10]
            if b.get("clear_due"):
                if old_due:
                    argv.append("--clear-due")
                    changes.append(f"due {old_due} -> none")
            elif b.get("due"):
                d = check_due(str(b["due"]))
                if d != old_due:
                    argv += ["--due", d]
                    changes.append(f"due {old_due or 'none'} -> {d}")
            if not changes:
                raise WriteError("nothing to change")
            lines = [f"Task: {_oneline(rec.get('summary'))}", *changes]
            check = {
                "kind": "task_update",
                "id": tid,
                "status": b.get("status") if "--status" in argv else None,
                "priority": b.get("priority") if "--priority" in argv else None,
                "due": ""
                if "--clear-due" in argv
                else (argv[argv.index("--due") + 1] if "--due" in argv else None),
            }
        elif a == "task_delete":
            tid = _uuid(b.get("id"), "task")
            rec = self._record("tasks", tid)
            argv = ["tasks", "delete", tid, "--force"]
            lines = [
                f"Delete task: {_oneline(rec.get('summary'))}",
                f"Status: {rec.get('status')}",
                "Its links go with it.",
            ]
            check = {"kind": "gone", "args": ["tasks", "show", tid]}
        elif a == "log":
            text = _text(b.get("text"), "log text")
            date = str(b.get("date") or "").strip()
            if not DATE_RE.match(date):
                raise WriteError("date must be YYYY-MM-DD or YYYY-MM-DD HH:MM")
            links = [_uuid(x, "link") for x in dict.fromkeys(b.get("links") or [])][:20]
            argv = ["log", "--date", date]
            for u in links:
                argv += ["--link", u]
            argv += ["--strict-links", "--yes", "-"]
            stdin = text + "\n"
            names = [str(n) for n in (b.get("link_names") or [])][: len(links)]
            lines = [
                f"Date: {date}",
                f"Links: {', '.join(names) or ', '.join(x[:8] for x in links) or 'none'}",
                f"Text: {text[:600]}{'...' if len(text) > 600 else ''}",
            ]
            check = {"kind": "log"}
        elif a == "interaction_edit":
            iid = _uuid(b.get("id"), "interaction")
            rec = self._record("interactions", iid)
            argv = ["interactions", "edit", iid, "--force"]
            changes = []
            if b.get("summary"):
                s = _text(b["summary"], "summary")
                argv += ["--summary", s]
                changes.append(f"summary -> {s[:300]}")
            if b.get("date"):
                d = str(b["date"]).strip()
                if not DATE_RE.match(d):
                    raise WriteError("date must be YYYY-MM-DD or YYYY-MM-DD HH:MM")
                argv += ["--date", d]
                changes.append(f"date {str(rec.get('date'))[:16]} -> {d}")
            note = _short(b.get("note"), "note", 500)
            if not note:
                raise WriteError("say why you are correcting it (the note is kept with the record)")
            argv += ["--note", note]
            if not changes:
                changes.append("note only")
            lines = [
                f"Interaction {iid[:8]} ({str(rec.get('date'))[:10]})",
                *changes,
                f"Why: {note}",
                "The old text is kept under details.superseded.",
            ]
            check = {"kind": "edit", "id": iid, "summary": b.get("summary")}
        elif a == "interaction_delete":
            iid = _uuid(b.get("id"), "interaction")
            rec = self._record("interactions", iid)
            argv = ["interactions", "delete", iid, "--force"]
            lines = [
                f"Delete interaction {iid[:8]} ({str(rec.get('date'))[:10]})",
                f"Text: {str(rec.get('summary'))[:300]}",
                "This destroys the record and every link to it. Correct is usually better.",
            ]
            check = {"kind": "gone", "args": ["interactions", "show", iid]}
        elif a in ("link", "unlink"):
            s, t = _uuid(b.get("source"), "source"), _uuid(b.get("target"), "target")
            if s == t:
                raise WriteError("source and target are the same record")
            sn, tn = _short(b.get("source_name"), "name"), _short(b.get("target_name"), "name")
            if a == "link":
                ctype = str(b.get("type", "LINKS_TO"))
                if ctype not in CONN_TYPES:
                    raise WriteError("bad link type")
                argv = ["link", s, t, "--type", ctype]
                role = _short(b.get("role"), "role", 80)
                if role:
                    argv += ["--role", role]
                lines = [
                    f"{sn or s[:8]}  --{ctype}-->  {tn or t[:8]}",
                    f"Role: {role or '(default)'}",
                ]
            else:
                argv = ["unlink", s, t, "--force"]
                lines = [f"Remove every link between {sn or s[:8]} and {tn or t[:8]}"]
            check = {"kind": a, "source": s, "target": t}
        elif a in ("people_update", "labs_update"):
            kind = a.split("_")[0]
            key = str(b.get("key", "")).strip()
            if kind == "people" and not NETID_RE.match(key):
                raise WriteError("netid needed")
            key = key if kind == "people" else _key(key, "lab name")
            rec = self._record(kind, key)
            sets = b.get("set") or {}
            if not isinstance(sets, dict) or not sets or len(sets) > 10:
                raise WriteError("give 1 to 10 fields to set")
            argv = [kind, "update", key]
            lines = [f"{'Person' if kind == 'people' else 'Unit'}: {self._label(kind, rec)}"]
            for k, v in sets.items():
                if not SET_KEY_RE.match(str(k)) or k in SET_FORBIDDEN:
                    raise WriteError(f"field {k!r} cannot be set here")
                val = _short(v, k, 300)
                if not val or "=" in str(k):
                    raise WriteError(f"value for {k} is empty")
                argv += ["--set", f"{k}={val}"]
                old = rec.get(k, (rec.get("details") or {}).get(k))
                lines.append(f"{k}: {old if old not in (None, '') else '(none)'} -> {val}")
            check = {"kind": "fields", "args": [kind, "show", key], "set": dict(sets)}
        elif a == "people_tag":
            key = str(b.get("key", "")).strip()
            if not NETID_RE.match(key):
                raise WriteError("netid needed")
            tag = str(b.get("tag", "")).strip().lstrip("#")
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,30}", tag):
                raise WriteError("tag: letters, digits, - and _ only")
            rec = self._record("people", key)
            argv = ["people", "tag", key, tag]
            lines = [f"Person: {self._label('people', rec)}", f"Add tag: #{tag}"]
            check = {"kind": "tag", "args": ["people", "show", key], "tag": tag}
        elif a == "labs_add":
            name = _key(b.get("name"), "name")
            desc = _short(b.get("description"), "description", 500)
            argv = (
                ["labs", "add", "--", name]
                if not desc
                else ["labs", "add", "--description", desc, "--", name]
            )
            lines = [f"Unit: {name}", f"Description: {desc or '-'}"]
            check = {"kind": "exists", "args": ["labs", "show", name]}
        elif a == "projects_add":
            name = _key(b.get("name"), "name")
            summary = _short(b.get("summary"), "summary", 500)
            if not summary:
                raise WriteError("a project needs a one-line summary")
            argv = ["projects", "add", "--summary", summary, "--", name]
            lines = [f"Project: {name}", f"Summary: {summary or '-'}"]
            check = {"kind": "exists", "args": ["projects", "show", name]}
        elif a == "projects_doc_add":
            proj = _key(b.get("key"), "project")
            url = str(b.get("url", "")).strip()
            if not URL_RE.match(url) or len(url) > 500:
                raise WriteError("document link must be an https:// URL")
            title = _short(b.get("title"), "title", 160)
            if not title:
                raise WriteError("title needed")
            self._record("projects", proj)
            argv = ["projects", "docs", "add", proj, url, "--title", title]
            lines = [f"Project: {proj}", f"Document: {title}", f"URL: {url}"]
            check = {"kind": "doc", "args": ["projects", "show", proj], "url": url, "present": True}
        elif a == "projects_doc_rm":
            proj = _key(b.get("key"), "project")
            url = str(b.get("url", "")).strip()
            rec = self._record("projects", proj)
            docs = [
                d for d in (rec.get("details") or {}).get("documents", []) if d.get("url") == url
            ]
            if len(docs) != 1:
                raise WriteError("pick one linked document (exact URL)")
            argv = ["projects", "docs", "rm", proj, url]
            lines = [f"Project: {proj}", f"Remove document: {docs[0].get('title')}", f"URL: {url}"]
            check = {
                "kind": "doc",
                "args": ["projects", "show", proj],
                "url": url,
                "present": False,
            }
        elif a == "gcp_add":
            pid = str(b.get("project_id", "")).strip()
            if not re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", pid):
                raise WriteError("GCP project ids are 6-30 lowercase letters, digits and hyphens")
            name = _short(b.get("name"), "name", 160)
            argv = ["gcp", "add", pid] + (["--name", name] if name else [])
            lines = [f"GCP project: {pid}", f"Name: {name or '-'}"]
            check = {"kind": "exists", "args": ["gcp", "show", pid]}
        elif a == "grants_add":
            c = _key(b.get("c_number"), "grant number")
            title = _key(b.get("title"), "grant title")
            agency = _short(b.get("agency"), "agency", 100)
            amount = str(b.get("amount") or "").strip()
            if amount and not re.fullmatch(r"\d{1,12}", amount):
                raise WriteError("amount: whole dollars, digits only")
            argv = (
                ["grants", "add"]
                + (["--agency", agency] if agency else [])
                + (["--amount", amount] if amount else [])
                + ["--", c, title]
            )
            lines = [
                f"Grant: {c}",
                f"Title: {title}",
                f"Agency: {agency or '-'}",
                f"Amount: {amount or '-'}",
            ]
            check = {"kind": "exists", "args": ["grants", "show", c]}
        elif a == "assets_add":
            name = _key(b.get("name"), "asset name")
            atype = str(b.get("type", "OTHER"))
            if atype not in ("GPU", "SERVER", "WORKSTATION", "STORAGE", "LICENSE", "OTHER"):
                raise WriteError("bad asset type")
            loc = _short(b.get("location"), "location", 160)
            argv = (
                ["assets", "add", "--type", atype]
                + (["--location", loc] if loc else [])
                + ["--", name]
            )
            lines = [f"Asset: {name}", f"Type: {atype}", f"Location: {loc or '-'}"]
            check = {"kind": "exists", "args": ["assets", "show", name]}
        elif a.endswith("_delete"):
            kind = a.split("_")[0]
            key = str(b.get("key", "")).strip()
            key = key if (kind == "people" and NETID_RE.match(key)) else _key(key, f"{kind} key")
            rec = self._record(kind, key)
            argv = [kind, "delete", key, "--force"]
            lines = [
                f"Delete {kind[:-1] if kind.endswith('s') else kind}: {self._label(kind, rec)}",
                "Every link to it is removed too (the ledger has no undo).",
            ]
            check = {"kind": "gone", "args": [kind, "show", key]}
        else:  # pragma: no cover - WRITES and this chain must stay in step
            raise WriteError("that ledger action is not wired")
        return {
            "action": a,
            "label": label,
            "destructive": destructive,
            "argv": argv,
            "stdin": stdin,
            "lines": [_ascii(x) for x in lines],
            "check": check,
        }

    # -- tokens
    def _sign(self, kind: str, payload: str) -> str:
        return hmac.new(self.secret, f"{kind}|{payload}".encode(), hashlib.sha256).hexdigest()

    def review(self, b: dict[str, Any]) -> dict[str, Any]:
        built = self.build(b)
        tok = secrets.token_urlsafe(18)
        body = json.dumps([built["argv"], built["stdin"]])
        with self.lock:
            now = time.time()
            self.pending = {k: v for k, v in self.pending.items() if v["exp"] > now}
            if len(self.pending) > 50:
                raise WriteError("too many pending ledger reviews; commit or wait a few minutes")
            self.pending[tok] = {
                **built,
                "exp": now + TOKEN_TTL,
                "sig": self._sign("argv", body),
                "confirmed": not built["destructive"],
            }
        card = {k: built[k] for k in ("action", "label", "destructive", "lines")}
        card["command"] = (
            "nexus "
            + " ".join(x if " " not in x else f'"{x}"' for x in built["argv"][:12])
            + (" ..." if len(built["argv"]) > 12 else "")
        )
        out = {"token": tok, "card": card, "expires_in": TOKEN_TTL}
        if built["destructive"]:
            out["confirm_1"] = self._sign("c1", tok)
        return out

    def confirm(self, b: dict[str, Any]) -> dict[str, Any]:
        """Second click on a destructive card: confirm_1 -> confirm_2."""
        tok, c1 = str(b.get("token", "")), str(b.get("confirm_1", ""))
        with self.lock:
            p = self.pending.get(tok)
            if not p or p["exp"] < time.time() or not p["destructive"]:
                raise WriteError("review expired or not destructive; review again")
            if not hmac.compare_digest(c1, self._sign("c1", tok)) or p.get("c1_used"):
                raise WriteError("first confirmation missing or already used")
            p["c1_used"] = True
            p["c2"] = secrets.token_urlsafe(18)
        return {"confirm_2": p["c2"]}

    def commit(self, b: dict[str, Any]) -> dict[str, Any]:
        tok = str(b.get("token", ""))
        with self.lock:
            p = self.pending.pop(tok, None)  # single use, even if the checks below fail
        if not p or p["exp"] < time.time():
            raise WriteError("review expired or already used; review again")
        body = json.dumps([p["argv"], p["stdin"]])
        if not hmac.compare_digest(p["sig"], self._sign("argv", body)):
            raise WriteError("review does not match; review again")
        if p["destructive"]:
            c2 = str(b.get("confirm_2", ""))
            if not p.get("c2") or not hmac.compare_digest(c2, p["c2"]):
                raise WriteError("a destructive change needs both confirmations; review again")
        rc, out = self._run(p["argv"], p["stdin"])
        result = self._read_back(p, rc, out)
        self.store.journal(
            f"ledger_tab_{p['action']}",
            result.get("id") or "-",
            bool(result["ok"]),
            {"rc": rc, "argv0": p["argv"][:2]},
        )
        return {
            "ok": result["ok"],
            "result": result,
            "card": {k: p[k] for k in ("action", "label", "lines")},
        }

    def _run(self, argv: list[str], stdin: str) -> tuple[int, str]:
        if self._run_fn is not None:
            return self._run_fn(argv, stdin)
        mcp = getattr(self.ledger, "mcp", None)
        if mcp is not None:  # v1.6: hosted server first; deletes stay on the CLI
            try:
                rc, out = ledger_mcp_write.run(mcp, argv, stdin, self.ledger.my_id)
                return rc, _ascii(out)
            except (ledger_mcp_write.NotMapped, ledger_mcp_write.Refused) as e:
                if not getattr(self.ledger, "has_cli", True):
                    return 1, f"ledger MCP: {e}"
        serve = self.ledger.serve
        if serve.available():
            try:
                rc, out, err = serve.run(argv, stdin=stdin, timeout=300)
                return rc, _ascii(out + "\n" + err)
            except ServeUnavailable:
                pass
            except ServeError as e:  # may have run: never re-run on the CLI
                serve.mark_down(str(e))
                return 1, f"ledger serve: {e}"
        import subprocess

        r = subprocess.run(
            [self.ledger.binary, *argv],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
            env={**__import__("os").environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "4000"},
        )
        return r.returncode, _ascii((r.stdout or "") + "\n" + (r.stderr or ""))

    def _show(self, args: list[str]) -> Any:
        try:
            return self.ledger._run(args)
        except LedgerError:
            return None

    def _read_back(self, p: dict[str, Any], rc: int, out: str) -> dict[str, Any]:
        c = p["check"]
        tail = "\n".join(ln for ln in out.splitlines() if ln.strip())[-1500:]
        res: dict[str, Any] = {"rc": rc, "output_tail": tail, "ok": False, "read_back": ""}
        k = c.get("kind")
        if k == "task_add":
            for line in reversed(out.splitlines()):
                if line.strip().startswith("{") and '"id"' in line:
                    try:
                        j = json.loads(line)
                    except ValueError:
                        continue
                    t = self._show(["tasks", "show", str(j.get("id"))])
                    res.update(
                        ok=bool(t),
                        id=j.get("id"),
                        read_back=f"task {str(j.get('id'))[:8]} exists"
                        if t
                        else "not found after add",
                    )
                    break
        elif k == "task_update":
            t = self._show(["tasks", "show", c["id"]]) or {}
            bad = [f for f in ("status", "priority") if c.get(f) and t.get(f) != c[f]]
            if (
                c.get("due") is not None
                and c.get("due") != ""
                and str(t.get("due_date") or "")[:10] != str(c["due"])[:10]
            ):
                bad.append("due")
            if c.get("due") == "" and t.get("due_date"):
                bad.append("due (still set)")
            res.update(
                ok=rc == 0 and bool(t) and not bad,
                id=c["id"],
                read_back=(
                    f"status {t.get('status')}, priority {t.get('priority')}, "
                    f"due {str(t.get('due_date') or 'none')[:10]}"
                )
                + (f"; did not change: {', '.join(bad)}" if bad else ""),
            )
        elif k == "log":
            m = re.search(r"Logged \(ID: ([0-9a-f-]{36})\)", out)
            iid = m.group(1) if m else None
            rec = self._show(["interactions", "show", iid]) if iid else None
            res.update(
                ok=bool(rec) and rc == 0,
                id=iid,
                read_back=(
                    f"interaction {str(iid)[:8]} with {len((rec or {}).get('links') or [])} links"
                )
                if rec
                else "no interaction id in the output"
                + (" (one may exist: check before logging again)" if rc else ""),
            )
        elif k == "edit":
            rec = self._show(["interactions", "show", c["id"]]) or {}
            ok = (
                rc == 0
                and bool(rec)
                and (
                    not c.get("summary")
                    or str(rec.get("summary", "")).startswith(
                        str(c["summary"])[:40].replace("[", "(").replace("]", ")")
                    )
                )
            )
            res.update(ok=ok, id=c["id"], read_back=f"summary now: {str(rec.get('summary'))[:200]}")
        elif k in ("link", "unlink"):
            tree = self._show(["tree", c["source"]]) or {}
            linked = any(x.get("id") == c["target"] for x in tree.get("connections") or [])
            want = k == "link"
            res.update(
                ok=rc == 0 and linked == want,
                id=f"{c['source']}->{c['target']}",
                read_back=("linked" if linked else "not linked") + " now",
            )
        elif k == "exists":
            rec = self._show(c["args"])
            res.update(
                ok=bool(rec),
                id=(rec or {}).get("id"),
                read_back="record exists" if rec else "not found after add",
            )
        elif k == "gone":
            rec = self._show(c["args"])
            res.update(
                ok=rc == 0 and not rec,
                read_back="record is gone" if not rec else "record still exists",
            )
        elif k == "fields":
            rec = self._show(c["args"]) or {}
            vals = {**(rec.get("details") or {}), **rec}
            bad = [
                f
                for f, v in c["set"].items()
                if str(vals.get(f)) != str(v).replace("[", "(").replace("]", ")")
            ]
            res.update(
                ok=rc == 0 and bool(rec) and not bad,
                id=rec.get("id"),
                read_back="all fields read back"
                if not bad
                else f"did not read back: {', '.join(bad)}",
            )
        elif k == "tag":
            rec = self._show(c["args"]) or {}
            tags = (rec.get("details") or {}).get("tags") or []
            res.update(
                ok=c["tag"] in tags,
                id=rec.get("id"),
                read_back=f"tags: {', '.join(tags) or 'none'}",
            )
        elif k == "doc":
            rec = self._show(c["args"]) or {}
            urls = [d.get("url") for d in (rec.get("details") or {}).get("documents", [])]
            present = c["url"] in urls
            res.update(
                ok=rc == 0 and present == c["present"],
                id=rec.get("id"),
                read_back="document linked" if present else "document not linked",
            )
        return res
