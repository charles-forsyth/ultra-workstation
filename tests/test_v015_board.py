"""v0.15 Board: columns, Watching flags, Done today, Nudge (one draft, never a send)."""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from ultra.board import Board
from ultra.compose import Composer
from ultra.config import Config
from ultra.server import ApiError
from ultra.store import Store

NY = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 10, 1, 10, 0, tzinfo=NY)
TID = "55555555-5555-4555-8555-555555555555"


def items() -> list[dict[str, Any]]:
    return [
        {
            "key": "g-a1",
            "source": "email",
            "from": "Ben",
            "addr": "ben@example.org",
            "subject": "Storage",
            "court": "MINE",
            "badges": [],
            "waiting_days": 2,
        },
        {
            "key": "g-a2",
            "source": "email",
            "from": "Cy",
            "addr": "cy@example.org",
            "subject": "Project done",
            "court": "MINE",
            "badges": ["READY"],
            "waiting_days": 0,
        },
        {
            "key": "g-w1",
            "source": "email",
            "from": "Dee",
            "addr": "dee@example.org",
            "subject": "Security review",
            "court": "WAITING",
            "badges": [],
            "waiting_days": 6,
        },
        {
            "key": "g-w2",
            "source": "email",
            "from": "Dee",
            "addr": "dee@example.org",
            "subject": "Firewall rule",
            "court": "WAITING",
            "badges": [],
            "waiting_days": 2,
        },
        {
            "key": "s-w3",
            "source": "slack",
            "from": "Dee",
            "addr": "dee@example.org",
            "subject": "DM",
            "court": "WAITING",
            "badges": [],
            "waiting_days": 1,
        },
        {
            "key": "g-w4",
            "source": "email",
            "from": "Eve",
            "addr": "eve@example.org",
            "subject": "Quote",
            "court": "WAITING",
            "badges": [],
            "waiting_days": 3,
        },
        {
            "key": f"t-{TID}",
            "source": "task",
            "from": "Task",
            "addr": "",
            "subject": "Waiting on vendor",
            "court": "TASK",
            "status": "BLOCKED",
            "badges": [],
        },
        {
            "key": "g-low",
            "source": "email",
            "from": "News",
            "addr": "n@x.org",
            "subject": "Weekly",
            "court": "LOW",
            "badges": [],
            "waiting_days": 0,
        },
    ]


@pytest.fixture
def board(tmp_path: Path) -> Board:
    store = Store(tmp_path / "b.db")
    comp = Composer(Config({}), store, {"ada@example.org"})
    clock = {"now": NOW}
    b = Board(
        store,
        items,
        NY,
        "Ada Example",
        compose=comp,
        from_addr="ada@example.org",
        my_addrs={"ada@example.org"},
        signature_fn=lambda: "Ada",
        now=lambda: clock["now"],
    )
    b._clock = clock  # type: ignore[attr-defined]
    return b


M = re.match("x", "x")


def test_columns_follow_the_court_rules(board):
    c = board.build()["columns"]
    assert [r["key"] for r in c["mine"]] == ["g-a2", "g-a1"]  # READY first
    groups = {g["who"]: g for g in c["waiting"]}
    assert [r["key"] for r in groups["Dee"]["items"]] == ["g-w1", "g-w2", "s-w3"]  # oldest first
    assert groups["Dee"]["oldest_days"] == 6 and groups["Dee"]["can_nudge"]
    assert c["waiting"][0]["who"] == "Dee"  # most overdue person first
    assert [r["key"] for r in groups["Blocked tasks"]["items"]] == [f"t-{TID}"]
    assert groups["Blocked tasks"]["can_nudge"] is False
    all_keys = json.dumps(c)
    assert "g-low" not in all_keys  # LOW never on the board
    assert board.build()["counts"] == {"mine": 2, "waiting": 5, "watching": 0, "done": 0}


def test_watch_moves_an_item_and_lapses_on_its_date(board):
    board.r_watch({}, {"key": "g-a1", "until": "2026-10-03", "note": "after the call"}, M)
    c = board.build()["columns"]
    assert [r["key"] for r in c["watching"]] == ["g-a1"]
    assert c["watching"][0]["watch"]["note"] == "after the call"
    assert "g-a1" not in [r["key"] for r in c["mine"]]
    board._clock["now"] = dt.datetime(2026, 10, 3, 8, 0, tzinfo=NY)  # the date comes
    c = board.build()["columns"]
    assert c["watching"] == [] and "g-a1" in [r["key"] for r in c["mine"]]


def test_unwatch_and_no_date_watch(board):
    board.r_watch({}, {"key": "g-w4"}, M)
    assert [r["key"] for r in board.build()["columns"]["watching"]] == ["g-w4"]
    board.r_unwatch({}, {"key": "g-w4"}, M)
    assert board.build()["columns"]["watching"] == []
    rows = board.store.journal_recent(5, "board_")
    assert [r["action"] for r in rows] == ["board_unwatch", "board_watch"]


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        ({"key": "../x"}, "bad item key"),
        ({"key": "g-a1", "until": "tomorrow"}, "YYYY-MM-DD"),
        ({"key": "g-a1", "until": "2026-10-01"}, "after today"),
        ({"key": "g-a1", "until": "2026-09-01"}, "after today"),
    ],
)
def test_watch_validation(board, body, msg):
    with pytest.raises(ApiError, match=msg):
        board.r_watch({}, body, M)


def test_done_today_reads_the_journal(board):
    s = board.store
    s.journal("archive", "a1", True, {"subject": "Storage"})
    s.journal("ledger_task_status", TID, True, {"status": "DONE"})
    s.journal("ledger_task_status", TID, True, {"status": "IN_PROGRESS"})  # not done
    s.journal("sent", "7", True, {"subject": "Re: Quote"})
    s.journal("archive", "zz", False, {"subject": "failed"})  # failures not shown
    s.journal("board_watch", "g-a1", True)  # not a "done" action
    board._clock["now"] = dt.datetime.now(NY)  # journal rows are stamped now
    done = board.build()["columns"]["done"]
    assert [d["what"] for d in done] == ["Sent", "Task completed", "Archived"]  # newest first
    assert done[-1]["subject"] == "Storage"


def test_nudge_makes_one_new_draft_listing_every_thread(board):
    r = board.r_nudge({}, {"addr": "DEE@example.org"}, M)
    d = r["draft"]
    v = d["current"]
    assert d["kind"] == "new" and d["state"] == "DRAFT"  # a draft, never sent
    assert "dee@example.org" in v["to_addrs"] and v["cc"] == ""
    assert v["subject"] == "Following up on 2 open items"
    assert "- Security review (my last note 6 days ago)" in v["body"]
    assert "- Firewall rule (my last note 2 days ago)" in v["body"]
    assert "DM" not in v["body"]  # Slack threads are not emailed about
    assert v["body"].startswith("Hi Dee,") and v["body"].rstrip().endswith("Ada")
    assert v["author"] == "ai" and v["instruction"] == "Board nudge"
    assert r["threads"] == ["g-w1", "g-w2"]
    j = board.store.journal_recent(1, "board_")[0]
    assert j["action"] == "board_nudge_draft" and "Security" not in json.dumps(j)


def test_nudge_single_item_wording(board):
    v = board.r_nudge({}, {"addr": "eve@example.org"}, M)["draft"]["current"]
    assert v["subject"] == "Following up: Quote"
    assert 'Just checking in on "Quote" (my last note was 3 days ago).' in v["body"]


@pytest.mark.parametrize(
    ("addr", "msg"),
    [
        ("not-an-address", "pick a person"),
        ("ada@example.org", "your own address"),
        ("ben@example.org", "nothing is waiting"),  # Ben is MINE, not WAITING
        ("attacker@evil.example", "nothing is waiting"),  # only people on the board
    ],
)
def test_nudge_refuses_anyone_not_waiting(board, addr, msg):
    with pytest.raises(ApiError, match=msg):
        board.r_nudge({}, {"addr": addr}, M)


def test_nudge_never_calls_send(board, monkeypatch):
    called: list[Any] = []
    for name in ("approve", "review", "send", "queue_send"):
        if hasattr(board.compose, name):
            monkeypatch.setattr(board.compose, name, lambda *a, **k: called.append(a))
    board.r_nudge({}, {"addr": "dee@example.org"}, M)
    assert called == []


def test_board_routes_over_http_demo():
    import http.client

    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:

        def req(method: str, path: str, body: Any = None, token: bool = True) -> tuple[int, Any]:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            h = {"Host": f"127.0.0.1:{port}"}
            data = None
            if body is not None:
                data = json.dumps(body)
                h["Content-Type"] = "application/json"
                if token:
                    h["X-Ultra-Token"] = api.token
            c.request(method, path, data, h)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")

        s, b = req("GET", "/api/board")
        assert s == 200 and b["counts"]["mine"] >= 1
        waiting = b["columns"]["waiting"]
        dee = next(g for g in waiting if g["can_nudge"])
        assert req("POST", "/api/board/nudge", {"addr": dee["addr"]}, token=False)[0] == 403
        s, n = req("POST", "/api/board/nudge", {"addr": dee["addr"]})
        assert s == 200 and n["draft"]["state"] == "DRAFT"
        s, _ = req("GET", f"/api/drafts/{n['draft']['id']}")
        assert s == 200  # the page can open it in the composer
        key = b["columns"]["mine"][0]["key"]
        assert req("POST", "/api/board/watch", {"key": key})[0] == 200
        s, b2 = req("GET", "/api/board")
        assert key in [r["key"] for r in b2["columns"]["watching"]]
    finally:
        httpd.shutdown()
        httpd.server_close()
