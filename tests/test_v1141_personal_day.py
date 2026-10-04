"""v1.14.1: the Personal workspace's Day view (check-in plan, end-of-day report) is built
from the notes vault, never from the work ledger (SPEC 8.11)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from ultra.day import Day
from ultra.ledger import Ledger, LedgerError

ROOT = Path(__file__).resolve().parents[1]
TZ = dt.timezone(dt.timedelta(hours=-4))
DAY = "2026-10-03"

OVERVIEW = {
    "enabled": True,
    "areas": [{"key": "home", "icon": "H"}, {"key": "garden", "icon": "G"}],
    "due": [
        {
            "text": "Fix the latch",
            "due": "2026-10-01",
            "days": -2,
            "area": "home",
            "path": "Tasks.md",
        },
        {"text": "Drop off the camper", "due": DAY, "days": 0, "area": "home", "path": "Tasks.md"},
        {"text": "Prep the bed", "due": "2026-10-05", "days": 2, "area": "garden", "path": "G.md"},
    ],
    "day": {"log": ["- 7:30 AM Fed the hens"], "daily": "Daily Notes/x.md", "checkins": []},
}


class Store:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []

    def journal_between(self, t0: float, t1: float) -> list[dict[str, Any]]:
        return self.rows


def _day(life: Any = None, tasks: Any = None, rows: Any = None) -> Day:
    return Day(
        Store(rows),
        TZ,
        lambda d: {"events": []},
        lambda: [],
        tasks or (lambda: {"w1": {"id": "w1", "summary": "Work thing", "priority": "HIGH"}}),
        life_fn=life,
    )


def test_personal_plan_uses_the_notes_not_the_ledger():
    def boom() -> dict:
        raise AssertionError("the ledger must not be asked in Personal")

    d = _day(life=lambda: OVERVIEW, tasks=boom)
    p = d.plan(DAY)
    assert p["personal"] is True
    assert [t["summary"] for t in p["overdue"]] == ["H Fix the latch"]
    assert [t["summary"] for t in p["due_today"]] == ["H Drop off the camper"]
    assert [t["summary"] for t in p["top_tasks"]] == ["G Prep the bed"]
    assert p["blocks"] == [] or all(not b["key"].startswith("t-") for b in p["blocks"])
    text = d.plan_text(p)
    assert "Overdue:\n- H Fix the latch (due 2026-10-01)" in text
    assert "Coming up:\n- G Prep the bed (2026-10-05)" in text
    assert "Logged today:\n- 7:30 AM Fed the hens" in text
    for work in ("Work thing", "HIGH", "High-priority", "ledger"):
        assert work not in text


def test_work_plan_is_unchanged():
    p = _day().plan(DAY)
    assert p["personal"] is False and [t["summary"] for t in p["top_tasks"]] == ["Work thing"]
    assert "High-priority tasks:" in _day().plan_text(p)


def test_long_log_lines_are_clipped_at_a_word():
    from ultra.day import _clip

    line = "word " * 60
    out = _clip(line.strip())
    assert len(out) <= 183 and out.endswith("word...")
    assert _clip("short") == "short"


def test_notes_unavailable_is_a_warning_not_a_crash():
    def down() -> dict:
        raise RuntimeError("vault-mcp died")

    p = _day(life=down).plan(DAY)
    assert p["overdue"] == [] and any("Notes unavailable" in w for w in p["warnings"])


def test_personal_report_words():
    rows = [
        {
            "action": "sent",
            "ts": 1791070000,
            "ok": 1,
            "target": "x",
            "detail": '{"subject": "Re: hi"}',
        },
        {
            "action": "vault_task_done",
            "ts": 1791070100,
            "ok": 1,
            "target": "Tasks.md",
            "detail": "{}",
        },
        {"action": "archive", "ts": 1791070200, "ok": 1, "target": "y", "detail": "{}"},
    ]
    d = _day(life=lambda: OVERVIEW, rows=rows)
    r = d.report(DAY)
    assert r["personal"] is True and r["sections"]["Notes"][0]["text"] == "To-do ticked done"
    t = d.report_text(r)
    assert "Mail and notes: sent 1 email, archived 1, 1 note written from Ultra." in t
    assert "Logged today:\n- 7:30 AM Fed the hens" in t
    assert "Still open: 0 emails are my move; 1 overdue to-dos." in t
    for work in ("Slack", "interactions", "task states", "ledger"):
        assert work not in t


def test_a_ledger_that_is_off_never_reads(tmp_path: Path):
    from ultra.config import Config
    from ultra.store import Store as RealStore

    led = Ledger(Config({"ledger": {"enabled": False}}), RealStore(tmp_path / "s.db"))
    assert led.enabled is False
    with pytest.raises(LedgerError, match="off in this workspace"):
        led._run(["tasks", "list"])


def test_leftover_task_cache_is_ignored_when_the_ledger_is_off():
    src = (ROOT / "src/ultra/live.py").read_text()
    i = src.index("def _open_tasks_cached")
    assert "if not self.ledger.enabled" in src[i : i + 300]
    assert "self.day.life_fn = self.life_api.life.overview" in src
    assert "self.vault.store = self.store" in src


def test_day_page_words():
    js = (ROOT / "src/ultra/static/day.js").read_text()
    for s in ('"Coming up (two weeks)"', '"Logged today"', '"Save to notes"', '"On the calendar"'):
        assert s in js
    assert 'P ? "" : sec("Suggested focus blocks"' in js
    assert "openNote(n.dataset.note)" in js
