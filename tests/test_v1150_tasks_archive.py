"""v1.15: `[ledger] new_tasks = false` makes ledger tasks archive-only in a workspace.

The page hides Task everywhere (key t, buttons, menus, palette, help), and the server
refuses a new task on every path (desk stage, Ask/Board answers, a card staged before,
the Ledger tab, and the writer itself, before MCP or the CLI fallback), so a stale page
cannot add one. Reads, status changes and due dates keep working. A workspace whose
Task goes to the notes vault (Personal) is not affected.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from test_v04 import _m, _msgs, _rules

from ultra.config import Config
from ultra.demo import DemoAI, DemoLedger, DemoLedgerCLI, DemoWriter
from ultra.desk import Desk
from ultra.ledger_write import (
    TASKS_OFF_NOTE,
    LedgerWriter,
    WriteError,
    new_tasks_on,
    tasks_off_note,
)
from ultra.server import ApiError
from ultra.store import Store

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/ultra/static"
NOTE = "Tasks now live in the team tracker; ledger tasks are archive-only."
OFF = {"ledger": {"new_tasks": False, "tasks_note": NOTE}}


def src(name: str) -> str:
    return (STATIC / name).read_text()


# ---------------------------------------------------------------- config
def test_switch_defaults_on_and_reads_the_note():
    assert new_tasks_on(Config({})) is True
    assert new_tasks_on(Config(OFF)) is False
    assert tasks_off_note(Config(OFF)) == NOTE
    assert tasks_off_note(Config({"ledger": {"new_tasks": False}})) == TASKS_OFF_NOTE
    assert len(tasks_off_note(Config({"ledger": {"tasks_note": "x " * 500}}))) <= 200


def test_example_config_documents_the_switch():
    import tomllib

    text = (ROOT / "src/ultra/config.example.toml").read_text()
    assert tomllib.loads(text)["ledger"]["new_tasks"] is True


# ---------------------------------------------------------------- the writer
FAKE = r"""
import json, os, sys
open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\n")
print("Updated Task: x")
"""


def _writer(tmp_path: Path, monkeypatch, raw: dict[str, Any], mcp: Any = None) -> tuple:
    b = tmp_path / "nexus"
    b.write_text("#!/usr/bin/env python3\n" + FAKE)
    b.chmod(0o755)
    log = tmp_path / "calls.log"
    monkeypatch.setenv("FAKE_LOG", str(log))
    led = {**raw.get("ledger", {}), "binary": str(b), "use_serve": False}
    return LedgerWriter(Config({"ledger": led}), Store(tmp_path / "s.db"), mcp=mcp), log


def test_writer_refuses_task_add_before_anything_runs(tmp_path, monkeypatch):
    class Mcp:  # a hosted ledger server that must never be asked
        def call(self, *a: Any, **k: Any) -> Any:
            raise AssertionError("MCP was called")

    w, log = _writer(tmp_path, monkeypatch, OFF, mcp=Mcp())
    with pytest.raises(WriteError, match="archive-only"):
        w.task_add("Follow up with Ben", "HIGH")
    # even a direct argv, the CLI fallback path, is refused before MCP or the binary
    with pytest.raises(WriteError, match="archive-only"):
        w._run(["tasks", "add", "--priority", "LOW", "--json", "--", "x"])
    assert not log.exists()


def test_writer_still_changes_task_status_when_off(tmp_path, monkeypatch):
    w, log = _writer(tmp_path, monkeypatch, OFF)
    tid = "bbbbbbbb-0000-4000-8000-000000000002"
    assert w.task_status(tid, "DONE")["ok"]
    assert json.loads(log.read_text().splitlines()[0])[:2] == ["tasks", "update"]


def test_writer_default_still_adds(tmp_path, monkeypatch):
    w, _ = _writer(tmp_path, monkeypatch, {})
    assert w.new_tasks is True


# ---------------------------------------------------------------- the desk
def _desk(tmp_path: Path, off: bool = True) -> tuple[Desk, DemoWriter]:
    led = DemoLedger()
    w = DemoWriter(led)
    if off:
        w.new_tasks = False  # type: ignore[attr-defined]
        w.tasks_note = NOTE  # type: ignore[attr-defined]
    msgs = _msgs()
    desk = Desk(
        Store(tmp_path / "d.db"),
        _rules(),
        led,
        w,
        lambda key: {"key": key, "messages": msgs},
        "America/New_York",
        "adal",
    )
    return desk, w


def test_desk_refuses_a_task_card_and_keeps_log(tmp_path):
    desk, w = _desk(tmp_path)
    assert desk.tasks_off()
    with pytest.raises(ApiError) as e:
        desk.r_stage({}, {"action": "task", "key": "g-t1"}, None)
    assert e.value.status == 403 and str(e.value) == NOTE
    with pytest.raises(ApiError, match="archive-only"):
        desk.r_stage({}, {"action": "task"}, None)  # from the bucket
    with pytest.raises(ApiError, match="archive-only"):  # Ask "Task from it", Board Wait
        desk.r_stage_answer({}, {"action": "task", "text": "Follow up"}, None)
    assert desk.r_stage({}, {"action": "log", "key": "g-t1"}, None)["action"] == "log"
    assert w.calls == []


def test_a_task_card_staged_before_the_switch_cannot_commit(tmp_path):
    desk, w = _desk(tmp_path, off=False)
    card = desk.r_stage({}, {"action": "task", "key": "g-t1"}, None)
    w.new_tasks = False  # type: ignore[attr-defined]
    w.tasks_note = NOTE  # type: ignore[attr-defined]
    with pytest.raises(ApiError) as e:
        desk.r_commit({}, {"card": card["id"], "text": "Follow up", "chips": []}, None)
    assert e.value.status == 403 and w.calls == []


def test_desk_with_tasks_on_is_unchanged(tmp_path):
    desk, _ = _desk(tmp_path, off=False)
    assert not desk.tasks_off()
    assert desk.r_stage({}, {"action": "task", "key": "g-t1"}, None)["action"] == "task"


def test_personal_vault_task_is_never_switched_off(tmp_path):
    """Task goes to the notes vault where there is no ledger: the switch does not apply."""

    class NoLedger:
        enabled = False

    class Vault:
        writes = True
        name = "Notes"

        def write_options(self) -> dict[str, Any]:
            return {"topics": [], "sections": []}

    w = DemoWriter(DemoLedger())
    w.new_tasks = False  # type: ignore[attr-defined]
    desk = Desk(
        Store(tmp_path / "d.db"), _rules(), NoLedger(), w, lambda k: {}, "UTC", vault=Vault()
    )
    assert not desk.tasks_off()
    card = desk.r_stage_answer({}, {"action": "task", "text": "Feed the hens"}, None)
    assert card["action"] == "task" and card["target"] == "vault"


# ---------------------------------------------------------------- the Ledger tab
def test_ledger_tab_refuses_task_add_but_not_task_update():
    from ultra.ledgertab import LedgerTab, LedgerTabWriter

    led = DemoLedgerCLI()
    led.cfg = Config(OFF)  # type: ignore[attr-defined]
    calls: list[list[str]] = []

    def run(argv: list[str], stdin: str) -> tuple[int, str]:
        calls.append(argv)
        return led.write(argv, stdin)

    store = Store()
    tab = LedgerTab(led, LedgerTabWriter(led, store, run=run), store, DemoAI(), "Ada")  # type: ignore[arg-type]
    with pytest.raises(ApiError, match="archive-only"):
        tab.r_write_review({}, {"action": "task_add", "summary": "x", "priority": "LOW"}, None)
    t = "aaaaaaaa-0000-4000-8000-000000000061"
    rv = tab.r_write_review({}, {"action": "task_update", "id": t, "status": "DONE"}, None)
    assert rv["token"] and calls == []


# ---------------------------------------------------------------- the session and HTTP
def _session(raw: dict[str, Any], live: Any = None) -> dict[str, Any]:
    from ultra.server import Api

    api = Api.__new__(Api)
    api.cfg = Config(raw)
    api.token, api.demo, api.live = "t", live is None, live
    return api.session({}, None, None)


def test_session_tells_the_page():
    assert _session({})["new_tasks"] is True
    s = _session(OFF)
    assert s["new_tasks"] is False and s["tasks_note"] == NOTE

    class Live:  # a live workspace answers from its desk (vault Task stays on)
        class desk:
            @staticmethod
            def tasks_off() -> bool:
                return False

        class ledger:
            enabled = False

        class slack:
            enabled = False

        class ai:
            enabled = False

        class vault:
            enabled = True

    assert _session(OFF, live=Live())["new_tasks"] is True


def test_stage_route_refuses_over_http():
    import http.client

    from ultra.server import build

    httpd, api = build(Config(OFF), 0, demo=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        h = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
        h["X-Ultra-Token"] = api.token
        for path, body in [
            ("/api/ledger/stage", {"action": "task", "key": "g-100"}),
            ("/api/ledger/stage-answer", {"action": "task", "text": "Follow up"}),
        ]:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("POST", path, json.dumps(body), h)
            r = c.getresponse()
            assert r.status == 403 and json.loads(r.read())["error"] == NOTE
        assert api.demo_writer.calls == []  # type: ignore[attr-defined]
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- the page
def test_page_hides_every_task_entry_point():
    app = src("app.js")
    assert "export const newTasksOn = () => S.newTasks !== false;" in app
    assert "S.newTasks = s.new_tasks !== false;" in app
    # thread toolbar, row button, row menu, palette, bucket button, key t, help panel
    assert '${newTasksOn() ? `<button class="btn small" data-a="task"' in app
    assert '${task || !newTasksOn() ? "" : `<button class="ra" data-ra="task"' in app
    assert '!(r.id === "task" && (task || !newTasksOn()))' in app
    assert '...(newTasksOn() ? [{ t: "Task from the bucket"' in app
    assert "$('#bucket-acts [data-act=\"task\"]'); if (bt) bt.hidden = true;" in app
    assert 'hideKey("t");' in app
    keys = src("keys.js")
    assert "export function hideKey(k)" in keys and "KEYS.filter((k) => !HIDDEN.has(k[0]))" in keys
    # the stage helpers refuse too (Board Wait, Ask "Task from it", drag onto Task)
    led = src("ledger.js")
    assert led.count("!newTasksOn()) { toast(tasksOffNote()") == 2
    assert '${newTasksOn() ? `<button class="btn tiny" data-ta="task"' in src("ask.js")
    assert 'col === "mine" && newTasksOn()' in src("board.js")
    lt = src("ltab.js")
    assert '...(newTasksOn() ? [["task_add", "Task"]] : [])' in lt
    assert '"Tasks (archive)"' in lt


def test_work_day_labels_the_archive():
    day = src("day.js")
    assert "const arch = !P && !newTasksOn();" in day
    assert "Nexus tasks (archive): overdue" in day


def test_thread_toolbar_test_still_sees_the_task_button():
    """test_v150_calm reads data-a values from the toolbar source; Task is still there."""
    app = src("app.js")
    i = app.index('<div class="thread-acts">')
    assert 'data-a="task"' in app[i : app.index("</div>", i)]
    assert re.search(r'if \(e\.key === "t"\) \{ e\.preventDefault\(\); click\("task"\)', app)
    _m("x")  # shared helper import kept honest
