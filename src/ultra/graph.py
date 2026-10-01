"""Graph view (v1.2, SPEC 7.6): a ledger neighborhood as nodes and edges, read only.

Built from `nexus tree <id> --json` (the same read the Ledger tab uses). The center is
the operator (`[ledger] my_id`) unless a ledger id is given; one hop out, plus the
links between those neighbors when a second hop is asked for and is cheap.

Interactions and tasks are not drawn as nodes (2,700 interactions would bury
everything): each node carries how many of them link to it, and the center's counts
are in the header. Clicking a node opens it in the Ledger tab; dragging a stream item
onto a node adds both to the bucket, which builds a normal staged log card. The graph
itself never writes.
"""

from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from ultra.ledger import LedgerError

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NETID = re.compile(r"^[a-z][a-z0-9_.-]{1,31}$")
TTL = 600
NODE_TYPES = {
    "Researcher": "people",
    "Lab": "labs",
    "GCPProject": "gcp",
    "ResearchProject": "projects",
    "Grant": "grants",
    "Asset": "assets",
}
COUNTED = ("Interaction", "Task")
# which neighbors' trees to read first for the second hop
HOP_ORDER = {
    "Lab": 0,
    "ResearchProject": 1,
    "GCPProject": 2,
    "Researcher": 3,
    "Grant": 4,
    "Asset": 5,
}
MAX_NODES = 250
MAX_SECOND_HOP = 40  # neighbors whose own trees are read for neighbor-to-neighbor edges


def _bad(msg: str, status: int = 400) -> Exception:
    from ultra.server import ApiError

    return ApiError(status, msg)


def build_graph(
    center: dict[str, Any],
    conns: list[dict[str, Any]],
    second: dict[str, list[dict[str, Any]]] | None = None,
    hide: set[str] | None = None,
) -> dict[str, Any]:
    """Nodes and edges from a center and its tree connections. Pure.

    second: neighbor id -> that neighbor's own connections (for edges between
    neighbors and per-node interaction/task counts). hide: entity types left out.
    """
    hide = hide or set()
    cid = str(center.get("id") or "")
    nodes: dict[str, dict[str, Any]] = {
        cid: {
            "id": cid,
            "name": str(center.get("name") or ""),
            "type": str(center.get("type") or "Researcher"),
            "kind": NODE_TYPES.get(str(center.get("type") or ""), "people"),
            "center": True,
            "interactions": 0,
            "tasks": 0,
        }
    }
    edges: list[dict[str, Any]] = []
    counts = {"Interaction": 0, "Task": 0}
    for c in conns:
        et = str(c.get("entity_type") or "")
        if et in COUNTED:
            counts[et] += 1
            continue
        if et not in NODE_TYPES or et in hide:
            continue
        nid = str(c.get("id") or "")
        if not UUID.match(nid) or nid == cid:
            continue
        if nid not in nodes:
            if len(nodes) >= MAX_NODES:
                continue
            nodes[nid] = {
                "id": nid,
                "name": str(c.get("name") or ""),
                "type": et,
                "kind": NODE_TYPES[et],
                "center": False,
                "interactions": 0,
                "tasks": 0,
            }
        edges.append({"a": cid, "b": nid, "type": str(c.get("type") or ""), "role": c.get("role")})
    nodes[cid]["interactions"] = counts["Interaction"]
    nodes[cid]["tasks"] = counts["Task"]
    seen = {(e["a"], e["b"]) for e in edges}
    for nid, sub in (second or {}).items():
        if nid not in nodes:
            continue
        for c in sub:
            et = str(c.get("entity_type") or "")
            if et == "Interaction":
                nodes[nid]["interactions"] += 1
                continue
            if et == "Task":
                nodes[nid]["tasks"] += 1
                continue
            oid = str(c.get("id") or "")
            if oid in nodes and oid != nid and oid != cid:
                key = (min(nid, oid), max(nid, oid))
                if key not in seen:
                    seen.add(key)
                    edges.append(
                        {
                            "a": nid,
                            "b": oid,
                            "type": str(c.get("type") or ""),
                            "role": c.get("role"),
                        }
                    )
    by_type: dict[str, int] = {}
    for n in nodes.values():
        if not n["center"]:
            by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    return {
        "center": nodes[cid],
        "nodes": list(nodes.values()),
        "edges": edges,
        "by_type": by_type,
        "interactions": counts["Interaction"],
        "tasks": counts["Task"],
        "truncated": len(nodes) >= MAX_NODES,
    }


class Graph:
    def __init__(self, ledger: Any, store: Any, my_id: str = "") -> None:
        self.ledger = ledger
        self.store = store
        self.my_id = my_id
        self.lock = threading.Lock()
        self.warming: set[str] = set()  # centers whose neighbor trees are being read

    def register(self, api: Any) -> None:
        api.add("GET", r"/api/graph", self.r_graph)

    def _cached_tree(self, ident: str) -> dict[str, Any] | None:
        hit = self.store.cache_get(f"graph:tree:{ident}", TTL)
        return hit[0] if hit is not None else None

    def _tree(self, ident: str, fresh: bool) -> dict[str, Any]:
        key = f"graph:tree:{ident}"
        if not fresh:
            cached = self._cached_tree(ident)
            if cached is not None:
                return cached
        try:
            d = self.ledger._run(["tree", ident], timeout=120)
        except LedgerError as e:
            raise _bad(str(e), 502) from e
        if not isinstance(d, dict):
            raise _bad("the ledger returned no tree", 502)
        self.store.cache_put(key, d)
        return d

    def r_graph(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        if not self.ledger.enabled:
            raise _bad("the ledger is not configured", 503)
        ident = str((q.get("id") or [""])[0]).strip().lower()
        if not ident:
            ident = self.my_id.strip().lower()
            if not ident:
                raise _bad(
                    "set [ledger] my_id in config.toml, or open the graph from a person or lab"
                )
        if not (UUID.match(ident) or NETID.match(ident)):
            raise _bad("id must be a ledger UUID or a NetID")
        fresh = (q.get("fresh") or [""])[0] == "1"
        hops = (q.get("hops") or ["2"])[0] == "2"
        hide = {t for t in (q.get("hide") or [""])[0].split(",") if t in NODE_TYPES}
        tree = self._tree(ident, fresh)
        center = tree.get("root") or {}
        conns = tree.get("connections") or []
        if not UUID.match(str(center.get("id") or "")):
            raise _bad("no ledger record for that id", 404)
        first = build_graph(center, conns, None, hide)
        nbrs = sorted(
            (n for n in first["nodes"] if not n["center"]),
            key=lambda n: HOP_ORDER.get(n["type"], 9),
        )[:MAX_SECOND_HOP]
        # Second hop (neighbor-to-neighbor links, per-node counts) costs one tree read
        # per neighbor: seconds each on the CLI. Use what is cached now; read the rest
        # in the background so the page can ask again (pending=true) without waiting.
        second: dict[str, list[dict[str, Any]]] = {}
        missing = []
        if hops:
            for n in nbrs:
                c = self._cached_tree(n["id"])
                if c is None:
                    missing.append(n)
                else:
                    second[n["id"]] = c.get("connections") or []
            if missing:
                self._warm(str(center["id"]), missing)
        g = build_graph(center, conns, second, hide)
        g["second_hop"] = len(second)
        g["pending"] = len(missing)
        return g

    def _warm(self, cid: str, nbrs: list[dict[str, Any]]) -> None:
        """Read neighbor trees into the cache in a background thread (once per center)."""
        with self.lock:
            if cid in self.warming:
                return
            self.warming.add(cid)

        def one(n: dict[str, Any]) -> None:
            try:
                self._tree(n["id"], False)
            except Exception:  # noqa: BLE001 - a neighbor that fails just has no extra edges
                self.store.cache_put(f"graph:tree:{n['id']}", {"root": {}, "connections": []})

        def run() -> None:
            try:
                with ThreadPoolExecutor(max_workers=3) as ex:  # the ledger has its own semaphore
                    list(ex.map(one, nbrs))
            finally:
                with self.lock:
                    self.warming.discard(cid)

        threading.Thread(target=run, daemon=True, name="graph-warm").start()
