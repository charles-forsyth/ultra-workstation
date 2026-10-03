"""Ledger reads through the hosted ledger MCP server (SPEC 8.4, 8.8; v1.4).

Every ledger read in Ultra goes through `Ledger._run(argv)` and expects the JSON the
`nexus <argv> --json` CLI prints. This module answers the same argv from the hosted
MCP server and returns the same shapes, so the context rail, item context, Ledger tab,
Graph, Day and Board do not change. `run(client, argv, my_id)` returns:

- the CLI-shaped JSON on success;
- None when the record does not exist (the CLI prints "... not found" and exits 0);
- raises `NotMapped` for a read with no MCP tool (`doctor`, `gcp audit-report`): the
  caller uses `nexus serve` or the CLI for those;
- raises the client's McpError subclasses on transport or auth failures (the caller
  falls back to serve/CLI, which is safe because these are reads).

Shape notes (checked against the live server, 2026-10-03):
- Search rows: `{id, type, name, score, reason}`; `name` is the CLI's plain name
  ("Ada Lovelace (alovelace)" for people, task summary, grant title, ...).
- Tree edges: `{type, role, id, entity_type, name}`. The MCP brief carries no edge role
  (`role` is None) and display text is cut at 200 characters. Interactions are ordered
  oldest first by date, as the CLI lists them.
- Dossier: built from the person's full tree (labs, projects, GCP projects, grants,
  assets, tasks, every linked interaction with its date). Lab-owned assets that are not
  linked to the person, and the person's cached cloud scan, are not included.
- Interaction show: the record from `nexus_interactions_show` plus `links` from its
  tree (connection types; `direction` unknown).
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ultra.mcpclient import McpClient, McpToolError

MAX_EDGES = 5000
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NOT_FOUND = re.compile(r"\b(no|not found|unknown)\b.*", re.I)
LIST_TOOLS = {
    "labs": "nexus_labs_list",
    "gcp": "nexus_gcp_list",
    "projects": "nexus_projects_list",
    "grants": "nexus_grants_list",
    "assets": "nexus_assets_list",
}
SHOW_TOOLS = {  # kind -> (tool, argument name, key of the record in the answer)
    "labs": ("nexus_labs_show", "name", "unit"),
    "projects": ("nexus_projects_show", "name", "project"),
    "grants": ("nexus_grants_show", "c_number", "grant"),
    "assets": ("nexus_assets_show", "name", "asset"),
    "gcp": ("nexus_gcp_show", "project_id", "gcp_project"),
}
DOSSIER_TYPES = ("Researcher", "Lab", "GCPProject", "ResearchProject", "Task", "Asset", "Grant")


class NotMapped(Exception):
    """No MCP tool answers this read; use serve or the CLI."""


def _flags(args: list[str]) -> tuple[list[str], dict[str, str | bool]]:
    """Split argv into positionals and --flags (value or True)."""
    pos: list[str] = []
    flags: dict[str, str | bool] = {}
    VALUED = {"--limit", "--since", "--until", "--contains", "--sample", "--source"}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--json":
            pass
        elif a in VALUED and i + 1 < len(args):
            flags[a] = args[i + 1]
            i += 1
        elif a.startswith("--"):
            flags[a] = True
        else:
            pos.append(a)
        i += 1
    return pos, flags


def _clean(d: Any) -> Any:
    """Drop the server's bookkeeping keys (`_type`, `_score`, `_reason`)."""
    if isinstance(d, dict):
        return {k: v for k, v in d.items() if not k.startswith("_")}
    return d


def plain_name(row: dict[str, Any]) -> str:
    """The CLI's `get_plain_name` for a serialized entity or brief reference."""
    t = row.get("_type")
    if t == "Researcher":
        name = str(row.get("name") or row.get("display") or "")
        return f"{name} ({row['netid']})" if row.get("netid") else name
    if t == "GCPProject":
        return str(row.get("name") or row.get("project_id") or row.get("display") or "")
    if t in ("Task", "Interaction"):
        return str(row.get("summary") or row.get("display") or "")
    if t == "Grant":
        return str(row.get("title") or row.get("display") or "")
    return str(row.get("name") or row.get("display") or row.get("summary") or "")


def _not_found(e: McpToolError) -> bool:
    msg = str(e).split(":", 1)[-1]
    return bool(NOT_FOUND.search(msg)) and "must" not in msg


def _edges(tree: dict[str, Any]) -> list[dict[str, Any]]:
    """CLI tree connections from an MCP tree; interactions oldest first by date."""
    rows = []
    for e in tree.get("edges") or []:
        n = e.get("neighbor") or {}
        rows.append(
            {
                "type": (e.get("via") or {}).get("type"),
                "role": None,
                "id": n.get("id"),
                "entity_type": n.get("_type"),
                "name": plain_name(n),
                "date": n.get("date"),
            }
        )
    rows.sort(key=lambda r: (r["entity_type"] == "Interaction", str(r.get("date") or "")))
    for r in rows:
        r.pop("date", None)
    return rows


def _tree(client: McpClient, term: str) -> dict[str, Any] | None:
    try:
        t = client.call("nexus_tree", {"term": term, "max_edges": MAX_EDGES})
    except McpToolError as e:
        if _not_found(e):
            return None
        raise
    return t if isinstance(t, dict) and t.get("root") else None


def tree(client: McpClient, term: str) -> dict[str, Any] | None:
    t = _tree(client, term)
    if t is None:
        return None
    root = t["root"]
    return {
        "root": {"id": root.get("id"), "type": root.get("_type"), "name": plain_name(root)},
        "connections": _edges(t),
    }


def people_show(client: McpClient, netid: str) -> dict[str, Any] | None:
    """Exact NetID or email match only (the CLI's `people show <netid>`)."""
    rows = client.call("nexus_search", {"term": netid, "limit": 3}) or []
    for r in rows if isinstance(rows, list) else []:
        if r.get("_type") != "Researcher":
            continue
        if str(r.get("netid") or "").lower() == netid.lower():
            return dict(_clean(r))
    return None


def search(client: McpClient, term: str, limit: int) -> list[dict[str, Any]]:
    rows = client.call("nexus_search", {"term": term, "limit": limit}) or []
    return [
        {
            "id": r.get("id"),
            "type": r.get("_type"),
            "name": plain_name(r),
            "score": r.get("_score"),
            "reason": r.get("_reason"),
        }
        for r in (rows if isinstance(rows, list) else [])
        if isinstance(r, dict)
    ]


def tasks_list(client: McpClient, my_id: str, everyone: bool, all_states: bool) -> list:
    args: dict[str, Any] = {"limit": MAX_EDGES}
    if not all_states:
        args["status"] = "OPEN"
    rows = client.call("nexus_tasks_list", args) or []
    out = []
    for r in rows if isinstance(rows, list) else []:
        owner = (r.get("assigned_to") or {}).get("netid") if r.get("assigned_to") else None
        if not everyone and my_id and owner != my_id:
            continue
        out.append(
            {
                "id": r.get("id"),
                "summary": r.get("summary"),
                "status": r.get("status"),
                "priority": r.get("priority"),
                "due_date": r.get("due_date"),
                "assigned_to": owner,
            }
        )
    return out


def tasks_show(client: McpClient, tid: str) -> dict[str, Any] | None:
    try:
        d = client.call("nexus_tasks_show", {"task_id": tid})
    except McpToolError as e:
        if _not_found(e):
            return None
        raise
    if not isinstance(d, dict) or not d.get("task"):
        return None
    out = dict(_clean(d["task"]))
    proj = next(
        (
            x["entity"]
            for x in d.get("links") or []
            if (x.get("entity") or {}).get("_type") == "ResearchProject"
        ),
        None,
    )
    if proj:
        out["project"] = plain_name(proj)
    if d.get("assigned_to"):
        out["assigned_to"] = d["assigned_to"].get("netid")
    return out


def interactions_show(client: McpClient, iid: str) -> dict[str, Any] | None:
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_rec = ex.submit(client.call, "nexus_interactions_show", {"interaction_id": iid})
        f_tree = ex.submit(_tree, client, iid)
        try:
            d = f_rec.result()
        except McpToolError as e:
            if _not_found(e):
                return None
            raise
        t = f_tree.result()
    if not isinstance(d, dict) or not d.get("interaction"):
        return None
    out = dict(_clean(d["interaction"]))
    out["links"] = [
        {
            "id": c["id"],
            "name": c["name"],
            "entity_type": c["entity_type"],
            "connection_type": c["type"],
            "direction": "",
            "meta_data": {},
        }
        for c in (_edges(t) if t else [])
    ]
    return out


def interactions_list(client: McpClient, flags: dict[str, str | bool]) -> list:
    args: dict[str, Any] = {"limit": int(str(flags.get("--limit") or 50))}
    for f, k in (("--since", "since"), ("--until", "until"), ("--contains", "contains")):
        if isinstance(flags.get(f), str):
            args[k] = flags[f]
    if flags.get("--full-text"):
        args["full_text"] = True
    rows = client.call("nexus_interactions_list", args) or []
    return [
        {"id": r.get("id"), "date": r.get("date"), "summary": r.get("summary")}
        for r in (rows if isinstance(rows, list) else [])
    ]


def dossier(client: McpClient, netid: str) -> dict[str, Any] | None:
    t = _tree(client, netid)
    if t is None or (t.get("root") or {}).get("_type") != "Researcher":
        return None
    r = t["root"]
    conns = _edges(t)
    by = lambda et: [c for c in conns if c["entity_type"] == et]  # noqa: E731
    ent = lambda c: {"id": c["id"], "name": c["name"], "entity_type": c["entity_type"]}  # noqa: E731
    briefs = {str((e.get("neighbor") or {}).get("id")): e["neighbor"] for e in t.get("edges") or []}
    return {
        "researcher": {
            k: r.get(k) for k in ("id", "netid", "name", "title", "dept", "email_alias")
        },
        "labs": [ent(c) for c in by("Lab")],
        "assets": [ent(c) for c in by("Asset")],
        "grants": [ent(c) for c in by("Grant")],
        "projects": [
            {**ent(c), "role": "PI" if c["type"] == "PI_OF" else "Member"}
            for c in by("ResearchProject")
        ],
        "interactions": sorted(
            (
                {
                    "id": c["id"],
                    "date": (briefs.get(str(c["id"])) or {}).get("date"),
                    "summary": c["name"],
                }
                for c in by("Interaction")
            ),
            key=lambda i: str(i["date"] or ""),
            reverse=True,
        ),
        "connections": [
            {**ent(c), "connection_type": c["type"], "role": None}
            for c in conns
            if c["entity_type"] in DOSSIER_TYPES
        ],
        "external_state": [],
    }


def people_list(client: McpClient, limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    offset = 0
    while len(out) < limit:
        page = client.call(
            "nexus_people_list", {"offset": offset, "limit": min(5000, limit - len(out))}
        )
        rows = (page or {}).get("people") or [] if isinstance(page, dict) else []
        out.extend(rows)
        offset += len(rows)
        if not rows or offset >= int((page or {}).get("total") or 0):
            break
    return out


def entity_list(client: McpClient, kind: str, limit: int) -> list[dict[str, Any]]:
    rows = client.call(LIST_TOOLS[kind], {"limit": min(limit, MAX_EDGES)}) or []
    return [_clean(r) for r in (rows if isinstance(rows, list) else [])]


def entity_show(client: McpClient, kind: str, key: str) -> dict[str, Any] | None:
    tool, arg, field = SHOW_TOOLS[kind]
    try:
        d = client.call(tool, {arg: key, "max_connections": 0})
    except McpToolError as e:
        if _not_found(e):
            return None
        raise
    if not isinstance(d, dict) or not d.get(field):
        return None
    out = dict(_clean(d[field]))
    if kind == "gcp":
        out["external_state"] = d.get("external_state") or {}
    return out


def run(client: McpClient, args: list[str], my_id: str = "") -> Any:
    """Answer a CLI read argv from the MCP server, in the CLI's JSON shape."""
    pos, flags = _flags(args)
    if not pos:
        raise NotMapped("empty command")
    head, sub = pos[0], (pos[1] if len(pos) > 1 else "")
    rest = pos[2:] if head not in ("search", "tree", "dossier", "stats") else pos[1:]
    lim = int(str(flags.get("--limit") or 0) or 0)
    if head == "search" and rest:
        return search(client, rest[0], lim or 10)
    if head == "tree" and rest:
        return tree(client, rest[0])
    if head == "dossier" and rest:
        return dossier(client, rest[0])
    if head == "stats":
        return client.call("nexus_stats")
    if head == "people" and sub == "show" and rest:
        return people_show(client, rest[0])
    if head == "people" and sub == "list":
        return people_list(client, lim or 100)
    if head == "tasks" and sub == "list":
        return tasks_list(client, my_id, bool(flags.get("--global")), bool(flags.get("--all")))
    if head == "tasks" and sub == "show" and rest and UUID_RE.match(rest[0]):
        return tasks_show(client, rest[0])
    if head == "interactions" and sub == "list":
        return interactions_list(client, flags)
    if head == "interactions" and sub == "show" and rest:
        return interactions_show(client, rest[0])
    if head in LIST_TOOLS and sub == "list":
        return entity_list(client, head, MAX_EDGES if flags.get("--all") else (lim or 100))
    if head in SHOW_TOOLS and sub == "show" and rest:
        return entity_show(client, head, rest[0])
    if head == "org" and sub == "show" and rest:
        return client.call(
            "nexus_org_show", {"unit_name": rest[0], "max_depth": 5, "max_members": 500}
        )
    if head == "ship" and sub == "status":
        return client.call("nexus_ship_status", {"limit": min(lim or 50, MAX_EDGES)})
    raise NotMapped(" ".join(pos[:2]))
