"""v1.6: ledger writes through the hosted ledger MCP server (SPEC 8.4, 8.8).

`ledger_mcp_write.run(client, argv, stdin)` maps each argv Ultra's two writers build to
one write tool. What must hold:

- every write argv the writers build maps to the right tool and arguments;
- deletes (and `projects docs rm`) never go to MCP;
- a refusal before anything ran falls back to serve/CLI; an unknown outcome (timeout,
  5xx, lost connection) is never re-run anywhere, because a second log is a duplicate;
- the writers' read-back parsing works on the server's answers.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from ultra import ledger_mcp_write as W
from ultra.config import Config
from ultra.ledger_write import LedgerWriter, WriteError
from ultra.mcpclient import McpAuthNeeded, McpToolError, McpUnknown, McpUnreachable

TID = "11111111-2222-4333-8444-555555555555"
IID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
PID = "99999999-8888-4777-8666-555555555555"


class FakeClient:
    def __init__(self, answers: dict[str, Any] | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.answers = answers or {}

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        self.calls.append((tool, dict(args or {})))
        a = self.answers.get(tool)
        if callable(a):
            return a(args or {})
        if isinstance(a, Exception):
            raise a
        if a is not None:
            return a
        return {"ok": True, "exit_code": 0, "output": "done", "errors": "", "json": None}

    def whoami(self) -> dict[str, Any]:
        return {"netid": "alovelace", "role": "admin"}


# ---------------------------------------------------------------- argv -> tool
CASES = [
    (
        ["log", "--date", "2026-10-03 09:00", "--link", PID, "--strict-links", "--yes", "-"],
        "hello\n",
        ("nexus_log", {"text": "hello", "links": [PID], "date": "2026-10-03 09:00"}),
    ),
    (
        ["tasks", "add", "--priority", "HIGH", "--json", "--due", "2026-10-09", "--", "-odd text"],
        "",
        ("nexus_tasks_add", {"summary": "-odd text", "priority": "HIGH", "due": "2026-10-09"}),
    ),
    (
        ["tasks", "update", TID, "--status", "DONE"],
        "",
        ("nexus_tasks_update", {"task_id": TID, "status": "DONE"}),
    ),
    (
        ["tasks", "update", TID, "--clear-due"],
        "",
        ("nexus_tasks_update", {"task_id": TID, "clear_due": True}),
    ),
    (
        ["link", PID, IID, "--type", "REFERENCED_IN"],
        "",
        (
            "nexus_link",
            {"source": PID, "target": IID, "connection_type": "REFERENCED_IN", "role": "Member"},
        ),
    ),
    (
        ["link", PID, IID, "--type", "MEMBER_OF", "--role", "PI"],
        "",
        (
            "nexus_link",
            {"source": PID, "target": IID, "connection_type": "MEMBER_OF", "role": "PI"},
        ),
    ),
    (["unlink", PID, IID, "--force"], "", ("nexus_unlink", {"source": PID, "target": IID})),
    (
        ["interactions", "edit", IID, "--force", "--summary", "fixed", "--note", "typo"],
        "",
        ("nexus_interactions_edit", {"interaction_id": IID, "summary": "fixed", "note": "typo"}),
    ),
    (
        ["people", "add", "--title", "Prof", "--", "alovelace", "Ada Lovelace"],
        "",
        ("nexus_people_add", {"netid": "alovelace", "name": "Ada Lovelace", "title": "Prof"}),
    ),
    (
        ["people", "update", "alovelace", "--set", "office=MRB 1", "--set", "a=b=c"],
        "",
        ("nexus_people_update", {"netid": "alovelace", "details": {"office": "MRB 1", "a": "b=c"}}),
    ),
    (
        ["people", "tag", "alovelace", "VIP"],
        "",
        ("nexus_people_tag", {"netid": "alovelace", "tag": "VIP"}),
    ),
    (
        ["labs", "add", "--description", "d", "--", "Lovelace Lab"],
        "",
        ("nexus_labs_add", {"name": "Lovelace Lab", "description": "d"}),
    ),
    (
        ["labs", "update", "Lovelace Lab", "--set", "room=1"],
        "",
        ("nexus_labs_update", {"name": "Lovelace Lab", "details": {"room": "1"}}),
    ),
    (
        ["projects", "add", "--summary", "s", "--", "Engine"],
        "",
        ("nexus_projects_add", {"name": "Engine", "summary": "s"}),
    ),
    (
        ["projects", "docs", "add", "Engine", "https://docs.example.org/x", "--title", "Plan"],
        "",
        (
            "nexus_projects_docs_add",
            {"project_name": "Engine", "url": "https://docs.example.org/x", "title": "Plan"},
        ),
    ),
    (
        ["grants", "add", "--agency", "NSF", "--amount", "5000", "--", "C-1", "Engines"],
        "",
        (
            "nexus_grants_add",
            {"c_number": "C-1", "title": "Engines", "agency": "NSF", "amount": 5000},
        ),
    ),
    (
        ["gcp", "add", "my-proj-1", "--name", "Mine"],
        "",
        ("nexus_gcp_add", {"project_id": "my-proj-1", "name": "Mine"}),
    ),
    (
        ["assets", "add", "--type", "GPU", "--location", "Rack 2", "--", "Box"],
        "",
        ("nexus_assets_add", {"name": "Box", "asset_type": "GPU", "location": "Rack 2"}),
    ),
]


@pytest.mark.parametrize(("argv", "stdin", "want"), CASES, ids=[" ".join(c[0][:2]) for c in CASES])
def test_every_write_maps_to_its_tool(argv, stdin, want):
    assert W.build(argv, stdin) == want


@pytest.mark.parametrize(
    "argv",
    [
        ["tasks", "delete", TID, "--force"],
        ["interactions", "delete", IID, "--force"],
        ["people", "delete", "alovelace", "--force"],
        ["labs", "delete", "Lovelace Lab", "--force"],
        ["projects", "delete", "Engine", "--force"],
        ["projects", "docs", "rm", "Engine", "https://docs.example.org/x"],
        ["gcp", "delete", "my-proj-1", "--force"],
        ["grants", "delete", "C-1", "--force"],
        ["assets", "delete", "Box", "--force"],
        ["doctor", "--fix"],
        ["log", "--date", "2026-10-03", "inline text"],  # text not on stdin
        ["tasks", "add", "--owner", "x", "--", "t"],  # an option the tool does not take
    ],
)
def test_deletes_and_unknown_shapes_stay_on_the_cli(argv):
    with pytest.raises(W.NotMapped):
        W.build(argv)


def test_a_log_with_no_links_links_you():
    c = FakeClient()
    W.run(c, ["log", "--date", "2026-10-03", "--strict-links", "--yes", "-"], "x", me="")
    assert c.calls[0] == ("nexus_log", {"text": "x", "links": ["alovelace"], "date": "2026-10-03"})


# ---------------------------------------------------------------- outcomes
def test_server_output_is_passed_back_with_the_log_id():
    c = FakeClient(
        {
            "nexus_log": {
                "ok": False,
                "exit_code": 1,
                "output": "Reading from stdin...",
                "errors": "",
                "json": {
                    "interaction_id": IID,
                    "linked": ["Ada"],
                    "unresolved": ["zz"],
                    "committed": True,
                },
            }
        }
    )
    rc, out = W.run(
        c, ["log", "--date", "2026-10-03", "--link", "zz", "--strict-links", "--yes", "-"], "x"
    )
    assert rc == 1 and f"Logged (ID: {IID})" in out and "--link 'zz' did not resolve" in out


@pytest.mark.parametrize("exc", [McpUnreachable("down"), McpAuthNeeded("signed out")])
def test_refused_before_running_raises_refused(exc):
    with pytest.raises(W.Refused):
        W.run(FakeClient({"nexus_tasks_update": exc}), ["tasks", "update", TID, "--status", "DONE"])


def test_a_tool_refusal_without_exit_code_is_refused():
    c = FakeClient({"nexus_people_update": {"ok": False, "error": "invalid key 'title'"}})
    with pytest.raises(W.Refused):
        W.run(c, ["people", "update", "alovelace", "--set", "title=Prof"])


@pytest.mark.parametrize(
    "answer",
    [
        McpUnknown("timed out"),
        McpToolError("boom"),
        {"ok": False, "error": "timed out after 10 minutes; the outcome is unknown"},
        "not a dict",
    ],
)
def test_unknown_outcomes_are_failures_never_refusals(answer):
    rc, out = W.run(
        FakeClient({"nexus_log": answer}),
        ["log", "--link", PID, "--strict-links", "--yes", "-"],
        "x",
    )
    assert rc == 1 and ("unknown" in out.lower() or "check the record" in out.lower())


def test_unlink_does_the_servers_second_step():
    def unlink(a):
        if "confirm_token" not in a:
            return {"ok": True, "confirm_required": True, "confirm_token": "tok1"}
        assert a["confirm_token"] == "tok1"
        return {
            "ok": True,
            "exit_code": 0,
            "output": f"Unlinked {PID} and {IID}.",
            "errors": "",
            "json": None,
        }

    c = FakeClient({"nexus_unlink": unlink})
    rc, out = W.run(c, ["unlink", PID, IID, "--force"])
    assert (
        rc == 0
        and "Unlinked" in out
        and [t for t, _ in c.calls] == ["nexus_unlink", "nexus_unlink"]
    )


# ---------------------------------------------------------------- the desk writer
class Store:
    def __init__(self):
        self.journaled: list[tuple] = []

    def journal(self, *a):
        self.journaled.append(a)

    def cache_del_prefix(self, p):
        pass


def writer(monkeypatch, client, cli: bool = True) -> tuple[LedgerWriter, list]:
    ran: list = []
    monkeypatch.setattr("ultra.ledger_write.shutil.which", lambda b: "/bin/nexus" if cli else None)

    def fake_run(cmd, **kw):
        ran.append(cmd)

        class R:
            returncode = 0
            stdout = (
                "Logged (ID: " + IID + ")\nUpdated Task: x\nLinked a --[X]--> b\nUnlinked a and b."
            )
            stderr = ""

        return R()

    monkeypatch.setattr("ultra.ledger_write.subprocess.run", fake_run)
    w = LedgerWriter(Config({"ledger": {"my_id": "alovelace"}}), Store(), mcp=client)  # type: ignore[arg-type]
    monkeypatch.setattr(w.serve, "available", lambda: False)
    return w, ran


def test_desk_log_goes_over_mcp_and_reads_the_id(monkeypatch):
    c = FakeClient(
        {
            "nexus_log": {
                "ok": True,
                "exit_code": 0,
                "output": "Linked (explicit): Ada Lovelace",
                "errors": "",
                "json": {"interaction_id": IID, "linked": [], "unresolved": []},
            }
        }
    )
    w, ran = writer(monkeypatch, c)
    r = w.log("Met Ada", "2026-10-03 09:00", [PID])
    assert r["id"] == IID and r["rc"] == 0 and not ran
    assert c.calls[0][0] == "nexus_log" and w.last_via == "mcp"


def test_desk_task_add_reads_the_json_line(monkeypatch):
    line = json.dumps({"id": TID, "summary": "x", "due_date": "2026-10-09"})
    c = FakeClient(
        {
            "nexus_tasks_add": {
                "ok": True,
                "exit_code": 0,
                "output": "Added\n" + line,
                "errors": "",
                "json": None,
            }
        }
    )
    w, ran = writer(monkeypatch, c)
    r = w.task_add("x", "HIGH", "2026-10-09")
    assert r["id"] == TID and r["due_date"] == "2026-10-09" and not ran


def test_desk_unknown_outcome_is_never_rerun_on_the_cli(monkeypatch):
    w, ran = writer(monkeypatch, FakeClient({"nexus_log": McpUnknown("timed out")}))
    r = w.log("Met Ada", "2026-10-03 09:00", [PID])
    assert r["id"] is None and r["rc"] == 1 and not ran  # no second log anywhere


def test_desk_refusal_falls_back_to_the_cli(monkeypatch):
    w, ran = writer(monkeypatch, FakeClient({"nexus_log": McpUnreachable("down")}))
    r = w.log("Met Ada", "2026-10-03 09:00", [PID])
    assert ran and ran[0][1] == "log" and r["id"] == IID and w.last_via == "cli"


def test_desk_refusal_without_a_cli_is_an_error(monkeypatch):
    w, ran = writer(monkeypatch, FakeClient({"nexus_log": McpUnreachable("down")}), cli=False)
    with pytest.raises(WriteError):
        w.log("Met Ada", "2026-10-03 09:00", [PID])
    assert not ran


def test_desk_without_mcp_uses_the_cli(monkeypatch):
    w, ran = writer(monkeypatch, None)
    w.log("Met Ada", "2026-10-03 09:00", [PID])
    assert ran and w.last_via == "cli"


# ---------------------------------------------------------------- the Ledger tab writer
def tab_writer(monkeypatch, client, cli: bool):
    from ultra.ledgertab import LedgerTabWriter

    ran: list = []

    class L:
        mcp = client
        my_id = "alovelace"
        has_cli = cli
        binary = "nexus"

        class serve:
            @staticmethod
            def available():
                return False

    def fake_run(cmd, **kw):
        ran.append(cmd)

        class R:
            returncode = 0
            stdout = "deleted"
            stderr = ""

        return R()

    monkeypatch.setattr("subprocess.run", fake_run)
    return LedgerTabWriter(L(), Store()), ran  # type: ignore[arg-type]


def test_tab_delete_never_reaches_mcp(monkeypatch):
    c = FakeClient()
    w, ran = tab_writer(monkeypatch, c, cli=True)
    rc, _ = w._run(["tasks", "delete", TID, "--force"], "")
    assert rc == 0 and ran and not c.calls


def test_tab_delete_without_a_cli_says_so(monkeypatch):
    c = FakeClient()
    w, ran = tab_writer(monkeypatch, c, cli=False)
    rc, out = w._run(["people", "delete", "alovelace", "--force"], "")
    assert rc == 1 and "CLI" in out and not ran and not c.calls


def test_tab_write_goes_over_mcp(monkeypatch):
    c = FakeClient()
    w, ran = tab_writer(monkeypatch, c, cli=True)
    rc, _ = w._run(["people", "tag", "alovelace", "VIP"], "")
    assert (
        rc == 0
        and c.calls == [("nexus_people_tag", {"netid": "alovelace", "tag": "VIP"})]
        and not ran
    )


def test_tab_unknown_outcome_is_not_rerun(monkeypatch):
    w, ran = tab_writer(monkeypatch, FakeClient({"nexus_log": McpUnknown("lost")}), cli=True)
    rc, _ = w._run(["log", "--date", "2026-10-03", "--strict-links", "--yes", "-"], "x\n")
    assert rc == 1 and not ran
