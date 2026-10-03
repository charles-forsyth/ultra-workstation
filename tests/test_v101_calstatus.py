"""v1.0.1: the status bar's Calendar light reflects a real calendar read."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from ultra import google_auth
from ultra.calendar import Calendar, CalendarError
from ultra.config import Config
from ultra.store import Store

ROOT = Path(__file__).resolve().parents[1]


class Svc:
    def __init__(self, fail: Exception | None = None) -> None:
        self.fail = fail
        self.calls = 0

    def events(self) -> Any:
        svc = self

        class E:
            def list(self, **kw: Any) -> Any:
                class R:
                    def execute(self) -> Any:
                        svc.calls += 1
                        if svc.fail:
                            raise svc.fail
                        return {"items": []}

                return R()

        return E()


def cal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, svc: Svc) -> Calendar:
    c = Calendar(Config({"operator": {"timezone": "America/New_York"}}), Store(tmp_path / "s.db"))
    monkeypatch.setattr(c, "_svc", lambda: svc)
    return c


def test_state_starts_unknown_then_ok_with_a_time(tmp_path, monkeypatch):
    c = cal(tmp_path, monkeypatch, Svc())
    assert c.state == {"ok": None, "error": "", "at": None}
    c.day("2026-10-01", fresh=True)
    assert c.state["ok"] is True and abs(c.state["at"] - time.time()) < 5


def test_read_error_turns_it_red(tmp_path, monkeypatch):
    c = cal(tmp_path, monkeypatch, Svc(RuntimeError("quota")))
    with pytest.raises(CalendarError):
        c.day("2026-10-01", fresh=True)
    assert c.state["ok"] is False and "quota" in c.state["error"] and c.state["at"]


def test_missing_token_turns_it_red_with_the_fix(tmp_path, monkeypatch):
    c = cal(tmp_path, monkeypatch, Svc(google_auth.AuthNeeded("calendar", "no token")))
    with pytest.raises(google_auth.AuthNeeded):
        c.day("2026-10-01", fresh=True)
    assert c.state["ok"] is False and c.state["auth"] == "calendar"
    assert "ultra auth google --capability calendar" in c.state["error"]


def test_status_poll_checks_the_calendar_and_reports_age(tmp_path, monkeypatch):
    from ultra import live as live_mod

    svc = Svc()
    c = cal(tmp_path, monkeypatch, svc)

    class L:  # just the parts _kick_stale / r_status touch
        _check_calendar = live_mod.Live._check_calendar
        _kick_stale = live_mod.Live._kick_stale

        def __init__(self) -> None:
            self.calendar = c
            self.ran: list[str] = []
            self.ledger = type("Ld", (), {"enabled": False})()
            self.slack = type("Sl", (), {"enabled": False, "items": lambda self: ([], 1.0)})()
            self.mail = type("M", (), {"stream": lambda self, n: ({}, 1.0)})()
            self.poll_mail = 999
            self.poll_slack = 999
            self.show_tasks = False
            self.mcp: dict[str, Any] = {}  # v1.3: no MCP servers configured

        def _job(self, name: str, fn: Any, *a: Any) -> None:
            self.ran.append(name)
            fn(*a)

    lv = L()
    lv._kick_stale()
    assert lv.ran == ["calendar"] and svc.calls == 1 and c.state["ok"] is True
    lv._kick_stale()  # fresh: no second read within 10 minutes
    assert lv.ran == ["calendar"] and svc.calls == 1
    c.state["at"] = time.time() - live_mod.CAL_CHECK_SECONDS - 1
    lv._kick_stale()
    assert svc.calls == 2


def test_page_reads_calendar_status():
    js = (ROOT / "src/ultra/static/app.js").read_text()
    assert 'calendar: "sb-cal"' in js
    assert "Calendar arrives in v0.6" not in js
