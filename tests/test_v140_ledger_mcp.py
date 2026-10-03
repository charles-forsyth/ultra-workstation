"""v1.4.0: ledger reads through the hosted ledger MCP server (SPEC 8.4, 8.8).

`ledger_mcp.run(client, argv)` must give the same JSON shapes the `nexus ... --json` CLI
prints for every read Ultra makes, so nothing above `Ledger._run` changes. A fake client
answers each tool with data shaped like the live server's answers (checked 2026-10-03).
"""

from __future__ import annotations

from typing import Any

import pytest

from ultra import ledger_mcp
from ultra.config import Config
from ultra.ledger import Ledger, LedgerError
from ultra.mcpclient import McpAuthNeeded, McpToolError, McpUnreachable
from ultra.store import Store

ADA = "aaaaaaaa-0000-0000-0000-000000000001"
LAB = "bbbbbbbb-0000-0000-0000-000000000002"
GCP = "cccccccc-0000-0000-0000-000000000003"
TSK = "dddddddd-0000-0000-0000-000000000004"
IX1 = "eeeeeeee-0000-0000-0000-000000000005"
IX2 = "ffffffff-0000-0000-0000-000000000006"

ADA_REC = {
    "name": "Ada Lovelace",
    "title": "Professor",
    "id": ADA,
    "dept": "Math",
    "netid": "alovelace",
    "email_alias": "ada.lovelace@example.org",
    "admin_role": "NONE",
    "details": {"type": "Researcher"},
    "_type": "Researcher",
}


def edge(t: str, n: dict[str, Any]) -> dict[str, Any]:
    return {"via": {"type": t, "weight": 1}, "neighbor": n}


ADA_TREE = {
    "root": ADA_REC,
    "total_edges": 5,
    "edges_by_type": {"PI_OF": 2, "PARTICIPATED_IN": 2, "REFERENCED_IN": 1},
    "shown_edges": 5,
    "edges": [
        edge(
            "PARTICIPATED_IN",
            {
                "id": IX2,
                "_type": "Interaction",
                "display": "Newer call",
                "date": "2026-10-02T10:00:00",
            },
        ),
        edge("PI_OF", {"id": LAB, "_type": "Lab", "display": "Lovelace Lab (alovelace)"}),
        edge("PI_OF", {"id": GCP, "_type": "GCPProject", "display": "ada-lab"}),
        edge(
            "PARTICIPATED_IN",
            {
                "id": IX1,
                "_type": "Interaction",
                "display": "Older email",
                "date": "2026-09-01T09:00:00",
            },
        ),
        edge("REFERENCED_IN", {"id": TSK, "_type": "Task", "display": "Send the plan"}),
    ],
}


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail: Exception | None = None

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        args = args or {}
        self.calls.append((tool, args))
        if self.fail:
            raise self.fail
        if tool == "nexus_search":
            t = args["term"].lower()
            if t in ("alovelace", "ada.lovelace@example.org"):
                return [{**ADA_REC, "_score": 1.0, "_reason": "Exact ID"}]
            if t == "ada":
                return [
                    {**ADA_REC, "_score": 0.9, "_reason": "Name/Summary Match"},
                    {
                        "id": TSK,
                        "summary": "Send Ada the plan",
                        "status": "TODO",
                        "_type": "Task",
                        "_score": 0.9,
                        "_reason": "Name/Summary Match",
                    },
                ]
            return []
        if tool == "nexus_tree":
            if args["term"] in ("alovelace", ADA):
                return ADA_TREE
            if args["term"] == IX1:
                return {
                    "root": {"id": IX1, "summary": "Older email", "_type": "Interaction"},
                    "edges": [
                        edge(
                            "PARTICIPATED_IN",
                            {"id": LAB, "_type": "Lab", "display": "Lovelace Lab (alovelace)"},
                        ),
                        edge(
                            "REFERENCED_IN",
                            {"id": GCP, "_type": "GCPProject", "display": "ada-lab"},
                        ),
                    ],
                }
            raise McpToolError(f"nexus_tree: No entity matches '{args['term']}'")
        if tool == "nexus_tasks_list":
            rows = [
                {
                    "id": TSK,
                    "summary": "Send the plan",
                    "status": "TODO",
                    "priority": "HIGH",
                    "due_date": "2026-10-05T00:00:00",
                    "_type": "Task",
                    "assigned_to": {"netid": "me", "name": "Me"},
                },
                {
                    "id": "99999999-0000-0000-0000-000000000009",
                    "summary": "Mike's task",
                    "status": "BLOCKED",
                    "priority": "LOW",
                    "due_date": None,
                    "_type": "Task",
                    "assigned_to": {"netid": "mikek", "name": "Mike"},
                },
            ]
            return rows
        if tool == "nexus_tasks_show":
            if args["task_id"] != TSK:
                raise McpToolError(f"nexus_tasks_show: No task {args['task_id']}")
            return {
                "task": {
                    "id": TSK,
                    "summary": "Send the plan",
                    "status": "TODO",
                    "priority": "HIGH",
                    "due_date": None,
                    "details": {},
                    "_type": "Task",
                },
                "assigned_to": {"netid": "me", "name": "Me"},
                "links": [
                    {
                        "type": "REFERENCED_IN",
                        "direction": "in",
                        "entity": {"id": "p1", "_type": "ResearchProject", "display": "CephRDS"},
                    }
                ],
            }
        if tool == "nexus_interactions_show":
            return {
                "interaction": {
                    "id": IX1,
                    "date": "2026-09-01T09:00:00",
                    "summary": "Older email",
                    "details": {"original_text": "full text"},
                    "notes": None,
                    "_type": "Interaction",
                },
                "linked_entities": [ADA_REC],
            }
        if tool == "nexus_interactions_list":
            return [
                {
                    "id": IX2,
                    "date": "2026-10-02T10:00:00",
                    "summary": "Newer call",
                    "source": "mcp",
                    "original_text_chars": 10,
                }
            ]
        if tool == "nexus_people_list":
            return {
                "total": 1,
                "offset": 0,
                "count": 1,
                "people": [{"netid": "alovelace", "name": "Ada Lovelace", "id": ADA}],
            }
        if tool == "nexus_labs_list":
            return [{"id": LAB, "name": "Lovelace Lab (alovelace)", "type": "LAB", "_type": "Lab"}]
        if tool == "nexus_labs_show":
            if args["name"] != "Lovelace Lab (alovelace)":
                raise McpToolError(f"nexus_labs_show: No unit named '{args['name']}'")
            return {
                "unit": {
                    "id": LAB,
                    "name": "Lovelace Lab (alovelace)",
                    "type": "LAB",
                    "details": {},
                    "_type": "Lab",
                },
                "total_connections": 1,
                "connections": [],
            }
        if tool == "nexus_gcp_show":
            return {
                "gcp_project": {
                    "id": GCP,
                    "project_id": "ada-lab",
                    "name": "ada-lab",
                    "status": "ACTIVE",
                    "details": {},
                    "_type": "GCPProject",
                },
                "external_state": {"data": {"project_id": "ada-lab"}, "last_updated": "2026-01-01"},
            }
        if tool == "nexus_stats":
            return {"counts": {"researchers": 1}}
        if tool == "nexus_org_show":
            return {"name": args["unit_name"], "members": []}
        if tool == "nexus_ship_status":
            return {"counts": {"COLD": 1}, "researchers": []}
        raise AssertionError(f"unexpected tool {tool}")


@pytest.fixture
def fc():
    return FakeClient()


def test_search_rows_match_cli_shape(fc):
    rows = ledger_mcp.run(fc, ["search", "--limit", "25", "ada"])
    assert rows[0] == {
        "id": ADA,
        "type": "Researcher",
        "name": "Ada Lovelace (alovelace)",
        "score": 0.9,
        "reason": "Name/Summary Match",
    }
    assert rows[1]["type"] == "Task" and rows[1]["name"] == "Send Ada the plan"
    assert fc.calls[-1] == ("nexus_search", {"term": "ada", "limit": 25})


def test_people_show_exact_only(fc):
    p = ledger_mcp.run(fc, ["people", "show", "alovelace"])
    assert p["netid"] == "alovelace" and p["email_alias"] == "ada.lovelace@example.org"
    assert "_type" not in p and "_score" not in p
    assert ledger_mcp.run(fc, ["people", "show", "ada"]) is None  # a fuzzy hit is not a match


def test_tree_shape_and_order(fc):
    t = ledger_mcp.run(fc, ["tree", "alovelace"])
    assert t["root"] == {"id": ADA, "type": "Researcher", "name": "Ada Lovelace (alovelace)"}
    kinds = [c["entity_type"] for c in t["connections"]]
    # non-interactions first, then interactions oldest first (the CLI's order; callers
    # take the newest five from the end)
    assert kinds[-2:] == ["Interaction", "Interaction"]
    assert [c["name"] for c in t["connections"][-2:]] == ["Older email", "Newer call"]
    assert set(t["connections"][0]) == {"type", "role", "id", "entity_type", "name"}
    assert fc.calls[-1] == ("nexus_tree", {"term": "alovelace", "max_edges": 5000})


def test_tree_not_found_is_none(fc):
    assert ledger_mcp.run(fc, ["tree", "nobody"]) is None


def test_dossier_from_tree(fc):
    d = ledger_mcp.run(fc, ["dossier", "alovelace"])
    assert d["researcher"]["netid"] == "alovelace"
    assert [x["name"] for x in d["labs"]] == ["Lovelace Lab (alovelace)"]
    assert [i["summary"] for i in d["interactions"]] == [
        "Newer call",
        "Older email",
    ]  # newest first
    assert d["interactions"][0]["date"] == "2026-10-02T10:00:00"
    types = {c["entity_type"] for c in d["connections"]}
    assert types == {"Lab", "GCPProject", "Task"}
    assert all("connection_type" in c for c in d["connections"])
    assert ledger_mcp.run(fc, ["dossier", "nobody"]) is None


def test_tasks_list_mine_vs_global(fc):
    mine = ledger_mcp.run(fc, ["tasks", "list"], my_id="me")
    assert [t["id"] for t in mine] == [TSK]
    assert mine[0] == {
        "id": TSK,
        "summary": "Send the plan",
        "status": "TODO",
        "priority": "HIGH",
        "due_date": "2026-10-05T00:00:00",
        "assigned_to": "me",
    }
    assert fc.calls[-1][1]["status"] == "OPEN"
    allx = ledger_mcp.run(fc, ["tasks", "list", "--global"], my_id="me")
    assert len(allx) == 2
    ledger_mcp.run(fc, ["tasks", "list", "--global", "--all"], my_id="me")
    assert "status" not in fc.calls[-1][1]  # --all includes DONE


def test_tasks_show(fc):
    t = ledger_mcp.run(fc, ["tasks", "show", TSK])
    assert (
        t["summary"] == "Send the plan" and t["assigned_to"] == "me" and t["project"] == "CephRDS"
    )
    assert ledger_mcp.run(fc, ["tasks", "show", "99999999-0000-0000-0000-000000000000"]) is None


def test_interactions_show_has_links(fc):
    d = ledger_mcp.run(fc, ["interactions", "show", IX1])
    assert d["summary"] == "Older email" and d["details"]["original_text"] == "full text"
    assert {x["id"] for x in d["links"]} >= {LAB, GCP}
    assert set(d["links"][0]) == {
        "id",
        "name",
        "entity_type",
        "connection_type",
        "direction",
        "meta_data",
    }


def test_interactions_list_filters(fc):
    rows = ledger_mcp.run(
        fc, ["interactions", "list", "--limit", "20", "--since", "2026-10-01", "--contains", "call"]
    )
    assert rows == [{"id": IX2, "date": "2026-10-02T10:00:00", "summary": "Newer call"}]
    assert fc.calls[-1] == (
        "nexus_interactions_list",
        {"limit": 20, "since": "2026-10-01", "contains": "call"},
    )


def test_lists_and_shows(fc):
    assert ledger_mcp.run(fc, ["people", "list", "--limit", "20000"])[0]["netid"] == "alovelace"
    labs = ledger_mcp.run(fc, ["labs", "list", "--all"])
    assert labs[0]["name"] == "Lovelace Lab (alovelace)" and "_type" not in labs[0]
    assert fc.calls[-1] == ("nexus_labs_list", {"limit": 5000})
    lab = ledger_mcp.run(fc, ["labs", "show", "Lovelace Lab (alovelace)"])
    assert lab["id"] == LAB
    assert ledger_mcp.run(fc, ["labs", "show", "Nope"]) is None
    g = ledger_mcp.run(fc, ["gcp", "show", "ada-lab"])
    assert g["project_id"] == "ada-lab" and g["external_state"]["data"]["project_id"] == "ada-lab"
    assert ledger_mcp.run(fc, ["stats"])["counts"]["researchers"] == 1
    assert ledger_mcp.run(fc, ["org", "show", "Math Dept"])["name"] == "Math Dept"
    assert ledger_mcp.run(fc, ["ship", "status", "--limit", "1000"])["counts"]["COLD"] == 1


def test_unmapped_reads_raise(fc):
    for argv in (["doctor", "--sample", "10"], ["gcp", "audit-report"]):
        with pytest.raises(ledger_mcp.NotMapped):
            ledger_mcp.run(fc, argv)


def test_no_write_tool_is_ever_called(fc):
    """Every argv Ultra reads with maps only to read tools."""
    argvs = [
        ["search", "ada"],
        ["tree", "alovelace"],
        ["dossier", "alovelace"],
        ["stats"],
        ["people", "show", "alovelace"],
        ["people", "list"],
        ["tasks", "list", "--global"],
        ["tasks", "show", TSK],
        ["interactions", "list"],
        ["interactions", "show", IX1],
        ["labs", "list"],
        ["labs", "show", "Lovelace Lab (alovelace)"],
        ["gcp", "show", "ada-lab"],
        ["org", "show", "x"],
        ["ship", "status"],
    ]
    for a in argvs:
        ledger_mcp.run(fc, a)
    read_tools = {
        "nexus_search",
        "nexus_tree",
        "nexus_stats",
        "nexus_people_list",
        "nexus_tasks_list",
        "nexus_tasks_show",
        "nexus_interactions_list",
        "nexus_interactions_show",
        "nexus_labs_list",
        "nexus_labs_show",
        "nexus_gcp_show",
        "nexus_org_show",
        "nexus_ship_status",
    }
    assert {t for t, _ in fc.calls} <= read_tools


# ---------------------------------------------------------------- Ledger integration
def _ledger(fc: FakeClient, tmp_path, cli: bool) -> Ledger:
    cfg = Config(
        {
            "ledger": {
                "backend": "mcp",
                "my_id": "me",
                "org_email_domain": "example.org",
                "netid_from_local_part": True,
                "binary": "true" if cli else "no-such-binary-x",
            }
        }
    )
    led = Ledger(cfg, Store(tmp_path / "s.db"), mcp=fc)  # type: ignore[arg-type]
    return led


def test_ledger_uses_mcp_when_configured(fc, tmp_path):
    led = _ledger(fc, tmp_path, cli=False)
    assert led.enabled  # no CLI on PATH, but MCP is enough
    assert led.resolve("alovelace@example.org", "")["netid"] == "alovelace"
    assert led.state["via"] == "mcp"
    ctx = led.context("alovelace@example.org")
    assert ctx["labs"] == ["Lovelace Lab (alovelace)"] and ctx["interaction_count"] == 2
    assert ctx["interactions"][0]["summary"] == "Newer call"  # newest first in the rail
    assert [t["id"] for t in ctx["open_tasks"]] == [TSK]


def test_ledger_backend_default_is_cli(fc, tmp_path):
    led = Ledger(
        Config({"ledger": {"binary": "no-such-binary-x"}}), Store(tmp_path / "s.db"), mcp=fc
    )  # type: ignore[arg-type]
    assert led.mcp is None and not led.enabled


def test_unreachable_without_cli_raises_ledger_error(fc, tmp_path):
    led = _ledger(fc, tmp_path, cli=False)
    fc.fail = McpUnreachable("nexus: cannot reach the server")
    with pytest.raises(LedgerError):
        led.search("ada")
    assert led.state["ok"] is False and "MCP" in led.state["error"]


def test_auth_needed_without_cli_raises(fc, tmp_path):
    led = _ledger(fc, tmp_path, cli=False)
    fc.fail = McpAuthNeeded("not signed in to nexus: run `ultra auth nexus`")
    with pytest.raises(LedgerError, match="ultra auth nexus"):
        led.search("ada")


def test_falls_back_to_cli_when_unreachable(fc, tmp_path, monkeypatch):
    """With the CLI installed, an unreachable server falls back to the CLI read."""
    import subprocess

    led = _ledger(fc, tmp_path, cli=True)
    fc.fail = McpUnreachable("down")
    ran: list[list[str]] = []

    def fake_run(argv, **kw):
        ran.append(argv)
        return subprocess.CompletedProcess(
            argv, 0, stdout='[{"id": "x", "type": "Lab", "name": "L"}]', stderr=""
        )

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert led.search("ada") == [{"id": "x", "type": "Lab", "name": "L"}]
    assert ran and ran[0][1:] == ["search", "ada", "--json"]


def test_unmapped_read_goes_to_cli(fc, tmp_path, monkeypatch):
    import subprocess

    led = _ledger(fc, tmp_path, cli=True)
    ran: list[list[str]] = []

    def fake_run(argv, **kw):
        ran.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert led._run(["doctor", "--sample", "10"]) == {"ok": True}
    assert fc.calls == []  # never sent to the MCP server


def test_read_allowlist_still_applies_before_mcp(fc, tmp_path):
    led = _ledger(fc, tmp_path, cli=False)
    with pytest.raises(LedgerError):
        led._run(["log", "--date", "x", "-"])
    with pytest.raises(LedgerError):
        led._run(["doctor", "--fix"])
    assert fc.calls == []


def test_graph_second_hop_fills_in_background_on_mcp(fc, tmp_path):
    """First answer at once with the first hop; the second hop arrives from the
    background fill (8 wide over MCP) and the next ask has it, with pending 0."""
    import time as _t

    from ultra.graph import Graph

    led = _ledger(fc, tmp_path, cli=False)
    g = Graph(led, led.store, my_id="alovelace")
    first = g.r_graph({}, None, None)  # type: ignore[arg-type]
    assert {n["name"] for n in first["nodes"]} >= {"Lovelace Lab (alovelace)", "ada-lab"}
    for _ in range(50):
        if not g.warming:
            break
        _t.sleep(0.05)
    again = g.r_graph({}, None, None)  # type: ignore[arg-type]
    assert again["pending"] == 0 and again["second_hop"] == 2
