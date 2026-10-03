"""v1.1 Inbox Tidy: the rule, the preview token, the run guard and Undo."""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ultra.server import ApiError
from ultra.store import Store
from ultra.tidy import Tidy, plan

NY = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 10, 1, 15, 0, tzinfo=NY)
DAY = 86400_000
NOW_MS = NOW.timestamp() * 1000
TODAY_MS = dt.datetime(2026, 10, 1, 0, 0, tzinfo=NY).timestamp() * 1000
M = re.match("x", "x")


def row(key: str, court: str, age_days: float, source: str = "email", **kw: Any) -> dict[str, Any]:
    return {
        "key": key,
        "source": source,
        "from": "Someone",
        "subject": f"subject {key}",
        "court": court,
        "badges": kw.pop("badges", []),
        "ts_ms": NOW_MS - age_days * DAY,
        **kw,
    }


def items() -> list[dict[str, Any]]:
    return [
        row("g-low0001", "LOW", 0.5),  # bulk, today -> keep (activity today)
        row("g-low0002", "LOW", 2),  # bulk -> archive
        row("g-mine001", "MINE", 30),  # your move -> keep however old
        row("g-vip0001", "WAITING", 30, badges=["VIP"]),  # VIP -> keep
        row("g-rdy0001", "WAITING", 30, badges=["READY"]),  # READY -> keep
        row("g-wait001", "WAITING", 10),  # waiting, old -> archive
        row("g-wait002", "WAITING", 3),  # waiting, new -> keep
        row("g-fyi0001", "FYI", 9),  # old fyi -> archive
        row("g-watch01", "WAITING", 40),  # on Watching -> keep
        row(
            "k-RITM0000001", "MINE", 20, source="ticket", threads=["tkt00001"], badges=["ASSIGNED"]
        ),
        row("k-RITM0000002", "LOW", 20, source="ticket", threads=["tkt00002", "tkt00003"]),
        row("s-C1:1.2", "MINE", 50, source="slack"),  # never touched
        row("t-1234", "TASK", 50, source="task"),  # never touched
        row("g-bad/../x", "LOW", 50),  # malformed id -> skipped
        # Slack/task rows whose ids happen to look like Gmail ids are still never touched
        row("g-slack001", "LOW", 50, source="slack"),
        row("g-task0001", "LOW", 50, source="task", threads=["task0001"]),
    ]


def by_key(p: dict[str, list[dict[str, Any]]]) -> dict[str, tuple[str, str]]:
    out = {}
    for side in ("archive", "keep"):
        for r in p[side]:
            out[r["key"]] = (side, r["reason"])
    return out


def test_rule():
    got = by_key(plan(items(), 7, NOW_MS, {"g-watch01"}, TODAY_MS))
    assert got["g-low0001"] == ("keep", "activity today")
    assert got["g-low0002"] == ("archive", "automated or bulk sender")
    assert got["g-mine001"] == ("keep", "your move")
    assert got["g-vip0001"] == ("keep", "VIP")
    assert got["g-rdy0001"] == ("keep", "a peer said it is ready")
    assert got["g-wait001"] == ("archive", "waiting on them, 10 days old")
    assert got["g-wait002"] == ("keep", "newer than 7 days")
    assert got["g-fyi0001"] == ("archive", "no action, 9 days old")
    assert got["g-watch01"] == ("keep", "on your Board's Watching list")
    assert got["k-RITM0000001"] == ("keep", "ticket assigned to you")
    assert got["k-RITM0000002"][0] == "archive"
    assert not {"s-C1:1.2", "t-1234", "g-bad/../x", "g-slack001", "g-task0001"} & set(got)


def test_days_moves_the_line():
    got = by_key(plan(items(), 14, NOW_MS, set(), TODAY_MS))
    assert got["g-wait001"] == ("keep", "newer than 14 days")
    assert got["g-watch01"][0] == "archive"  # not watched in this call


class Fake:
    def __init__(self) -> None:
        self.archived: list[list[str]] = []
        self.restored: list[list[str]] = []

    def archive(self, tids: list[str]) -> list[str]:
        self.archived.append(list(tids))
        return list(tids)

    def unarchive(self, tids: list[str]) -> list[str]:
        self.restored.append(list(tids))
        return list(tids)


@pytest.fixture
def tidy(tmp_path: Path) -> Tidy:
    f = Fake()
    t = Tidy(
        Store(tmp_path / "t.db"),
        items,
        f.archive,
        f.unarchive,
        lambda: {"g-watch01"},
        NY,
        now=lambda: NOW,
    )
    t.fake = f  # type: ignore[attr-defined]
    return t


def test_preview_lists_and_counts_threads(tidy):
    p = tidy.r_preview({}, {"days": 7}, M)
    assert {r["key"] for r in p["archive"]} == {
        "g-low0002",
        "g-wait001",
        "g-fyi0001",
        "k-RITM0000002",
    }
    assert p["thread_count"] == 5  # the ticket row holds two threads
    assert tidy.fake.archived == []  # preview never archives


def test_run_archives_only_ticked_threads_then_undo(tidy):
    p = tidy.r_preview({}, {}, M)
    r = tidy.r_run({}, {"token": p["token"], "threads": ["low0002", "wait001"]}, M)
    assert tidy.fake.archived == [["low0002", "wait001"]]
    assert r["archived"] == ["low0002", "wait001"]
    j = tidy.store.journal_recent(1, "tidy")[0]
    assert j["action"] == "tidy" and "subject" not in str(j)
    u = tidy.r_undo({}, {"undo": r["undo"]}, M)
    assert tidy.fake.restored == [["low0002", "wait001"]] and u["unarchived"] == [
        "low0002",
        "wait001",
    ]
    with pytest.raises(ApiError, match="nothing to undo"):
        tidy.r_undo({}, {"undo": r["undo"]}, M)  # one Undo per run


def test_token_is_single_use(tidy):
    p = tidy.r_preview({}, {}, M)
    tidy.r_run({}, {"token": p["token"], "threads": ["low0002"]}, M)
    with pytest.raises(ApiError, match="expired or was already used"):
        tidy.r_run({}, {"token": p["token"], "threads": ["wait001"]}, M)
    assert tidy.fake.archived == [["low0002"]]


def test_run_refuses_threads_outside_the_preview(tidy):
    p = tidy.r_preview({}, {}, M)
    with pytest.raises(ApiError, match="not in the preview"):
        tidy.r_run({}, {"token": p["token"], "threads": ["low0002", "mine001"]}, M)  # MINE was kept
    assert tidy.fake.archived == []


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        ({"token": "x", "threads": []}, "nothing selected"),
        ({"token": "x", "threads": ["../etc"]}, "Gmail thread ids"),
        ({"token": "nope", "threads": ["low0002"]}, "expired or was already used"),
    ],
)
def test_run_validation(tidy, body, msg):
    with pytest.raises(ApiError, match=msg):
        tidy.r_run({}, body, M)
    assert tidy.fake.archived == []


@pytest.mark.parametrize("days", [0, 366, "x", -3])
def test_days_validation(tidy, days):
    with pytest.raises(ApiError, match="days"):
        tidy.r_preview({}, {"days": days}, M)


def test_expired_preview_is_refused(tidy, monkeypatch):
    import ultra.tidy as mod

    p = tidy.r_preview({}, {}, M)
    real = mod.time.time
    monkeypatch.setattr(mod.time, "time", lambda: real() + mod.TOKEN_TTL + 1)
    with pytest.raises(ApiError, match="expired"):
        tidy.r_run({}, {"token": p["token"], "threads": ["low0002"]}, M)


def test_count_is_read_only_and_matches_the_preview(tidy):
    """v1.7: GET /api/mail/tidy/count feeds the calm stream's one-line suggestion."""
    from ultra.tidy import SUGGEST_AT

    c = tidy.r_count({}, None, M)
    assert c["suggest_at"] == SUGGEST_AT == 10
    assert not tidy.previews  # no token: count can never authorise a run
    assert c["count"] == tidy.r_preview({}, {}, M)["thread_count"]
    assert tidy.fake.archived == []
