"""Ledger adapter (the `nexus` CLI), read side for v0.2.

Only `--json` read commands, run as argv lists (no shell), at most N at once, cached
with TTLs. Each call takes several seconds against production, so the context rail
serves the cache immediately and refreshes behind it. Writes arrive in v0.4 and will
also go only through the CLI.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import threading
from typing import Any

from ultra.config import Config
from ultra.store import Store

TTL_PERSON = 86400
TTL_TREE = 1800
TTL_TASKS = 300
TTL_SEARCH = 600
TTL_FULL = 900  # full context: dossier + search + open tasks, cached 15 min

READ_COMMANDS = {  # the only subcommands this adapter will ever run
    ("people", "show"),
    ("search",),
    ("tree",),
    ("tasks", "list"),
    ("tasks", "show"),
    ("interactions", "list"),
    ("interactions", "show"),
    ("gcp", "show"),
    ("dossier",),
}


class LedgerError(Exception):
    pass


class Ledger:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.binary = str(cfg.get("ledger", "binary", "nexus"))
        self.enabled = bool(cfg.get("ledger", "enabled", True)) and bool(shutil.which(self.binary))
        self.sem = threading.Semaphore(int(cfg.get("ledger", "max_parallel", 3)))
        self.domain = str(cfg.get("ledger", "org_email_domain", "")).lower()
        self.netid_local = bool(cfg.get("ledger", "netid_from_local_part", False))
        self.state: dict[str, Any] = {"ok": None, "error": ""}
        self._inflight: dict[str, threading.Event] = {}
        self._il = threading.Lock()

    def _run(self, args: list[str], timeout: int = 120) -> Any:
        key = tuple(a for a in args[:2] if not a.startswith("-"))
        if key not in READ_COMMANDS and key[:1] not in READ_COMMANDS:
            raise LedgerError(f"not an allowed read command: {' '.join(args[:2])}")
        with self.sem:
            r = subprocess.run(
                [self.binary, *args, "--json"],
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        out = (r.stdout or "").strip()
        if not out.startswith(("{", "[")):
            # e.g. "Researcher x not found." on stdout with exit 0
            self.state = {"ok": True, "error": ""}
            return None
        try:
            data = json.loads(out)
        except ValueError as e:
            self.state = {"ok": False, "error": f"bad JSON from {args[0]}"}
            raise LedgerError(f"bad JSON from nexus {args[0]}") from e
        self.state = {"ok": True, "error": ""}
        return data

    def _cached(self, key: str, ttl: int, args: list[str]) -> Any:
        hit = self.store.cache_get(key, ttl)
        if hit is not None:
            return hit[0]
        data = self._run(args)
        self.store.cache_put(key, data)
        return data

    # ---------------------------------------------------------------- lookups
    def resolve(self, addr: str, name: str = "") -> dict[str, Any] | None:
        """Person record for an address, following SPEC 11.1. None if unknown."""
        addr = addr.lower()
        hit = self.store.cache_get(f"person:{addr}", TTL_PERSON)
        if hit is not None:
            return hit[0]
        person = None
        local, _, dom = addr.partition("@")
        if self.netid_local and dom == self.domain and re.fullmatch(r"[a-z0-9._-]+", local):
            p = self._run(["people", "show", local])
            if isinstance(p, dict) and p.get("netid"):
                person = p
        if person is None and name:
            res = self._run(["search", name]) or []
            people = [
                r
                for r in res
                if r.get("type") == "Researcher" and float(r.get("score") or 0) >= 0.85
            ]
            m = (
                re.search(r"\(([a-z0-9._-]+)\)\s*$", people[0]["name"])
                if len(people) == 1
                else None
            )
            if m:
                p = self._run(["people", "show", m.group(1)])
                if isinstance(p, dict) and p.get("netid"):
                    person = p
        if person is not None:
            alias = (person.get("email_alias") or "").lower()
            by_netid = dom == self.domain and local == person.get("netid")
            person["matched_by"] = "address" if addr == alias or by_netid else "name"
        self.store.cache_put(f"person:{addr}", person)
        return person

    def open_tasks(self) -> dict[str, dict[str, Any]]:
        tasks = self._cached("tasks:open", TTL_TASKS, ["tasks", "list"]) or []
        return {t["id"]: t for t in tasks if t.get("status") != "DONE"}

    def task(self, tid: str) -> dict[str, Any] | None:
        """One task with due date and details (vector dropped). Fresh read."""
        if not re.fullmatch(r"[0-9a-f-]{36}", tid):
            raise LedgerError("bad task id")
        d = self._run(["tasks", "show", tid])
        if not isinstance(d, dict):
            return None
        d.pop("vector", None)
        return d

    def open_task_ids(self) -> set[str]:
        return set(self.open_tasks())

    def search(self, term: str) -> list[dict[str, Any]]:
        term = " ".join(term.split())[:200]
        if not term or term.startswith("-"):
            return []
        res = self._cached(f"search:{term.lower()}", TTL_SEARCH, ["search", term]) or []
        return [r for r in res if isinstance(r, dict) and r.get("id")]

    def interaction(self, iid: str) -> dict[str, Any] | None:
        """Fresh read (no cache): used to read back a write."""
        if not re.fullmatch(r"[0-9a-f-]{36}", iid):
            raise LedgerError("bad interaction id")
        d = self._run(["interactions", "show", iid])
        return d if isinstance(d, dict) else None

    def tree_fresh(self, ident: str) -> dict[str, Any]:
        if not re.fullmatch(r"[0-9a-f-]{36}", ident):
            raise LedgerError("bad id")
        d = self._run(["tree", ident])
        return d if isinstance(d, dict) else {}

    def context(self, addr: str, name: str = "") -> dict[str, Any]:
        """Everything the context rail shows for one address."""
        if not self.enabled:
            return {"unavailable": True, "reason": "ledger CLI not found"}
        person = self.resolve(addr, name)
        if not person:
            return {"unresolved": True, "addr": addr}
        netid = person["netid"]
        # tree and the open-task list are independent: fetch them side by side
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=2) as ex:
            f_tree = ex.submit(self._cached, f"tree:{netid}", TTL_TREE, ["tree", netid])
            f_open = ex.submit(self.open_tasks)
            tree = f_tree.result() or {}
            open_map = f_open.result()
            open_ids = set(open_map)
        conns = tree.get("connections") or []
        by = lambda kind: [c for c in conns if c.get("entity_type") == kind]  # noqa: E731
        tasks = [t for t in reversed(by("Task")) if t["id"] in open_ids]
        ref = lambda c: {"id": c["id"], "name": c["name"], "edge": c.get("type")}  # noqa: E731
        return {
            "id": person.get("id"),
            "name": person.get("name"),
            "netid": netid,
            "title": person.get("title") or "",
            "dept": person.get("dept") or "",
            "email": person.get("email_alias") or "",
            "labs": [c["name"] for c in by("Lab")][:8],
            "projects": [c["name"] for c in by("GCPProject") + by("ResearchProject")][:8],
            "lab_refs": [ref(c) for c in by("Lab")][:8],
            "project_refs": [ref(c) for c in by("GCPProject") + by("ResearchProject")][:8],
            "open_tasks": [
                {
                    "id": t["id"],
                    "summary": t["name"],
                    "status": open_map.get(t["id"], {}).get("status", ""),
                    "priority": open_map.get(t["id"], {}).get("priority", ""),
                }
                for t in tasks
            ][:10],
            # tree lists edges oldest first; the newest five are at the end
            "interactions": [
                {"id": c["id"], "summary": c["name"]} for c in reversed(by("Interaction")[-5:])
            ],
            "interaction_count": len(by("Interaction")),
            "matched_by": person.get("matched_by", "address"),
        }

    # ---------------------------------------------------------------- full context
    def full_context(self, addr: str, name: str = "", fresh: bool = False) -> dict[str, Any]:
        """Everything the ledger knows that relates to one person (the Full context tab).

        Built only from read commands that already emit JSON (no ledger changes):
          1. `dossier <netid> --json`: labs, lab-owned assets, grants, projects, GCP
             projects, every interaction LINKED to them (full text), cloud audit state.
          2. `search "<name>" --json --limit 40`: interactions and tasks that MENTION
             them but were never linked (semantic + name match). Marked as unlinked.
          3. `tasks list --json`: open tasks, matched by link or by mention.
        The three run side by side (about 9 s together), then cache for 15 minutes.
        """
        if not self.enabled:
            return {"unavailable": True, "reason": "ledger CLI not found"}
        person = self.resolve(addr, name)
        if not person:
            return {"unresolved": True, "addr": addr}
        netid = str(person["netid"])
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", netid):
            raise LedgerError("unexpected netid")
        pname = re.sub(r"\s*\([^)]*\)\s*$", "", str(person.get("name") or "")).strip() or netid
        pname = pname.lstrip("-")[:120] or netid
        key = f"full:{netid}"
        if not fresh:
            hit = self.store.cache_get(key, TTL_FULL)
            if hit:
                return {**hit[0], "age": round(hit[1])}
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=3) as ex:
            f_dos = ex.submit(self._run, ["dossier", netid], 150)
            f_srch = ex.submit(self._run, ["search", "--limit", "40", pname], 150)
            f_tasks = ex.submit(self.open_tasks)
            dos = f_dos.result() or {}
            try:
                srch = f_srch.result() or []
            except LedgerError:
                srch = []
            open_map = f_tasks.result()
        if not isinstance(dos, dict) or "researcher" not in dos:
            raise LedgerError("dossier returned no researcher")
        linked_ix = [
            {
                "id": i["id"],
                "date": (i.get("date") or "")[:10],
                "summary": i.get("summary") or "",
                "linked": True,
            }
            for i in dos.get("interactions") or []
        ]
        linked_ids = {i["id"] for i in linked_ix}
        mentions = [
            {
                "id": r["id"],
                "date": "",
                "summary": r.get("name") or "",
                "linked": False,
                "reason": r.get("reason", ""),
            }
            for r in (srch if isinstance(srch, list) else [])
            if r.get("type") == "Interaction" and r.get("id") not in linked_ids
        ]
        conns = dos.get("connections") or []
        linked_task_ids = {c["id"] for c in conns if c.get("entity_type") == "Task"}
        search_task_ids = {
            r["id"] for r in (srch if isinstance(srch, list) else []) if r.get("type") == "Task"
        }
        # A task "mentions" them if it names them: full name, "First ... Last" with the
        # last name as a separate word (so "Carter-Smith" is not Carter), their lab
        # by last name ("Carter Lab"), or netid. A given name alone ("Ian") or a last
        # name alone matches other people too often. These are marked "mentions" so the
        # operator can tell them from linked ones.
        parts = [w for w in pname.split() if len(w) > 1]
        first, last = (parts[0], parts[-1]) if len(parts) > 1 else (pname, "")
        pats = [re.escape(pname), re.escape(netid)]
        if last:
            pats.append(rf"{re.escape(last)}(?!-)\s+lab")
            pats.append(rf"{re.escape(first)}\b.{{0,80}}\b{re.escape(last)}(?!-)")
        mention_re = re.compile(r"(?<![\w-])(" + "|".join(pats) + r")(?![\w-])", re.I)
        tasks = []
        for tid, t in open_map.items():
            text = str(t.get("summary") or "")
            why = (
                "linked"
                if tid in linked_task_ids
                else "search"
                if tid in search_task_ids
                else "mentions"
                if mention_re.search(text)
                else ""
            )
            if why:
                tasks.append(
                    {
                        "id": tid,
                        "summary": t.get("summary", ""),
                        "status": t.get("status", ""),
                        "priority": t.get("priority", ""),
                        "why": why,
                    }
                )
        prio = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
        tasks.sort(key=lambda x: (prio.get(str(x["priority"]), 9), x["summary"]))
        ext = []
        for s in dos.get("external_state") or []:
            m = re.search(r"'project_id': '([^']+)'", str(s))
            ts = re.search(r"'scan_time': '([^']+)'", str(s))
            if m:
                ext.append({"project": m.group(1), "scanned": (ts.group(1)[:10] if ts else "")})
        r = dos["researcher"]
        out = {
            "id": r.get("id"),
            "netid": netid,
            "name": r.get("name") or pname,
            "title": r.get("title") or "",
            "dept": r.get("dept") or "",
            "email": r.get("email_alias") or "",
            "labs": dos.get("labs") or [],
            "projects": dos.get("projects") or [],
            "gcp_projects": [c for c in conns if c.get("entity_type") == "GCPProject"],
            "grants": dos.get("grants") or [],
            "assets": dos.get("assets") or [],
            "connections": [c for c in conns if c.get("entity_type") in ("Researcher", "Lab")],
            "tasks": tasks,
            "interactions": linked_ix,
            "mentions": mentions,
            "cloud": ext,
            "counts": {"linked": len(linked_ix), "mentions": len(mentions), "tasks": len(tasks)},
            "matched_by": person.get("matched_by", "address"),
            "age": 0,
        }
        self.store.cache_put(key, out)
        return out

    def full_context_text(self, ctx: dict[str, Any], limit: int = 60_000) -> str:
        """Plain text of a full context, for the AI (drafting, summaries, audio)."""
        lines = [f"{ctx.get('name')} ({ctx.get('netid')}), {ctx.get('title')}, {ctx.get('dept')}"]
        for label, xs in (
            ("Labs", ctx.get("labs")),
            ("Projects", ctx.get("projects")),
            ("GCP projects", ctx.get("gcp_projects")),
            ("Grants", ctx.get("grants")),
            ("Assets", ctx.get("assets")),
        ):
            if xs:
                lines.append(f"{label}: " + "; ".join(str(x.get("name")) for x in xs))
        if ctx.get("tasks"):
            lines.append("Open tasks:")
            lines += [f"- [{t['priority']}/{t['status']}] {t['summary']}" for t in ctx["tasks"]]
        if ctx.get("interactions"):
            lines.append("Logged interactions (newest first):")
            lines += [f"- {i['date']}: {i['summary']}" for i in ctx["interactions"]]
        if ctx.get("mentions"):
            lines.append("Other logs that mention them (not linked):")
            lines += [f"- {i['summary']}" for i in ctx["mentions"]]
        return "\n".join(lines)[:limit]
