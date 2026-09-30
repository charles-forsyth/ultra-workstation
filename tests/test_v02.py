"""v0.2 tests: rules, Gmail parsing, stream building, ledger and Slack adapters.

No network: Gmail responses are synthetic dicts, nexus and claude are fake scripts.
"""

from __future__ import annotations

import base64
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

import pytest

from ultra.config import Config
from ultra.ledger import Ledger, LedgerError
from ultra.mail import Mail, extract_body, html_to_text, split_quoted
from ultra.rules import Rules, classify_thread, ticket_events
from ultra.slack import Slack, SlackError
from ultra.store import Store

ME = "ada@example.org"
NOW = int(time.time() * 1000)
DAY = 86400 * 1000


def _rules(**kw: Any) -> Rules:
    cfg = Config(
        {
            "operator": {"addresses": [ME]},
            "tickets": {"sender_patterns": [r"@desk\.example\.org$"]},
        }
    )
    r = Rules.from_config(cfg, vip={"vip@example.org"})
    for k, v in kw.items():
        setattr(r, k, v)
    return r


def msg(frm: str, ago_days: float, text: str = "hi", labels=None) -> dict:
    return {
        "from_addr": frm,
        "from_name": frm,
        "ts_ms": int(NOW - ago_days * DAY),
        "text": text,
        "labels": labels or [],
    }


# ---------------------------------------------------------------- rules
def test_last_from_other_in_inbox_is_mine():
    c = classify_thread(_rules(), [msg(ME, 2), msg("ben@example.org", 1)], True)
    assert c["court"] == "MINE" and c["waiting_days"] == 1


def test_last_from_me_is_waiting_with_age_badges():
    r = _rules()
    assert classify_thread(r, [msg("b@x.org", 9), msg(ME, 1)], True)["court"] == "WAITING"
    slow = msg(ME, 3.5)
    slow["to"] = "b@x.org"
    late = msg(ME, 6)
    late["to"] = "b@x.org"
    assert "SLOW" in classify_thread(r, [slow], True)["badges"]
    assert "OVERDUE" in classify_thread(r, [late], True)["badges"]


def test_ready_signal_after_my_message_is_flagged():
    r = _rules()
    c = classify_thread(r, [msg(ME, 2), msg("ben@example.org", 1, "The project is ready.")], True)
    assert c["court"] == "MINE" and "READY" in c["badges"] and c["ready"]
    # a "ready" that came BEFORE my reply does not count
    c = classify_thread(r, [msg("ben@example.org", 3, "It is ready"), msg(ME, 2)], True)
    assert "READY" not in c["badges"]


def test_noise_goes_low_unless_vip_or_ready():
    r = _rules()
    assert classify_thread(r, [msg("noreply@x.org", 1)], True)["court"] == "LOW"
    assert (
        classify_thread(r, [msg("a@x.org", 1, labels=["CATEGORY_PROMOTIONS"])], True)["court"]
        == "LOW"
    )
    c = classify_thread(r, [msg("vip@example.org", 1)], True)
    assert c["court"] == "MINE" and "VIP" in c["badges"]


def test_note_to_self_is_hidden_but_sent_to_others_waits():
    r = _rules()
    alone = msg(ME, 5)
    alone["to"] = ME
    assert classify_thread(r, [alone], True)["court"] == "NONE"
    out = msg(ME, 5)
    out["to"] = "Ben <ben@example.org>"
    assert classify_thread(r, [out], True)["court"] == "WAITING"


def test_rsvp_subjects():
    from ultra.rules import is_rsvp

    assert is_rsvp("Accepted: Weekly sync @ Mon")
    assert is_rsvp("Declined: Benefits Audit")
    assert is_rsvp("Updated invitation: X")
    assert not is_rsvp("Re: Accepted proposal")


def test_archived_thread_from_other_is_hidden():
    assert classify_thread(_rules(), [msg("b@x.org", 1)], False)["court"] == "NONE"


def test_ticket_numbers_and_events():
    r = _rules()
    assert r.ticket_numbers("RITM0012345 and inc0000001") == [
        "INC0000001",
        "RITM0012345",
    ]
    assert r.is_ticket_sender("notify@desk.example.org")
    assert ticket_events("RITM1 has been assigned to you; comments added") == [
        "assigned to you",
        "comments added",
    ]


# ---------------------------------------------------------------- body parsing
def _b(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def test_extract_body_prefers_plain_and_lists_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b("Plain body")}},
                    {"mimeType": "text/html", "body": {"data": _b("<p>HTML</p>")}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "plan.pdf",
                "body": {"attachmentId": "A1", "size": 2048},
            },
        ],
    }
    text, from_html, atts = extract_body(payload)
    assert text == "Plain body" and not from_html
    assert atts == [{"name": "plan.pdf", "mime": "application/pdf", "size": 2048, "id": "A1"}]


def test_html_only_body_is_converted_and_scripts_dropped():
    payload = {
        "mimeType": "text/html",
        "body": {"data": _b("<style>x{}</style><p>Hi&nbsp;there</p><script>evil()</script>")},
    }
    text, from_html, _ = extract_body(payload)
    assert from_html and "Hi" in text and "evil" not in text and "x{}" not in text
    assert html_to_text("<b>a</b><br>b") == "a\n\nb" or "a" in html_to_text("<b>a</b><br>b")


def test_split_quoted():
    new, q = split_quoted("Thanks!\n\nOn Mon, Sep 28, 2026 Ben <b@x.org> wrote:\n> old")
    assert new == "Thanks!" and q.startswith("On Mon")
    new, q = split_quoted("Yes.\n> earlier\n> more")
    assert new == "Yes." and q.startswith("> earlier")
    assert split_quoted("no quote") == ("no quote", "")


# ---------------------------------------------------------------- stream
def _thread(tid: str, msgs: list[dict]) -> dict:
    return {
        "id": tid,
        "historyId": "1",
        "messages": [
            {
                "to": "",
                "cc": "",
                **m,
                "id": f"{tid}-{i}",
                "subject": m.get("subject", "Subj"),
            }
            for i, m in enumerate(msgs)
        ],
    }


def test_build_stream_groups_tickets_and_counts(tmp_path):
    r = _rules()
    cfg = Config({"operator": {"addresses": [ME]}, "mail": {"include_sent": True}})
    mail = Mail(cfg, Store(tmp_path / "s.db"), r)
    tk1 = msg("notify@desk.example.org", 1, "RITM0000001 has been assigned to you")
    tk1["subject"] = "RITM0000001 assigned"
    tk2 = msg("notify@desk.example.org", 0.5, "RITM0000001 comments added")
    tk2["subject"] = "RITM0000001 comments"
    sent = msg(ME, 4)
    sent["to"] = "cy@example.org"
    threads = {
        "a": _thread("a", [msg(ME, 2), msg("ben@example.org", 1, "It is ready")]),
        "b": _thread("b", [sent]),
        "c": _thread("c", [tk1]),
        "d": _thread("d", [tk2]),
        "e": _thread("e", [msg("noreply@x.org", 1)]),
    }
    out = mail._build_stream(threads, inbox_ids={"a", "c", "d", "e"})
    by = {i["key"]: i for i in out["items"]}
    assert by["g-a"]["court"] == "MINE" and "READY" in by["g-a"]["badges"]
    assert by["g-b"]["court"] == "WAITING"
    assert by["g-b"]["addr"] == "cy@example.org"  # who I'm waiting on, not me
    tk = by["k-RITM0000001"]
    assert tk["source"] == "ticket" and sorted(tk["threads"]) == ["c", "d"]
    assert "ASSIGNED" in tk["badges"] and "comments added" in tk["events"]
    assert by["g-e"]["court"] == "LOW"
    assert out["counts"] == {"MINE": 2, "WAITING": 1, "LOW": 1}


def test_default_stream_is_inbox_only(tmp_path):
    """Chuck: work only with what is in the inbox. Archived threads never show,
    including a sent-last thread that would otherwise be Waiting."""
    r = _rules()
    mail = Mail(Config({"operator": {"addresses": [ME]}}), Store(tmp_path / "s.db"), r)
    assert mail.include_sent is False
    out_sent = msg(ME, 4)
    out_sent["to"] = "cy@example.org"
    waiting_in_inbox = msg(ME, 2)
    waiting_in_inbox["to"] = "dee@example.org"
    threads = {
        "a": _thread("a", [msg("ben@example.org", 1)]),
        "b": _thread("b", [out_sent]),  # archived, I sent last
        "c": _thread("c", [waiting_in_inbox]),  # still in inbox, I sent last
        "z": _thread("z", [msg("zed@example.org", 1)]),  # archived, from someone
    }
    out = mail._build_stream(threads, inbox_ids={"a", "c"})
    keys = {i["key"]: i["court"] for i in out["items"]}
    assert keys == {"g-a": "MINE", "g-c": "WAITING"}


# ---------------------------------------------------------------- store
def test_store_is_private_and_caches(tmp_path):
    s = Store(tmp_path / "state.db")
    assert stat.S_IMODE((tmp_path / "state.db").stat().st_mode) == 0o600
    s.cache_put("k", {"a": 1})
    hit = s.cache_get("k")
    assert hit is not None
    v, age = hit
    assert v == {"a": 1} and age < 5
    assert s.cache_get("k", ttl=-1) is None


# ---------------------------------------------------------------- ledger
def _fake_bin(tmp_path: Path, name: str, script: str) -> str:
    p = tmp_path / name
    p.write_text("#!/usr/bin/env python3\n" + script)
    p.chmod(0o755)
    return str(p)


NEXUS = r"""
import json, sys
a = sys.argv[1:]
log = open(__file__ + ".log", "a"); log.write(" ".join(a) + "\n"); log.close()
if a[:2] == ["people", "show"]:
    if a[2] == "bcarter":
        print(json.dumps({"netid": "bcarter", "name": "Ben Carter", "title": "Dir", "email_alias": "ben@example.org"}))
    else:
        print(f"Researcher {a[2]} not found.")
elif a[0] == "search":
    print(json.dumps([{"type": "Researcher", "name": "Ben Carter (bcarter)", "score": 0.9, "id": "u1"}]))
elif a[0] == "tree":
    print(json.dumps({"root": {}, "connections": [
        {"entity_type": "Lab", "name": "Lovelace Lab", "id": "l1", "type": "MEMBER_OF"},
        {"entity_type": "Task", "name": "old task", "id": "t-done", "type": "REFERENCED_IN"},
        {"entity_type": "Task", "name": "open task", "id": "t-open", "type": "REFERENCED_IN"},
        {"entity_type": "Interaction", "name": "first", "id": "i1", "type": "PARTICIPATED_IN"},
        {"entity_type": "Interaction", "name": "newest", "id": "i2", "type": "PARTICIPATED_IN"}]}))
elif a[:2] == ["tasks", "list"]:
    print(json.dumps([{"id": "t-open", "status": "TODO"}]))
else:
    print("unexpected", a); sys.exit(3)
"""


def _ledger(tmp_path, **ledger_cfg) -> tuple[Ledger, str]:
    b = _fake_bin(tmp_path, "nexus", NEXUS)
    cfg = Config(
        {
            "ledger": {
                "binary": b,
                "org_email_domain": "example.org",
                "netid_from_local_part": True,
                **ledger_cfg,
            }
        }
    )
    return Ledger(cfg, Store(tmp_path / "s.db")), b


def test_ledger_context_by_name_search(tmp_path):
    led, b = _ledger(tmp_path)
    c = led.context("ben@example.org", "Ben Carter")
    assert c["netid"] == "bcarter" and c["labs"] == ["Lovelace Lab"]
    assert [t["id"] for t in c["open_tasks"]] == ["t-open"]  # DONE task filtered
    assert c["interactions"][0]["summary"] == "newest"  # newest first
    calls = Path(b + ".log").read_text().splitlines()
    assert all(line.split()[-1] == "--json" for line in calls)
    # second call is served from cache: no new CLI calls
    n = len(calls)
    led.context("ben@example.org", "Ben Carter")
    assert len(Path(b + ".log").read_text().splitlines()) == n


def test_ledger_unknown_person(tmp_path):
    led, _ = _ledger(tmp_path)
    led_search = led
    led_search._run = lambda args, timeout=120: (  # type: ignore[method-assign]
        [] if args[0] == "search" else None
    )
    assert led_search.context("zed@other.org", "Zed")["unresolved"] is True


def test_ledger_refuses_write_commands(tmp_path):
    led, _ = _ledger(tmp_path)
    for bad in (
        ["log", "x"],
        ["link", "a", "b"],
        ["tasks", "add", "x"],
        ["people", "delete", "x"],
    ):
        with pytest.raises(LedgerError):
            led._run(bad)


# ---------------------------------------------------------------- slack
CLAUDE = r"""
import json, os, sys
open(__file__ + ".env", "w").write(json.dumps({"has_key": "ANTHROPIC_API_KEY" in os.environ, "argv": sys.argv[1:]}))
mode = open(__file__ + ".mode").read().strip()
if mode == "ok":
    print("Here you go:\n" + json.dumps([
        {"channel_id": "D1", "channel": "Cy Dunn", "ts": "1759180000.0001", "thread_ts": "", "from": "Cy Dunn", "from_email": "cy@example.org", "text": "Budget done", "permalink": "", "needs_me": False},
        {"channel_id": "D1", "channel": "Cy Dunn", "ts": "1759180100.0001", "thread_ts": "", "from": "Cy Dunn", "from_email": "cy@example.org", "text": "Can you check?", "permalink": "", "needs_me": True}]))
elif mode == "disabled":
    print("claude.ai connectors are disabled because ANTHROPIC_API_KEY is set")
else:
    print("I could not do that.")
"""


def _slack(tmp_path, mode: str) -> tuple[Slack, str]:
    b = _fake_bin(tmp_path, "claude", CLAUDE)
    Path(b + ".mode").write_text(mode)
    cfg = Config({"slack": {"enabled": True, "claude_binary": b, "claude_cwd": str(tmp_path)}})
    return Slack(cfg, Store(tmp_path / "s.db")), b


def test_slack_refresh_strips_api_key_and_uses_read_only_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    s, b = _slack(tmp_path, "ok")
    items = s.refresh()
    assert len(items) == 2
    seen = json.loads(Path(b + ".env").read_text())
    assert seen["has_key"] is False
    argv = seen["argv"]
    allow = argv[argv.index("--allowedTools") + 1]
    deny = argv[argv.index("--disallowedTools") + 1]
    assert "slack_send_message" not in allow and "slack_send_message" in deny
    assert "Bash" in deny and "Write" in deny
    rows = Slack.to_stream(items)
    assert len(rows) == 1 and rows[0]["count"] == 2 and rows[0]["court"] == "MINE"


def test_slack_disabled_connector_is_an_error_not_empty(tmp_path):
    s, _ = _slack(tmp_path, "disabled")
    with pytest.raises(SlackError, match="unreachable"):
        s.refresh()
    assert s.items() == ([], None)


def test_slack_prose_is_an_error(tmp_path):
    s, _ = _slack(tmp_path, "prose")
    with pytest.raises(SlackError):
        s.refresh()


def test_slack_off_by_default(tmp_path):
    s = Slack(Config({}), Store(tmp_path / "s.db"))
    assert not s.enabled
    with pytest.raises(SlackError):
        s.refresh()


def test_live_stream_serves_cache_and_filters(tmp_path, monkeypatch):
    from ultra.live import Live

    cfg = Config({"operator": {"addresses": [ME]}, "ledger": {"enabled": False}})
    live = Live(cfg, Store(tmp_path / "s.db"))
    started = []
    monkeypatch.setattr(live, "_job", lambda name, fn, *a: started.append(name))
    out = live.r_stream({"filter": ["mine"]}, None, None)  # type: ignore[arg-type]
    assert out["loading"] is True and started == ["mail"]
    live.store.cache_put(
        "mail:stream",
        {
            "items": [
                {
                    "key": "g-1",
                    "source": "email",
                    "court": "MINE",
                    "ts_ms": 1,
                    "badges": [],
                },
                {
                    "key": "g-2",
                    "source": "email",
                    "court": "WAITING",
                    "ts_ms": 2,
                    "badges": [],
                },
            ]
        },
    )
    started.clear()
    out = live.r_stream({"filter": ["waiting"]}, None, None)  # type: ignore[arg-type]
    assert [i["key"] for i in out["items"]] == ["g-2"] and not started
    assert out["counts"]["mine"] == 1 and out["counts"]["all"] == 2


def test_no_tokens_means_auth_needed(tmp_path):
    from ultra import google_auth

    cfg = Config({"google": {"token_read": str(tmp_path / "missing.json")}})
    with pytest.raises(google_auth.AuthNeeded, match="capability read"):
        google_auth.credentials(cfg, "read")
    bad = tmp_path / "t.json"
    bad.write_text(json.dumps({"scopes": ["https://www.googleapis.com/auth/calendar"]}))
    os.chmod(bad, 0o600)
    cfg = Config({"google": {"token_read": str(bad)}})
    with pytest.raises(google_auth.AuthNeeded, match="lacks the scope"):
        google_auth.credentials(cfg, "read")


def test_status_poll_refreshes_stale_sources(tmp_path, monkeypatch):
    """An idle open tab only calls /api/status; that must keep mail fresh."""
    from ultra.live import Live

    cfg = Config(
        {
            "operator": {"addresses": [ME]},
            "ledger": {"enabled": False},
            "server": {"poll_mail_seconds": 120},
        }
    )
    live = Live(cfg, Store(tmp_path / "s.db"))
    started: list[str] = []
    monkeypatch.setattr(live, "_job", lambda name, fn, *a: started.append(name))
    live.r_status({}, None, None)  # type: ignore[arg-type]
    assert started == ["mail"]  # no cache yet
    live.store.cache_put("mail:stream", {"items": []})
    started.clear()
    live.r_status({}, None, None)  # type: ignore[arg-type]
    assert started == []  # fresh: nothing started
    ts = time.time() - 121
    monkeypatch.setattr(
        live.store,
        "cache_get",
        lambda k, ttl=None: ({"items": []}, time.time() - ts) if k == "mail:stream" else None,
    )
    live.r_status({}, None, None)  # type: ignore[arg-type]
    assert started == ["mail"]  # stale: refresh started from the status poll
