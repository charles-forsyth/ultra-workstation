"""v0.9.1: task due dates (ledger 0.1.206+) end to end in Ultra.

- The writer passes --due and reads the new id from --json; strict YYYY-MM-DD only.
- Setting / clearing a due date goes by exact UUID and is read back before success.
- The task card validates the due date before anything runs.
- Stream rows show OVERDUE / DUE TODAY / DUE SOON and sort overdue first.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest
from test_v04 import BEN, FAKE, _calls, _desk, _wait

from ultra.config import Config
from ultra.ledger_write import LedgerWriter, WriteError, check_due
from ultra.store import Store
from ultra.tasks import task_rows

T1 = "55555555-5555-4555-8555-555555555551"


@pytest.fixture
def writer(tmp_path: Path, monkeypatch) -> tuple[LedgerWriter, Path]:
    b = tmp_path / "nexus"
    b.write_text("#!/usr/bin/env python3\n" + FAKE)
    b.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    return LedgerWriter(Config({"ledger": {"binary": str(b)}}), Store(tmp_path / "s.db")), log


# ---------------------------------------------------------------- validation
def test_check_due_accepts_only_real_absolute_dates():
    assert check_due("2026-10-05") == "2026-10-05"
    assert check_due(" 2026-10-05 ") == "2026-10-05"
    for bad in [
        "+3d",
        "tomorrow",
        "2026-02-30",
        "2026-10-5",
        "10/05/2026",
        "1999-01-01",
        "2026-10-05 09:00",
        "2026-10-05; rm -rf",
        "--clear-due",
        "",
    ]:
        with pytest.raises(WriteError):
            check_due(bad)


# ---------------------------------------------------------------- the writer
def test_task_add_passes_due_and_reads_the_id_from_json(writer):
    w, log = writer
    r = w.task_add("Send the plan", "HIGH", "2026-10-09")
    assert r["id"] == "bbbbbbbb-0000-4000-8000-000000000002"  # not the noise line
    assert r["due_date"] == "2026-10-09"
    argv = _calls(log)[0]["argv"]
    assert argv == [
        "tasks",
        "add",
        "--priority",
        "HIGH",
        "--json",
        "--due",
        "2026-10-09",
        "--",
        "Send the plan",
    ]
    j = w.store.journal_recent(1, "ledger_")[0]
    assert j["detail"]["due"] == "2026-10-09" and "Send the plan" not in json.dumps(j["detail"])


def test_task_add_without_due_sends_no_due_flag(writer):
    w, log = writer
    r = w.task_add("No date", "LOW")
    assert r["id"] and r["due_date"] is None
    assert "--due" not in _calls(log)[0]["argv"]


def test_task_add_with_a_bad_due_runs_nothing(writer):
    w, log = writer
    with pytest.raises(WriteError):
        w.task_add("x", "LOW", "next week")
    assert not log.exists()


def test_task_add_falls_back_to_the_old_output(tmp_path, monkeypatch):
    """An older ledger without --json still yields the id from its prose line."""
    b = tmp_path / "nexus"
    b.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        "print('Added Task: x (ID: cccccccc-0000-4000-8000-000000000003) -> Assigned to Me')\n"
    )
    b.chmod(0o755)
    w = LedgerWriter(Config({"ledger": {"binary": str(b)}}), Store(tmp_path / "s.db"))
    assert w.task_add("x", "LOW")["id"] == "cccccccc-0000-4000-8000-000000000003"


def test_task_due_set_and_clear_by_exact_uuid(writer):
    w, log = writer
    assert w.task_due(T1, "2026-10-09")["ok"]
    assert w.task_due(T1, "")["ok"]
    calls = [c["argv"] for c in _calls(log)]
    assert calls == [
        ["tasks", "update", T1, "--due", "2026-10-09"],
        ["tasks", "update", T1, "--clear-due"],
    ]
    acts = [(j["action"], j["detail"]["due"]) for j in w.store.journal_recent(5, "ledger_")]
    assert ("ledger_task_due", "2026-10-09") in acts and ("ledger_task_due", None) in acts


def test_task_due_refuses_non_uuids_and_bad_dates_before_running(writer):
    w, log = writer
    for tid in ["Send the plan", "5555", T1 + "x"]:
        with pytest.raises(WriteError):
            w.task_due(tid, "2026-10-09")
    with pytest.raises(WriteError):
        w.task_due(T1, "+3d")
    assert not log.exists()


def test_task_due_not_found_is_a_failure(writer):
    w, _ = writer
    r = w.task_due("00000000-0000-4000-8000-000000000000", "2026-10-09")
    assert r["ok"] is False


# ---------------------------------------------------------------- desk card
def test_card_commit_with_due_confirms_it(tmp_path):
    desk, w = _desk(tmp_path)
    card = desk.r_stage({}, {"action": "task", "key": "g-t1"}, None)
    chips = [c for c in card["chips"] if c["id"] == BEN]
    desk.r_commit(
        {},
        {
            "card": card["id"],
            "text": "Follow up",
            "chips": chips,
            "priority": "HIGH",
            "due": "2026-10-09",
        },
        None,
    )
    p = _wait(desk, card["id"])
    assert p["state"] == "done"
    assert w.calls[0] == (
        "task_add",
        {"summary": "Follow up", "priority": "HIGH", "due": "2026-10-09"},
    )
    assert any("Due date 2026-10-09 confirmed" in s["text"] and s["ok"] for s in p["steps"])


def test_card_commit_with_a_bad_due_is_refused_and_card_released(tmp_path):
    desk, w = _desk(tmp_path)
    card = desk.r_stage({}, {"action": "task", "key": "g-t1"}, None)
    with pytest.raises(Exception, match="due date"):
        desk.r_commit({}, {"card": card["id"], "text": "x", "chips": [], "due": "friday"}, None)
    assert w.calls == []
    desk.r_commit({}, {"card": card["id"], "text": "x", "chips": [], "due": ""}, None)
    assert _wait(desk, card["id"])["state"] == "done"
    assert w.calls[0][1]["due"] == ""


def test_log_cards_ignore_a_due_field(tmp_path):
    desk, w = _desk(tmp_path)
    card = desk.r_stage({}, {"action": "log", "key": "g-t1"}, None)
    desk.r_commit(
        {},
        {
            "card": card["id"],
            "text": "x",
            "chips": [],
            "date": "2026-09-29 10:00",
            "due": "garbage",
        },
        None,
    )
    assert _wait(desk, card["id"])["state"] == "done"
    assert [c[0] for c in w.calls] == ["log"]


# ---------------------------------------------------------------- live route read-back
class _Writer:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.calls = ok, []

    def task_due(self, tid: str, due: str) -> dict[str, Any]:
        self.calls.append((tid, due))
        return {"ok": self.ok, "rc": 0}


class _Ledger:
    def __init__(self, due: str) -> None:
        self.due = due

    def task(self, tid: str) -> dict[str, Any]:
        return {"id": tid, "due_date": self.due}


def _live(writer: Any, ledger: Any, tmp_path: Path) -> Any:
    from ultra.live import Live

    lv = Live.__new__(Live)
    lv.writer, lv.ledger, lv.store = writer, ledger, Store(tmp_path / "l.db")
    lv._job = lambda *a, **k: None  # type: ignore[method-assign]
    return lv


def test_live_due_action_reads_back_before_success(tmp_path):
    lv = _live(_Writer(), _Ledger("2026-10-09 00:00:00"), tmp_path)
    r = lv.r_task_action({}, {"id": T1, "action": "due", "due": "2026-10-09"}, None)
    assert r["ok"] and r["due_date"] == "2026-10-09"


def test_live_due_action_mismatch_is_reported_as_failure(tmp_path):
    lv = _live(_Writer(), _Ledger("2026-08-25 00:00:00"), tmp_path)
    r = lv.r_task_action({}, {"id": T1, "action": "due", "due": "2026-10-09"}, None)
    assert r["ok"] is False and "2026-08-25" in r["output_tail"]
    lv = _live(_Writer(), _Ledger(""), tmp_path)
    assert lv.r_task_action({}, {"id": T1, "action": "due", "due": ""}, None)["ok"]


def test_live_due_action_rejects_a_bad_id(tmp_path):
    lv = _live(_Writer(), _Ledger(""), tmp_path)
    with pytest.raises(Exception, match="UUID"):
        lv.r_task_action({}, {"id": "Send the plan", "action": "due", "due": "2026-10-09"}, None)
    assert lv.writer.calls == []


# ---------------------------------------------------------------- stream rows
def test_stream_rows_badge_and_sort_by_due(tmp_path):
    s = Store(tmp_path / "t.db")
    now = time.time()
    today = dt.date.today()
    day = lambda n: (today + dt.timedelta(days=n)).isoformat() + " 00:00:00"  # noqa: E731
    tasks = [
        {
            "id": "a" * 8 + "-0000-4000-8000-000000000001",
            "summary": "high later",
            "priority": "HIGH",
            "status": "TODO",
            "due_date": day(20),
        },
        {
            "id": "a" * 8 + "-0000-4000-8000-000000000002",
            "summary": "medium overdue",
            "priority": "MEDIUM",
            "status": "TODO",
            "due_date": day(-3),
        },
        {
            "id": "a" * 8 + "-0000-4000-8000-000000000003",
            "summary": "medium today",
            "priority": "MEDIUM",
            "status": "TODO",
            "due_date": day(0),
        },
        {
            "id": "a" * 8 + "-0000-4000-8000-000000000004",
            "summary": "medium soon",
            "priority": "MEDIUM",
            "status": "TODO",
            "due_date": day(2),
        },
        {
            "id": "a" * 8 + "-0000-4000-8000-000000000005",
            "summary": "medium none",
            "priority": "MEDIUM",
            "status": "TODO",
        },
    ]
    rows = {r["subject"]: r for r in task_rows(tasks, s, now=now)}
    assert rows["medium overdue"]["badges"][0] == "OVERDUE"
    assert rows["medium today"]["badges"][0] == "DUE TODAY"
    assert "DUE SOON" in rows["medium soon"]["badges"]
    assert not any(b.startswith(("OVERDUE", "DUE")) for b in rows["high later"]["badges"])
    assert rows["medium none"]["due_date"] == "" and "due" not in rows["medium none"]["snippet"]
    assert rows["medium overdue"]["snippet"].endswith(f"due {day(-3)[:10]}")
    order = [r["subject"] for r in sorted(rows.values(), key=lambda r: -r["ts_ms"])]
    # HIGH first, then overdue, then due today, then the rest of MEDIUM
    assert order[:3] == ["high later", "medium overdue", "medium today"]


def test_demo_due_action_validates(tmp_path):
    os.environ.setdefault("ULTRA_DEMO", "1")
    from ultra.demo import DemoLedger, DemoWriter

    w = DemoWriter(DemoLedger())
    assert w.task_due(T1, "2026-10-09")["ok"]
    with pytest.raises(WriteError):
        w.task_due(T1, "someday")
    assert w.task_add("x", "LOW", "2026-10-09")["due_date"] == "2026-10-09"
