"""v0.5.1 tests: tasks in the stream, task actions, Slack done marks."""

from __future__ import annotations

import time
from typing import Any

import pytest

from ultra.config import Config
from ultra.ledger_write import LedgerWriter, WriteError
from ultra.store import Store
from ultra.tasks import (
    slack_done,
    slack_undone,
    slack_visible,
    snooze_task,
    task_rows,
    unsnooze_task,
)

T1 = "55555555-5555-4555-8555-555555555551"
T2 = "55555555-5555-4555-8555-555555555552"
T3 = "55555555-5555-4555-8555-555555555553"


def _tasks() -> list[dict[str, Any]]:
    return [
        {"id": T1, "summary": "low one", "status": "TODO", "priority": "LOW"},
        {"id": T2, "summary": "critical one", "status": "BLOCKED", "priority": "CRITICAL"},
        {"id": T3, "summary": "done one", "status": "DONE", "priority": "HIGH"},
    ]


def test_task_rows_colour_data_and_order(tmp_path):
    s = Store(tmp_path / "s.db")
    rows = task_rows(_tasks(), s)
    assert [r["key"] for r in rows] == [f"t-{T1}", f"t-{T2}"]  # DONE never shows
    by = {r["key"]: r for r in rows}
    crit = by[f"t-{T2}"]
    assert crit["court"] == "TASK" and crit["source"] == "task"
    assert crit["priority"] == "CRITICAL" and crit["status"] == "BLOCKED"
    assert {"CRITICAL", "BLOCKED"} <= set(crit["badges"])
    # CRITICAL sorts above LOW in a newest-first stream
    assert crit["ts_ms"] > by[f"t-{T1}"]["ts_ms"]


def test_snooze_hides_until_time_or_unsnooze(tmp_path):
    s = Store(tmp_path / "s.db")
    snooze_task(s, T1, 3)
    assert [r["key"] for r in task_rows(_tasks(), s)] == [f"t-{T2}"]
    assert [r["key"] for r in task_rows(_tasks(), s, now=time.time() + 4 * 86400)] == [
        f"t-{T1}",
        f"t-{T2}",
    ]
    unsnooze_task(s, T1)
    assert len(task_rows(_tasks(), s)) == 2


def test_slack_done_hides_until_newer_message(tmp_path):
    s = Store(tmp_path / "s.db")
    row = {"key": "s-C1", "ts_ms": 1000}
    assert slack_visible(s, row)
    slack_done(s, "s-C1", 1000)
    assert not slack_visible(s, row)
    assert slack_visible(s, {"key": "s-C1", "ts_ms": 2000})  # someone wrote again
    slack_undone(s, "s-C1")
    assert slack_visible(s, row)


FAKE = r"""
import json, os, sys
open(os.environ["FAKE_LOG"], "a").write(json.dumps(sys.argv[1:]) + "\n")
print("Updated Task: x")
"""


def test_task_priority_and_status_commands(tmp_path, monkeypatch):
    b = tmp_path / "nexus"
    b.write_text("#!/usr/bin/env python3\n" + FAKE)
    b.chmod(0o755)
    log = tmp_path / "log.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    w = LedgerWriter(Config({"ledger": {"binary": str(b)}}), Store(tmp_path / "s.db"))
    assert w.task_priority(T1, "HIGH")["ok"]
    assert w.task_status(T1, "DONE")["ok"]
    import json

    calls = [json.loads(x) for x in log.read_text().splitlines()]
    assert calls == [
        ["tasks", "update", T1, "--priority", "HIGH"],
        ["tasks", "update", T1, "--status", "DONE"],
    ]
    for bad in (("adal", "HIGH"), (T1, "URGENT")):
        with pytest.raises(WriteError):
            w.task_priority(*bad)


def _demo_api():
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    httpd.server_close()
    return api


def _call(api, method: str, path: str, body: Any = None) -> Any:
    return api.dispatch(method, path, {}, body)[1]


def test_demo_tasks_filter_complete_and_slack_done():
    api = _demo_api()
    r = _call(api, "GET", "/api/stream", None)
    assert r["counts"]["tasks"] == 4
    tasks = api.dispatch("GET", "/api/stream", {"filter": ["tasks"]}, None)[1]["items"]
    assert tasks[0]["priority"] == "CRITICAL"  # most urgent first
    tid = tasks[0]["key"][2:]
    assert _call(api, "POST", "/api/task/action", {"id": tid, "action": "complete"})["ok"]
    after = api.dispatch("GET", "/api/stream", {"filter": ["tasks"]}, None)[1]
    assert after["counts"]["tasks"] == 3 and all(t["key"] != f"t-{tid}" for t in after["items"])
    slack = api.dispatch("GET", "/api/stream", {"filter": ["slack"]}, None)[1]["items"]
    _call(api, "POST", "/api/slack/done", {"key": slack[0]["key"]})
    assert api.dispatch("GET", "/api/stream", {"filter": ["slack"]}, None)[1]["items"] == []
    _call(api, "POST", "/api/slack/undone", {"key": slack[0]["key"]})
    assert len(api.dispatch("GET", "/api/stream", {"filter": ["slack"]}, None)[1]["items"]) == 1


def test_blocked_tasks_show_under_waiting_and_tasks_but_not_mine(tmp_path):
    """The operator files "waiting on someone" as BLOCKED; those belong in Waiting."""
    from ultra.live import FILTERS

    s = Store(tmp_path / "s.db")
    rows = {r["status"]: r for r in task_rows(_tasks(), s)}
    blocked, todo = rows["BLOCKED"], rows["TODO"]
    assert FILTERS["waiting"](blocked) and not FILTERS["waiting"](todo)
    assert FILTERS["tasks"](blocked) and FILTERS["tasks"](todo)
    assert not FILTERS["mine"](blocked) and not FILTERS["mine"](todo)
    mail_waiting = {"court": "WAITING", "source": "mail"}
    mail_mine = {"court": "MINE", "source": "mail", "status": "BLOCKED"}
    assert FILTERS["waiting"](mail_waiting) and not FILTERS["waiting"](mail_mine)
