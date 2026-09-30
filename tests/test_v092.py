"""v0.9.2: every AI call carries the real current date/time; one model, no silent fallback.

Operator rules (2026-09-30): "all generate AI calls go to gemini 3.8 flash" and "the day
page and check-plan, in fact most other things, need to check the current date and
time before acting".
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ultra import ai as ai_mod
from ultra.ai import AI
from ultra.config import Config
from ultra.day import Day
from ultra.store import Store


class _Resp:
    def __init__(self, text: str) -> None:
        self.text = text
        self.candidates = [type("C", (), {"finish_reason": "STOP"})()]
        self.usage_metadata = None


class _Models:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, model: str, contents: Any, config: Any) -> _Resp:
        self.calls.append({"model": model, "system": config.system_instruction})
        return _Resp("ok")


def _ai(monkeypatch, cfg: dict[str, Any] | None = None) -> tuple[AI, _Models]:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-real")
    monkeypatch.setattr(ai_mod, "load_env_file", lambda: {})
    a = AI(Config(cfg or {"calendar": {"timezone": "America/New_York"}}))
    m = _Models()
    a._client = type("Client", (), {"models": m})()
    return a, m


def test_default_model_is_38_flash_and_there_is_no_fallback(monkeypatch):
    a, _ = _ai(monkeypatch, {"ai": {"fallback_model": "gemini-3.5-flash"}})
    assert a.model == "gemini-3.8-flash"
    assert a.fallback == ""  # a configured fallback is ignored by rule


def test_every_generation_starts_with_the_real_clock(monkeypatch):
    a, m = _ai(monkeypatch)
    a._gen("hello", "SYSTEM RULES")
    sys = m.calls[0]["system"]
    now = dt.datetime.now(ZoneInfo("America/New_York"))
    assert sys.startswith("Current local date and time: ")
    assert now.strftime("%Y-%m-%d") in sys and "America/New_York" in sys
    assert sys.endswith("SYSTEM RULES")
    assert m.calls[0]["model"] == "gemini-3.8-flash" and len(m.calls) == 1


def test_clock_is_read_at_call_time_not_at_startup(monkeypatch):
    a, m = _ai(monkeypatch)
    real = dt.datetime

    class Fake(real):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return real(2031, 1, 2, 9, 30, tzinfo=tz)

    monkeypatch.setattr(ai_mod.dt, "datetime", Fake)
    a._gen("x", "y")
    assert "Thursday 2031-01-02 09:30" in m.calls[0]["system"]


def test_draft_and_summary_paths_also_carry_the_clock(monkeypatch):
    a, m = _ai(monkeypatch)
    a.draft("thread", "Ada", "reply")
    a.summary("thread", "Ada")
    assert all(c["system"].startswith("Current local date and time: ") for c in m.calls)


def test_failure_is_reported_not_retried_on_another_model(monkeypatch):
    a, m = _ai(monkeypatch)

    def boom(model: str, contents: Any, config: Any) -> None:
        m.calls.append({"model": model})
        raise RuntimeError("quota")

    m.generate_content = boom  # type: ignore[method-assign]
    with pytest.raises(ai_mod.AIError):
        a._gen("x", "y")
    assert [c["model"] for c in m.calls] == ["gemini-3.8-flash"]


# ---------------------------------------------------------------- Day
def _day(tmp_path: Path, ai: Any = None) -> Day:
    return Day(
        Store(tmp_path / "s.db"),
        ZoneInfo("America/New_York"),
        lambda d: {"events": []},
        lambda: [],
        lambda: {},
        ai=ai,
        operator="Ada",
    )


def test_now_endpoint_reads_the_clock(tmp_path):
    d = _day(tmp_path)
    n = d.r_now({}, None, None)  # type: ignore[arg-type]
    real = dt.datetime.now(ZoneInfo("America/New_York"))
    assert n["date"] == real.date().isoformat() and n["tz"] == "America/New_York"
    assert 0 <= n["hour"] <= 23 and n["weekday"] == real.strftime("%A")


def test_plan_carries_now_and_the_ai_note_is_told_the_time(tmp_path):
    seen: dict[str, str] = {}

    class FakeAI:
        enabled = True

        def _gen(self, prompt: str, system: str, *a: Any, **k: Any) -> Any:
            seen["system"] = system
            return type("R", (), {"text": "- ok", "model": "m", "tokens": 1, "seconds": 0.1})()

    d = _day(tmp_path, FakeAI())
    p = d.r_plan({}, None, None)  # type: ignore[arg-type]
    assert p["now"]["date"] == p["day"]
    d.r_plan_note({}, {}, None)  # type: ignore[arg-type]
    assert seen["system"].startswith(f"It is now {p['now']['weekday']} {p['now']['date']}")
    assert "rest of today only" in seen["system"]
