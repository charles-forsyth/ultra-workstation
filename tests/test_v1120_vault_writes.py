"""v1.12: Personal Log and Task write to the notes vault through vault-mcp (SPEC 8.10).

Two layers:
- unit tests with a fake vault-mcp (a tiny Python stdio server) drive the real StdioMcp
  transport, the read mapping, and the write allow-list;
- one end-to-end test runs the real vault-mcp binary (if installed) against a throwaway
  git vault, through Desk's stage -> commit path, and checks the note and the commit.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from ultra.config import Config
from ultra.vault import (
    READ_TOOLS,
    WRITE_TOOLS,
    Vault,
    VaultError,
    client_from_config,
    server_kind,
    writes_on,
)

ROOT = Path(__file__).resolve().parents[1]

FAKE = r"""
import json, os, sys
log = open(os.environ["FAKE_LOG"], "a")
for line in sys.stdin:
    m = json.loads(line)
    log.write(json.dumps({"method": m.get("method"), "params": m.get("params"), "ro": os.environ.get("VAULT_READ_ONLY"), "path": os.environ.get("VAULT_PATH")}) + "\n"); log.flush()
    if "id" not in m:
        continue
    if m["method"] == "initialize":
        r = {"protocolVersion": "2025-06-18", "capabilities": {}, "serverInfo": {"name": "fake-vault-mcp"}}
    elif m["method"] == "tools/call":
        name, a = m["params"]["name"], m["params"]["arguments"]
        if name == "vault_tasks":
            body = {"today": "2026-10-03", "results": [
                {"path": "01 - Hubs/Tasks and Todos.md", "line": 8, "text": "Coop waterer \U0001F4C5 2026-10-01", "due": "2026-10-01"},
                {"path": "01 - Hubs/Tasks and Todos.md", "line": 9, "text": "Garlic bed \U0001F4C5 2026-10-05", "due": "2026-10-05"},
                {"path": "01 - Hubs/Tasks and Todos.md", "line": 10, "text": "Read more"},
            ], "undated": 1}
        elif name == "vault_search":
            body = {"results": [{"path": "Journal/Garden/2026-09-06_Harvest.md", "title": "Harvest", "snippet": "garlic"}]}
        elif name == "vault_read":
            if a["path"].startswith("01 - Hubs/Tasks"):
                body = {"path": a["path"], "contents": "# T\n\n## WINTER PREP 2026\n\n## \u26a1 ACTIVE UPCOMING ACTIONS\n\n## \u2705 COMPLETED RECENTLY\n", "hash": "h"}
            else:
                body = {"path": a["path"], "contents": "# Hi", "hash": "h"}
        elif name == "vault_status":
            body = {"version": "0.1.0", "notes": 948, "log_topics": ["Homestead", "Garden"], "read_only": False}
        elif name in ("vault_log", "vault_task_add", "vault_log_note"):
            body = {"path": "Daily Notes/2026-10-03_Daily_Schedule.md" if name == "vault_log" else "01 - Hubs/Tasks and Todos.md", "commit": "abc1234", "line": 12, "created": False, "hash": "x", "message": "ok", "echo": a}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": {"content": [{"type": "text", "text": "unknown tool " + name}], "isError": True}})); sys.stdout.flush(); continue
        r = {"content": [{"type": "text", "text": json.dumps(body)}], "structuredContent": body}
    else:
        r = {}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": r})); sys.stdout.flush()
"""


def _cfg(tmp_path: Path, script: Path, **extra: Any) -> Config:
    return Config(
        {
            "vault": {
                "path": str(tmp_path / "Notes"),
                "name": "Notes",
                "command": sys.executable,
                "args": [str(script)],
                "kind": "vault-mcp",
                "timeout": 10,
                **extra,
            }
        }
    )


@pytest.fixture
def fake(tmp_path: Path, monkeypatch):
    script = tmp_path / "fake_vault_mcp.py"
    script.write_text(FAKE)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))

    def make(**extra: Any) -> Vault:
        cfg = _cfg(tmp_path, script, **extra)
        client = client_from_config(cfg)
        assert client is not None
        client.env["FAKE_LOG"] = str(log)
        made.append(client)
        return Vault(cfg, client, today_fn=lambda: dt.date(2026, 10, 3))

    made: list[Any] = []
    yield make, log
    for c in made:
        c.close()


def calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def test_kind_is_detected_from_the_command_and_writes_default_on():
    assert server_kind({"command": "/home/x/.local/bin/vault-mcp"}) == "vault-mcp"
    assert server_kind({"command": "node", "args": ["headless/index.js"]}) == "headless"
    assert server_kind({"command": "node", "kind": "vault-mcp"}) == "vault-mcp"
    assert writes_on({"command": "vault-mcp"}) is True
    assert writes_on({"command": "vault-mcp", "writes": False}) is False
    assert writes_on({"command": "node"}) is False  # the borrowed server never writes


def test_reads_map_to_vault_mcp_tools(fake):
    make, log = fake
    v = make()
    t = v.todos()
    assert [x["text"] for x in t["overdue"]] == ["Coop waterer"]
    assert [x["text"] for x in t["soon"]] == ["Garlic bed"] and t["undated"] == 1
    assert v.search("garlic")[0]["path"] == "Journal/Garden/2026-09-06_Harvest.md"
    assert v.note("Journal/Garden/2026-09-06_Harvest")["contents"] == "# Hi"
    assert v.health() == {"ok": True, "notes": 948}
    used = {c["params"]["name"] for c in calls(log) if c["method"] == "tools/call"}
    assert used == {"vault_tasks", "vault_search", "vault_read", "vault_status"}
    assert all(c["path"] == str(Path(v.client.env["VAULT_PATH"])) for c in calls(log))


def test_write_allow_list_and_via(fake):
    make, log = fake
    v = make()
    assert v.writes and WRITE_TOOLS == {"vault_log", "vault_log_note", "vault_task_add"}
    assert not (WRITE_TOOLS & READ_TOOLS)
    for bad in ("vault_append", "vault_task_done", "vault_checkin", "write_note", "delete_note"):
        with pytest.raises(VaultError, match="not a write Ultra makes"):
            v.write(bad, {"text": "x"})
    r = v.write("vault_log", {"text": "Fed the hens", "date": "2026-10-03"})
    assert r["commit"] == "abc1234" and r["echo"]["via"] == "Ultra"
    names = [c["params"]["name"] for c in calls(log) if c["method"] == "tools/call"]
    assert names == ["vault_log"]  # refused writes never reached the server


def test_writes_off_starts_the_server_read_only_and_refuses(fake):
    make, log = fake
    v = make(writes=False)
    assert v.writes is False
    with pytest.raises(VaultError, match="writes are off"):
        v.write("vault_log", {"text": "x"})
    v.health()
    assert all(c["ro"] == "1" for c in calls(log))


def test_write_options_lists_topics_and_open_sections(fake):
    make, _ = fake
    o = make().write_options()
    assert o["topics"] == ["Homestead", "Garden"]
    assert o["sections"] == ["WINTER PREP 2026", "\u26a1 ACTIVE UPCOMING ACTIONS"]  # no COMPLETED


# ---------------------------------------------------------------- Desk path


class _NoLedger:
    enabled = False


def _desk(vault: Vault):
    from ultra.desk import Desk
    from ultra.rules import Rules
    from ultra.store import Store

    store = Store(Path(vault.client.env["VAULT_PATH"]).parent / "state.db")  # type: ignore[union-attr]
    return Desk(
        store,
        Rules(me={"me@example.com"}),
        _NoLedger(),
        None,
        lambda k: {},
        "America/New_York",
        vault=vault,
    )


def _wait(desk, cid: str) -> dict[str, Any]:
    for _ in range(100):
        p = desk.r_commit_status({}, None, _m(cid))
        if p["state"] != "running":
            return p
        time.sleep(0.05)
    raise AssertionError("commit did not finish")


def _m(s: str):
    import re

    return re.match(r"(.+)", s)


def test_card_targets_the_vault_when_there_is_no_ledger(fake):
    make, log = fake
    desk = _desk(make())
    card = desk.r_stage_briefing(
        {}, {"text": "Swapped the furnace filter", "allow_unlinked": True, "title": "Note"}, None
    )
    assert card["target"] == "vault" and card["ledger"] is True
    assert card["vault"]["topics"] == ["Homestead", "Garden"] and card["vault"]["name"] == "Notes"
    desk.r_commit(
        {},
        {
            "card": card["id"],
            "text": "Swapped the furnace filter",
            "date": "2026-10-03 14:05",
            "where": {"kind": "daily"},
        },
        None,
    )
    p = _wait(desk, card["id"])
    assert p["state"] == "done", p
    assert (
        p["result"]["path"] == "Daily Notes/2026-10-03_Daily_Schedule.md"
        and p["result"]["commit"] == "abc1234"
    )
    w = [
        c for c in calls(log) if c["method"] == "tools/call" and c["params"]["name"] == "vault_log"
    ]
    assert w[0]["params"]["arguments"] == {
        "text": "Swapped the furnace filter",
        "date": "2026-10-03",
        "time": "2:05 PM",
        "via": "Ultra",
    }
    # the card is spent: a second commit is refused, nothing written twice
    from ultra.server import ApiError

    with pytest.raises(ApiError):
        desk.r_commit({}, {"card": card["id"], "text": "again", "date": "2026-10-03 14:05"}, None)


def test_journal_note_and_task_where(fake):
    make, log = fake
    desk = _desk(make())
    from ultra.server import ApiError

    card = desk.r_stage_briefing({}, {"text": "Bed prep.", "allow_unlinked": True}, None)
    with pytest.raises(ApiError, match="title"):
        desk.r_commit(
            {},
            {
                "card": card["id"],
                "text": "Bed prep.",
                "date": "2026-10-03 09:00",
                "where": {"kind": "journal", "topic": "Garden", "title": ""},
            },
            None,
        )
    desk.r_commit(
        {},
        {
            "card": card["id"],
            "text": "Bed prep.",
            "date": "2026-10-03 09:00",
            "where": {"kind": "journal", "topic": "Garden", "title": "Garlic bed"},
        },
        None,
    )
    assert _wait(desk, card["id"])["state"] == "done"
    t = desk.stager.stage("task", [{"kind": "note", "key": "", "subject": "x", "people": []}])
    desk._target(t)
    desk.r_commit(
        {},
        {
            "card": t["id"],
            "text": "Order garlic",
            "due": "2026-10-08",
            "where": {"section": "WINTER"},
        },
        None,
    )
    assert _wait(desk, t["id"])["state"] == "done"
    sent = [
        (c["params"]["name"], c["params"]["arguments"])
        for c in calls(log)
        if c["method"] == "tools/call" and c["params"]["name"] in WRITE_TOOLS
    ]
    assert sent == [
        (
            "vault_log_note",
            {
                "topic": "Garden",
                "title": "Garlic bed",
                "text": "Bed prep.",
                "date": "2026-10-03",
                "via": "Ultra",
            },
        ),
        (
            "vault_task_add",
            {"text": "Order garlic", "due": "2026-10-08", "section": "WINTER", "via": "Ultra"},
        ),
    ]


def test_with_a_ledger_the_card_still_goes_to_the_ledger(fake):
    make, _ = fake
    desk = _desk(make())

    class _On:
        enabled = True

    desk.ledger = _On()
    card = {"id": "x"}
    desk._target(card)
    assert card["target"] == "ledger"


def test_no_ledger_and_no_vault_writes_says_so(fake):
    make, _ = fake
    desk = _desk(make(writes=False))
    card = {"id": "x"}
    desk._target(card)
    assert card == {"id": "x", "ledger": False, "target": "none"}


def test_ui_wiring():
    js = (ROOT / "src/ultra/static/ledger.js").read_text()
    assert "The ledger CLI is not available" not in js
    assert 'c.target === "vault"' in js and "vaultWhere(c, isLog)" in js
    assert "Save to notes" in js and "body.where = w" in js
    assert 'if ($("#lc-chips"))' in js and 'if ($("#lc-find")) wireFind()' in js


# ---------------------------------------------------------------- end to end

VAULT_MCP = shutil.which("vault-mcp") or str(Path.home() / ".local/bin/vault-mcp")


@pytest.mark.skipif(not Path(VAULT_MCP).exists(), reason="vault-mcp is not installed")
def test_end_to_end_with_the_real_vault_mcp(tmp_path: Path):
    notes = tmp_path / "Notes"
    (notes / "01 - Hubs").mkdir(parents=True)
    (notes / "01 - Hubs/Tasks and Todos.md").write_text(
        "# Tasks\n\n## WINTER PREP 2026 (active, plan: [[W]])\n\n- [ ] Coop \U0001f4c5 2026-10-01\n\n## \u26a1 ACTIVE UPCOMING ACTIONS\n\n- [ ] Latch\n"
    )
    for a in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", "-C", str(notes), *a], check=True, capture_output=True)
    cfg = Config(
        {
            "vault": {
                "path": str(notes),
                "name": "Notes",
                "command": VAULT_MCP,
                "author": "Test Operator",
            }
        }
    )
    client = client_from_config(cfg)
    assert client is not None
    try:
        v = Vault(cfg, client, today_fn=lambda: dt.date(2026, 10, 3))
        assert v.kind == "vault-mcp" and v.writes
        assert [x["text"] for x in v.todos()["overdue"]][:1] == ["Coop"] or v.todos()["overdue"]
        desk = _desk(v)
        card = desk.r_stage_briefing({}, {"text": "Fed the hens", "allow_unlinked": True}, None)
        desk.r_commit(
            {},
            {
                "card": card["id"],
                "text": "Fed the hens",
                "date": "2026-10-03 07:30",
                "where": {"kind": "daily"},
            },
            None,
        )
        p = _wait(desk, card["id"])
        assert p["state"] == "done", p
        daily = (notes / "Daily Notes/2026-10-03_Daily_Schedule.md").read_text()
        assert "## Log\n\n- 7:30 AM Fed the hens\n" in daily
        t = desk.stager.stage("task", [{"kind": "note", "key": "", "subject": "x", "people": []}])
        desk._target(t)
        desk.r_commit(
            {},
            {
                "card": t["id"],
                "text": "Order garlic",
                "due": "2026-10-08",
                "where": {"section": ""},
            },
            None,
        )
        assert _wait(desk, t["id"])["state"] == "done"
        tasks = (notes / "01 - Hubs/Tasks and Todos.md").read_text()
        assert tasks.endswith(
            "- [ ] Latch\n- [ ] Order garlic \U0001f4c5 2026-10-08\n"
        )  # ACTIVE, not WINTER PREP
        log = subprocess.run(
            ["git", "-C", str(notes), "log", "--format=%an|%s", "-3"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "Test Operator (via Ultra)|Task: Order garlic (due 2026-10-08)" in log
        assert "Test Operator (via Ultra)|Log 2026-10-03: Fed the hens" in log
    finally:
        client.close()
