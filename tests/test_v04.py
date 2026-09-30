"""v0.4 tests: participants, bucket, staging, ledger writes, one-commit cards.

The writer is tested against a fake `nexus` that records argv and stdin, so the
exact command lines are asserted. A separate opt-in test (ULTRA_NEXUS_DEV=1) runs the
real CLI against a throwaway local database.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from ultra.bucket import (
    Stager,
    gcp_candidates,
    local_minute,
    participants,
    snapshot_thread,
    template_log,
    template_task,
)
from ultra.config import Config
from ultra.demo import DemoLedger, DemoWriter
from ultra.desk import Desk
from ultra.ledger_write import LedgerWriter, WriteError
from ultra.rules import Rules
from ultra.store import Store

ME = "ada@example.org"
BEN = "11111111-1111-4111-8111-111111111111"
CY = "22222222-2222-4222-8222-222222222222"
LAB = "33333333-3333-4333-8333-333333333333"


def _msgs() -> list[dict[str, Any]]:
    return [
        {
            "from": "Ben Carter <ben@example.org>",
            "to": f"Ada <{ME}>",
            "cc": "Cy Dunn <cy@example.org>, noreply@service.example.org",
            "subject": "RITM0000042 storage",
            "ts": "2026-09-29T13:05:00+00:00",
            "body": "Project id: ada-lab-prod needs 10 TB. Costs $50/TB.",
            "mine": False,
        },
        {
            "from": f"Ada <{ME}>",
            "to": "ben@example.org",
            "cc": "Dee <dee@partner.test>",
            "subject": "Re: RITM0000042 storage",
            "ts": "2026-09-29T14:00:00+00:00",
            "body": "On it.",
            "mine": True,
        },
    ]


def _rules() -> Rules:
    return Rules(me={ME})


# ---------------------------------------------------------------- participants
def test_participants_everyone_but_me_and_noise():
    ps = participants(_msgs(), _rules())
    addrs = [p["addr"] for p in ps]
    assert addrs[0] == "ben@example.org"  # the sender first
    assert set(addrs) == {"ben@example.org", "cy@example.org", "dee@partner.test"}
    ben = ps[0]
    assert ben["name"] == "Ben Carter" and "from" in ben["roles"] and "to" in ben["roles"]


def test_local_minute_converts_offsets():
    # 13:05 UTC is 09:05 in New York (EDT); a -0700 header converts too
    assert local_minute("2026-09-29T13:05:00+00:00", "America/New_York") == "2026-09-29 09:05"
    assert local_minute("2026-09-29T10:00:00-07:00", "America/New_York") == "2026-09-29 13:00"


def test_gcp_and_ticket_detection():
    assert gcp_candidates("Project id: ada-lab-prod needs") == ["ada-lab-prod"]
    assert gcp_candidates("console.cloud.google.com/home?project=my-proj-123") == ["my-proj-123"]
    snap = snapshot_thread("g-t1", {"messages": _msgs()}, _rules(), "email")
    assert snap["tickets"] == ["RITM0000042"] and snap["gcp"] == ["ada-lab-prod"]


def test_templates_are_ascii_and_factual():
    snap = snapshot_thread("g-t1", {"messages": _msgs()}, _rules(), "email")
    snap["latest_other"]["text"] += " \u2014 thanks \u201cAda\u201d"
    text = template_log([snap], "America/New_York")
    assert text.isascii()
    assert "RITM0000042" in text and "$50/TB" in text and "Ben Carter" in text
    assert "My reply: On it." in text
    assert template_task([snap]).startswith("Follow up: RITM0000042 storage (Ben Carter")


# ---------------------------------------------------------------- staging chips
def test_stage_builds_chips_from_all_people():
    st = Stager(DemoLedger(), "America/New_York", me_netid="adal")
    snap = snapshot_thread("g-t1", {"messages": _msgs()}, _rules(), "email")
    card = st.stage("log", [snap])
    ids = {c["id"]: c for c in card["chips"]}
    assert BEN in ids and CY in ids  # sender AND cc'd person
    assert ids[BEN]["via"] == "log" and ids[BEN]["checked"]
    assert LAB in ids and ids[LAB]["derived"]
    assert {u["addr"] for u in card["unresolved"]} == {"dee@partner.test"}
    assert card["date"] == "2026-09-29 10:00"  # latest message, local time


def test_task_chips_are_all_references():
    st = Stager(DemoLedger(), "America/New_York")
    snap = snapshot_thread("g-t1", {"messages": _msgs()}, _rules(), "email")
    card = st.stage("task", [snap])
    assert card["chips"] and all(c["via"] == "ref" for c in card["chips"])


def test_card_commits_once():
    st = Stager(DemoLedger(), "UTC")
    card = st.stage(
        "log",
        [
            {
                "kind": "snippet",
                "key": "s",
                "subject": "x",
                "text": "hi",
                "people": [],
                "tickets": [],
                "gcp": [],
                "last_ts": "",
            }
        ],
    )
    st.claim(card["id"])
    with pytest.raises(PermissionError):
        st.claim(card["id"])
    with pytest.raises(LookupError):
        st.claim("nope")


# ---------------------------------------------------------------- the writer
FAKE = r"""
import json, os, sys
a = sys.argv[1:]
stdin = sys.stdin.read()
rec = open(os.environ["FAKE_LOG"], "a")
rec.write(json.dumps({"argv": a, "stdin": stdin, "env_cols": os.environ.get("COLUMNS")}) + "\n")
rec.close()
if a[0] == "log":
    print("\x1b[32mLogged (ID: aaaaaaaa-0000-4000-8000-000000000001)\x1b[0m")
    for i, x in enumerate(a):
        if x == "--link":
            print(f"  Linked (explicit): Person {a[i+1][:4]}")
elif a[:2] == ["tasks", "add"]:
    print(f"Added Task: {a[-1]} (ID: bbbbbbbb-0000-4000-8000-000000000002) -> Assigned to Me")
elif a[0] == "link":
    print(f"Linked {a[1]} --[{a[4]}]--> {a[2]}")
elif a[0] == "unlink":
    print(f"Unlinked {a[1]} and {a[2]}.")
elif a[:2] == ["tasks", "update"]:
    print("Updated task")
else:
    print("unexpected"); sys.exit(3)
"""


@pytest.fixture
def writer(tmp_path: Path, monkeypatch) -> tuple[LedgerWriter, Path]:
    b = tmp_path / "nexus"
    b.write_text("#!/usr/bin/env python3\n" + FAKE)
    b.chmod(0o755)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    return LedgerWriter(Config({"ledger": {"binary": str(b)}}), Store(tmp_path / "s.db")), log


def _calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_log_text_goes_on_stdin_not_argv(writer):
    w, log = writer
    text = 'Costs $50/TB & "quotes" $(rm -rf ~) `x`'
    r = w.log(text, "2026-09-29 09:05", [BEN, CY, BEN])
    assert r["id"] == "aaaaaaaa-0000-4000-8000-000000000001"
    assert len(r["explicit"]) == 2
    c = _calls(log)[0]
    assert c["argv"] == [
        "log",
        "--date",
        "2026-09-29 09:05",
        "--link",
        BEN,
        "--link",
        CY,
        "--strict-links",
        "--yes",
        "-",
    ]
    assert c["stdin"] == text + "\n"
    assert c["env_cols"] == "4000"  # Rich never wraps the id


@pytest.mark.parametrize("bad", ["adal", "-rf", "--help", BEN + " x", "", "../x"])
def test_links_must_be_uuids(writer, bad):
    w, log = writer
    with pytest.raises(WriteError):
        w.log("hi", "2026-09-29 09:05", [bad])
    with pytest.raises(WriteError):
        w.link(bad, BEN, "REFERENCED_IN")
    assert not log.exists()


def test_bad_date_type_priority_refused(writer):
    w, log = writer
    with pytest.raises(WriteError):
        w.log("hi", "tomorrow", [])
    with pytest.raises(WriteError):
        w.link(BEN, CY, "PI_OF")
    with pytest.raises(WriteError):
        w.task_add("x", "URGENT")
    assert not log.exists()


def test_task_text_is_sanitised(writer):
    w, log = writer
    r = w.task_add("--help   Follow up [/x] with Ben", "HIGH")
    assert r["id"] == "bbbbbbbb-0000-4000-8000-000000000002"
    argv = _calls(log)[0]["argv"]
    assert argv == ["tasks", "add", "--priority", "HIGH", "--", "--help Follow up (/x) with Ben"]


def test_only_allowed_commands(writer):
    w, _ = writer
    for bad in (["db-reset"], ["people", "delete", "x"], ["interactions", "delete", "x"]):
        with pytest.raises(WriteError):
            w._run(bad)


def test_link_not_found_is_reported(tmp_path, monkeypatch):
    b = tmp_path / "nexus"
    b.write_text("#!/usr/bin/env python3\nprint('Source x or Target y not found!')\n")
    b.chmod(0o755)
    w = LedgerWriter(Config({"ledger": {"binary": str(b)}}), Store(tmp_path / "s.db"))
    assert w.link(BEN, CY, "REFERENCED_IN")["ok"] is False  # exit 0, but not linked


def test_writes_invalidate_caches_and_journal(writer):
    w, _ = writer
    w.store.cache_put("tasks:open", [1])
    w.store.cache_put("tree:bcarter", {})
    w.store.cache_put("person:ben@example.org", {"keep": True})
    w.task_add("x", "LOW")
    assert w.store.cache_get("tasks:open") is None and w.store.cache_get("tree:bcarter") is None
    assert w.store.cache_get("person:ben@example.org") is not None
    j = w.store.journal_recent(5, "ledger_")
    assert j[0]["action"] == "ledger_task_add" and "x" not in json.dumps(j[0]["detail"])


# ---------------------------------------------------------------- desk end to end
def _desk(tmp_path: Path) -> tuple[Desk, DemoWriter]:
    led = DemoLedger()
    w = DemoWriter(led)
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


def _wait(desk: Desk, cid: str) -> dict[str, Any]:
    for _ in range(100):
        p = desk.r_commit_status({}, None, _m(cid))
        if p["state"] != "running":
            return p
        time.sleep(0.02)
    raise AssertionError("commit did not finish")


def _m(v: str):
    import re

    return re.match(r"(.+)", v)


def test_desk_people_tab_lists_everyone(tmp_path):
    desk, _ = _desk(tmp_path)
    r = desk.r_people({}, None, _m("g-t1"))
    by = {p["addr"]: p for p in r["people"]}
    assert set(by) == {"ben@example.org", "cy@example.org", "dee@partner.test"}
    assert by["cy@example.org"]["ledger"]["id"] == CY
    assert by["dee@partner.test"]["ledger"] is None


def test_desk_log_commit_with_readback(tmp_path):
    desk, w = _desk(tmp_path)
    desk.r_bucket_add({}, {"kind": "conversation", "key": "g-t1"}, None)
    card = desk.r_stage({}, {"action": "log"}, None)
    chips = [c for c in card["chips"] if c["id"] in (BEN, CY, LAB)]
    desk.r_commit(
        {},
        {"card": card["id"], "text": "Talked \u2014 storage", "chips": chips, "date": card["date"]},
        None,
    )
    p = _wait(desk, card["id"])
    assert p["state"] == "done"
    kinds = [c[0] for c in w.calls]
    assert kinds[0] == "log"
    assert w.calls[0][1]["text"] == "Talked - storage"  # ASCII-fixed before writing
    assert set(w.calls[0][1]["links"]) == {BEN, CY, LAB}
    assert sorted(p["result"]["linked"]) == sorted([BEN, CY, LAB])
    assert p["result"]["missing"] == []
    assert desk.store.bucket_list() == []  # bucket cleared after a saved log
    # the same card cannot commit twice
    with pytest.raises(Exception, match="already"):
        desk.r_commit(
            {}, {"card": card["id"], "text": "again", "chips": chips, "date": card["date"]}, None
        )
    assert kinds.count("log") == 1


def test_desk_task_commit_links_references(tmp_path):
    desk, w = _desk(tmp_path)
    card = desk.r_stage({}, {"action": "task", "key": "g-t1"}, None)
    chips = [c for c in card["chips"] if c["id"] == BEN]
    desk.r_commit(
        {}, {"card": card["id"], "text": "Follow up", "chips": chips, "priority": "HIGH"}, None
    )
    p = _wait(desk, card["id"])
    assert p["state"] == "done" and p["result"]["kind"] == "task"
    assert [c[0] for c in w.calls] == ["task_add", "link"]
    assert w.calls[1][1]["type"] == "REFERENCED_IN" and w.calls[1][1]["target"] == BEN


def test_desk_validation_error_releases_card(tmp_path):
    desk, w = _desk(tmp_path)
    card = desk.r_stage({}, {"action": "log", "key": "g-t1"}, None)
    with pytest.raises(Exception, match="date"):
        desk.r_commit({}, {"card": card["id"], "text": "x", "chips": [], "date": "soon"}, None)
    desk.r_commit(
        {}, {"card": card["id"], "text": "x", "chips": [], "date": "2026-09-29 10:00"}, None
    )
    assert _wait(desk, card["id"])["state"] == "done"
    assert [c[0] for c in w.calls] == ["log"]


def test_desk_non_uuid_chips_dropped(tmp_path):
    desk, w = _desk(tmp_path)
    card = desk.r_stage({}, {"action": "log", "key": "g-t1"}, None)
    desk.r_commit(
        {},
        {
            "card": card["id"],
            "text": "x",
            "date": card["date"],
            "chips": [
                {"id": "--strict-links", "via": "log"},
                {"id": "adal", "via": "log"},
                {"id": BEN, "via": "log"},
            ],
        },
        None,
    )
    _wait(desk, card["id"])
    assert w.calls[0][1]["links"] == [BEN]


def test_desk_unlink_needs_confirm(tmp_path):
    desk, w = _desk(tmp_path)
    with pytest.raises(Exception, match="confirm"):
        desk.r_unlink({}, {"a": BEN, "b": CY}, None)
    assert w.calls == []


def test_bucket_add_entity_needs_uuid(tmp_path):
    desk, _ = _desk(tmp_path)
    with pytest.raises(Exception, match="UUID"):
        desk.r_bucket_add({}, {"kind": "entity", "id": "adal", "name": "x"}, None)
    r = desk.r_bucket_add(
        {}, {"kind": "entity", "id": LAB, "name": "Lovelace Lab", "type": "Lab"}, None
    )
    assert r["items"][0]["kind"] == "entity"


# ---------------------------------------------------------------- real CLI (opt-in)
@pytest.mark.skipif(not os.getenv("ULTRA_NEXUS_DEV"), reason="set ULTRA_NEXUS_DEV=<wrapper>")
def test_real_cli_against_dev_database(tmp_path):
    """Runs the real nexus CLI via a wrapper that points at a throwaway dev DB."""
    from ultra.ledger import Ledger

    cfg = Config({"ledger": {"binary": os.environ["ULTRA_NEXUS_DEV"]}})
    store = Store(tmp_path / "s.db")
    w, led = LedgerWriter(cfg, store), Ledger(cfg, store)
    ada = led._run(["people", "show", "adal"])["id"]
    r = w.log('Real CLI test: $50/TB & "quotes"', "2026-09-29 20:00", [ada])
    assert r["id"] and r["rc"] == 0
    back = led.interaction(r["id"])
    assert back and ada in {x["id"] for x in back["links"]}
    assert back["details"]["original_text"].startswith('Real CLI test: $50/TB & "quotes"')
    t = w.task_add("Real CLI task [ultra test]", "LOW")
    assert t["id"]
    assert w.link(t["id"], ada, "REFERENCED_IN")["ok"]
    assert w.task_status(t["id"], "DONE")["ok"]
