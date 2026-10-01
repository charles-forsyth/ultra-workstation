"""v1.2 Graph: nodes/edges from ledger trees, counts not nodes, validation, read only."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest

from ultra.graph import MAX_NODES, Graph, build_graph
from ultra.ledger import LedgerError
from ultra.server import ApiError
from ultra.store import Store

ROOT = Path(__file__).resolve().parents[1]
M = re.match("x", "x")


def u(n: int) -> str:
    return f"aaaaaaaa-0000-4000-8000-{n:012d}"


ME = {"id": u(1), "type": "Researcher", "name": "Ada (ada)"}
LAB = {"id": u(2), "entity_type": "Lab", "name": "Lovelace Lab", "type": "MEMBER_OF", "role": None}
BEN = {
    "id": u(3),
    "entity_type": "Researcher",
    "name": "Ben (bcarter)",
    "type": "COLLABORATES_WITH",
    "role": None,
}
GCP = {"id": u(4), "entity_type": "GCPProject", "name": "ada-lab", "type": "OPERATES", "role": None}
ASSET = {"id": u(5), "entity_type": "Asset", "name": "vm-1", "type": "OPERATES", "role": None}
IX = [
    {"id": u(100 + i), "entity_type": "Interaction", "name": f"call {i}", "type": "PARTICIPATED_IN"}
    for i in range(7)
]
TASKS = [
    {"id": u(200 + i), "entity_type": "Task", "name": f"t{i}", "type": "ASSIGNED_TO"}
    for i in range(3)
]


def test_nodes_edges_and_counts():
    g = build_graph(ME, [LAB, BEN, GCP, ASSET, *IX, *TASKS])
    ids = {n["id"] for n in g["nodes"]}
    assert ids == {u(1), u(2), u(3), u(4), u(5)}  # interactions and tasks are not nodes
    assert g["interactions"] == 7 and g["tasks"] == 3
    assert g["center"]["interactions"] == 7 and g["center"]["center"] is True
    assert {(e["a"], e["b"], e["type"]) for e in g["edges"]} == {
        (u(1), u(2), "MEMBER_OF"),
        (u(1), u(3), "COLLABORATES_WITH"),
        (u(1), u(4), "OPERATES"),
        (u(1), u(5), "OPERATES"),
    }
    assert g["by_type"] == {"Lab": 1, "Researcher": 1, "GCPProject": 1, "Asset": 1}


def test_hide_types():
    g = build_graph(ME, [LAB, BEN, ASSET], hide={"Asset", "Researcher"})
    assert {n["id"] for n in g["nodes"]} == {u(1), u(2)}


def test_second_hop_edges_between_neighbors_and_counts():
    second = {
        u(2): [
            {"id": u(3), "entity_type": "Researcher", "type": "PI_OF"},  # Ben is PI of the lab
            {"id": u(1), "entity_type": "Researcher", "type": "MEMBER_OF"},  # back to center: skip
            {
                "id": u(999),
                "entity_type": "Researcher",
                "type": "MEMBER_OF",
            },  # not on the graph: skip
            *IX[:2],
        ],
        u(3): [
            {"id": u(2), "entity_type": "Lab", "type": "PI_OF"}
        ],  # same pair from the other side
    }
    g = build_graph(ME, [LAB, BEN], second)
    pairs = [(e["a"], e["b"]) for e in g["edges"]]
    assert pairs.count((u(2), u(3))) + pairs.count((u(3), u(2))) == 1  # once, not twice
    assert len(g["edges"]) == 3
    lab = next(n for n in g["nodes"] if n["id"] == u(2))
    assert lab["interactions"] == 2


def test_junk_ids_and_cap():
    junk = [
        {"id": "not-a-uuid", "entity_type": "Lab", "name": "x"},
        {"id": u(1), "entity_type": "Lab"},
    ]
    g = build_graph(ME, junk)
    assert len(g["nodes"]) == 1 and g["edges"] == []
    many = [
        {"id": u(1000 + i), "entity_type": "Asset", "name": f"a{i}", "type": "OPERATES"}
        for i in range(400)
    ]
    g = build_graph(ME, many)
    assert len(g["nodes"]) == MAX_NODES and g["truncated"] is True


class FakeLedger:
    enabled = True

    def __init__(self, trees: dict[str, dict[str, Any]]) -> None:
        self.trees = trees
        self.calls: list[list[str]] = []
        self.lock = threading.Lock()

    def _run(self, args: list[str], timeout: int = 120) -> Any:
        with self.lock:
            self.calls.append(list(args))
        assert args[0] == "tree"  # the graph only ever reads trees
        if args[1] not in self.trees:
            raise LedgerError("not found")
        return self.trees[args[1]]


@pytest.fixture
def graph(tmp_path):
    led = FakeLedger(
        {
            "ada": {"root": ME, "connections": [LAB, BEN, *IX]},
            u(1): {"root": ME, "connections": [LAB, BEN, *IX]},
            u(2): {
                "root": {"id": u(2), "type": "Lab", "name": "Lovelace Lab"},
                "connections": [{"id": u(3), "entity_type": "Researcher", "type": "PI_OF"}],
            },
            u(3): {"root": {"id": u(3), "type": "Researcher", "name": "Ben"}, "connections": []},
        }
    )
    return Graph(led, Store(tmp_path / "g.db"), my_id="ada")


def _settle(graph: Graph) -> None:
    import time

    for _ in range(200):
        with graph.lock:
            if not graph.warming:
                return
        time.sleep(0.01)
    raise AssertionError("background warm-up did not finish")


def test_first_answer_is_immediate_then_second_hop_fills_in(graph):
    g = graph.r_graph({"hops": ["2"]}, None, M)
    assert g["center"]["id"] == u(1) and g["second_hop"] == 0 and g["pending"] == 2
    assert not any({e["a"], e["b"]} == {u(2), u(3)} for e in g["edges"])  # not yet
    _settle(graph)
    g = graph.r_graph({"hops": ["2"]}, None, M)
    assert g["pending"] == 0 and g["second_hop"] == 2
    assert any({e["a"], e["b"]} == {u(2), u(3)} for e in g["edges"])  # lab-to-Ben link
    assert all(c[0] == "tree" for c in graph.ledger.calls)


def test_warm_up_runs_once_per_center(graph):
    graph.r_graph({}, None, M)
    graph.r_graph({}, None, M)  # while warming: no second batch
    _settle(graph)
    reads = [c[1] for c in graph.ledger.calls]
    assert reads.count(u(2)) == 1 and reads.count(u(3)) == 1


def test_failed_neighbor_does_not_retry_forever(graph):
    graph.ledger.trees.pop(u(3))  # Ben's tree read fails
    graph.r_graph({}, None, M)
    _settle(graph)
    g = graph.r_graph({}, None, M)
    assert g["pending"] == 0  # the failure is cached as "no links"


def test_route_caches_trees(graph):
    graph.r_graph({}, None, M)
    _settle(graph)
    n = len(graph.ledger.calls)
    graph.r_graph({}, None, M)
    assert len(graph.ledger.calls) == n  # second call served from cache
    graph.r_graph({"fresh": ["1"], "hops": ["1"]}, None, M)
    assert len(graph.ledger.calls) == n + 1  # fresh re-reads the center only


@pytest.mark.parametrize("ident", ["../x", "Robert'); DROP", "a" * 40, "--json"])
def test_route_validates_id(graph, ident):
    with pytest.raises(ApiError, match="UUID or a NetID"):
        graph.r_graph({"id": [ident]}, None, M)
    assert graph.ledger.calls == []


def test_route_needs_a_center(tmp_path):
    g = Graph(FakeLedger({}), Store(tmp_path / "g.db"), my_id="")
    with pytest.raises(ApiError, match="my_id"):
        g.r_graph({}, None, M)


def test_unknown_record_is_a_ledger_error(graph):
    with pytest.raises(ApiError) as e:
        graph.r_graph({"id": ["nobody"]}, None, M)
    assert e.value.status == 502  # ledger said not found


def test_frontend_never_uses_innerhtml_for_names():
    js = (ROOT / "src/ultra/static/graph.js").read_text()
    draw = js[js.index("function draw(") :]
    assert "innerHTML" not in draw  # node names reach the page via textContent only
    assert "label.textContent = shortName(n.name)" in draw


def test_graph_over_http_demo():
    import http.client

    from ultra.config import Config
    from ultra.server import build

    httpd, _api = build(Config({}), 0, demo=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("GET", "/api/graph?hops=2", headers={"Host": f"127.0.0.1:{port}"})
        r = c.getresponse()
        body = json.loads(r.read())
        assert r.status == 200 and body["center"]["name"].startswith("Ben Carter")
        assert len(body["nodes"]) >= 3 and body["edges"]
    finally:
        httpd.shutdown()
        httpd.server_close()
