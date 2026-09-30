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

READ_COMMANDS = {  # the only subcommands this adapter will ever run
    ("people", "show"),
    ("search",),
    ("tree",),
    ("tasks", "list"),
    ("tasks", "show"),
    ("interactions", "list"),
    ("interactions", "show"),
    ("gcp", "show"),
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
