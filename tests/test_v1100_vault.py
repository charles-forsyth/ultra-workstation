"""v1.10: Home, the personal notes vault, read only (SPEC 8.10).

A fake stdio MCP server (a tiny Python script) stands in for headless-obsidian-mcp, so
these tests drive the real StdioMcp transport: start, initialize, call, restart after a
crash. The vault guards (read tools only, OBSIDIAN_TOOLS=reads, path checks, exclude)
are tested against it.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from ultra.config import Config
from ultra.vault import READ_TOOLS, StdioMcp, Vault, VaultApi, VaultError, client_from_config

ROOT = Path(__file__).resolve().parents[1]

FAKE = r"""
import json, os, sys
log = open(os.environ["FAKE_LOG"], "a")
for line in sys.stdin:
    m = json.loads(line)
    log.write(json.dumps({"method": m.get("method"), "params": m.get("params"), "tools": os.environ.get("OBSIDIAN_TOOLS")}) + "\n"); log.flush()
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake"}}
    elif m["method"] == "tools/call":
        name, args = m["params"]["name"], m["params"]["arguments"]
        if name == "crash":
            sys.exit(3)
        if name == "list_tasks":
            body = {"results": [
                {"path": "Hubs/Todo", "text": "Buy garlic \U0001F4C5 2026-10-01", "line": 3},
                {"path": "Hubs/Todo", "text": "Oil change due: 2026-10-05", "line": 4},
                {"path": "Hubs/Todo", "text": "Winterize \U0001F4C5 2026-10-31", "line": 5},
                {"path": "Hubs/Todo", "text": "Fix latch", "line": 6},
                {"path": "_archive/Old", "text": "Old thing \U0001F4C5 2026-09-01", "line": 1},
            ]}
        elif name == "search_notes_ranked":
            body = {"results": [{"path": "_archive/Old", "title": "Old"}, {"path": "Assets/Truck", "title": "Truck", "headline": "Truck log", "snippet": "oil"}]}
        elif name == "read_notes":
            p = args["paths"][0]
            body = {"notes": [{"path": p, "contents": "# Hi\n<script>x</script>", "tags": []}], "errors": []} if p != "missing" else {"notes": [], "errors": ["missing: not found"]}
        elif name == "get_vault_stats":
            body = {"notes": 3}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": "unknown tool"}], "isError": True}})); sys.stdout.flush(); continue
        r = {"content": [{"type": "text", "text": json.dumps(body)}]}
    else:
        r = {}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r})); sys.stdout.flush()
"""


@pytest.fixture
def vault(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_mcp.py"
    script.write_text(FAKE)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    cfg = Config(
        {
            "vault": {
                "path": str(tmp_path / "Notes"),
                "command": sys.executable,
                "args": [str(script)],
                "exclude": ["_archive"],
                "timeout": 10,
            }
        }
    )
    client = client_from_config(cfg)
    assert client is not None
    client.env["FAKE_LOG"] = str(log)
    v = Vault(cfg, client, today_fn=lambda: dt.date(2026, 10, 3))
    yield v, log
    client.close()


def calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_todos_are_bucketed_by_due_date_and_exclude_hides_folders(vault):
    v, _ = vault
    h = v.todos()
    assert [x["text"] for x in h["overdue"]] == ["Buy garlic"]
    assert h["overdue"][0]["days"] == -2
    assert [x["text"] for x in h["soon"]] == ["Oil change"]
    assert [x["text"] for x in h["later"]] == ["Winterize"]
    assert h["undated"] == 1  # "Fix latch"; the _archive one is hidden, not counted


def test_server_always_runs_with_read_tools_only(vault):
    v, log = vault
    v.health()
    assert all(c["tools"] == "reads" for c in calls(log))
    assert calls(log)[0]["method"] == "initialize"


def test_write_tools_are_refused_before_reaching_the_server(vault):
    v, log = vault
    for tool in ("write_note", "append_note", "delete_note", "move_note", "bulk_edit"):
        with pytest.raises(VaultError, match="not a read tool"):
            v._call(tool, {"path": "x"})
    assert not [c for c in calls(log) if c["method"] == "tools/call"] if log.exists() else True
    assert all(not t.startswith(("write", "append", "delete", "move", "set_")) for t in READ_TOOLS)


@pytest.mark.parametrize("bad", ["../etc/passwd", "/etc/passwd", "a/../../b", "", "_archive/Old"])
def test_note_paths_are_checked(vault, bad):
    v, _ = vault
    with pytest.raises(VaultError):
        v.note(bad)


def test_note_reads_and_search_hides_excluded(vault):
    v, _ = vault
    n = v.note("Assets/Truck")
    assert n["contents"].startswith("# Hi") and "obsidian://open?vault=Notes" in n["open_url"]
    assert [r["path"] for r in v.search("oil")] == ["Assets/Truck"]
    with pytest.raises(VaultError, match="not found"):
        v.note("missing")


def test_server_crash_is_an_error_then_it_restarts(vault):
    v, log = vault
    v.health()
    with pytest.raises(VaultError, match="stopped"):
        v.client.call("crash", {})
    v.cache.clear()
    assert v.health()["ok"] is True  # a fresh process
    assert sum(1 for c in calls(log) if c["method"] == "initialize") == 2


def test_tool_error_and_cache(vault):
    v, log = vault
    with pytest.raises(VaultError, match="unknown tool"):
        v.client.call("nope", {})
    v.todos()
    v.todos()
    assert sum(1 for c in calls(log) if (c.get("params") or {}).get("name") == "list_tasks") == 1


def test_not_configured_is_off(tmp_path):
    assert client_from_config(Config({})) is None
    assert client_from_config(Config({"vault": {"path": "/x", "enabled": False}})) is None
    v = Vault(Config({}), None)
    assert v.enabled is False
    api = VaultApi(v)
    assert api.r_home({}, None, None) == {"enabled": False}  # type: ignore[arg-type]


def test_missing_binary_is_a_clean_error(tmp_path):
    c = StdioMcp(["/nonexistent/node-x"], {"PATH": "/usr/bin"}, timeout=5)
    with pytest.raises(VaultError, match="cannot run"):
        c.call("get_vault_stats", {})


def test_ui_wiring():
    app = (ROOT / "src/ultra/static/app.js").read_text()
    today = (ROOT / "src/ultra/static/today.js").read_text()
    home = (ROOT / "src/ultra/static/home.js").read_text()
    assert "Search my notes (vault)..." in app and "searchVault(text" in app
    assert 'id="today-home"' in today and "homeLine(" in today
    assert "renderMd(n.contents)" in home  # note text goes through the escaping renderer
    assert "innerHTML = `${n.contents" not in home
    for verb in ("POST", "write_note", "append"):
        assert verb not in home  # the page only reads
