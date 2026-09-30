"""v0.9.8 ticket replies: the reference line (Ref:MSG...) is carried and enforced."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ultra.compose import ComposeError, Composer, ticket_ref, with_ref
from ultra.config import Config
from ultra.lint import Issue, blocking
from ultra.store import Store

ME = "ada@example.org"
DESK = "desk@tickets.example.org"


def _is_ticket(a: str) -> bool:
    return "tickets.example.org" in (a or "")


def _notice(ref_hist: str, ref_own: str, mid: str) -> dict[str, Any]:
    # the shape real notices have: earlier comments quoted first, own reference last
    body = (
        "Short description: storage request\n\nComments:\n"
        f"> earlier comment\n> {ref_hist}\n\nBen added: the quota is approved.\n\n{ref_own}"
    )
    return {
        "id": mid,
        "from": f"Service Desk <{DESK}>",
        "from_addr": DESK,
        "to": ME,
        "cc": "Ben Carter <ben@example.org>",
        "subject": "Request RITM0000001 comments added",
        "body": body,
        "mine": False,
        "message_id": f"<{mid}@t>",
        "references": "",
        "ts": "2026-09-29T10:00:00",
    }


def _thread(own_reply: bool = True) -> dict[str, Any]:
    msgs = [_notice("Ref:MSG10000001", "Ref:MSG10000002", "n1")]
    if own_reply:
        msgs.append(
            {
                "id": "m2",
                "from": f"Ada <{ME}>",
                "from_addr": ME,
                "to": DESK,
                "cc": "",
                "subject": "Re: Request RITM0000001 comments added",
                "mine": True,
                "body": "Thanks, done.\n\nAda\n\nRef:MSG10000002",
                "message_id": "<m2@t>",
                "references": "",
                "ts": "2026-09-29T11:00:00",
            }
        )
    msgs.append(_notice("Ref:MSG10000002", "Ref:MSG10000003", "n3"))
    return {"key": "g-thr123", "messages": msgs}


@pytest.fixture
def comp(tmp_path: Path) -> Composer:
    cfg = Config({"mail": {"send_delay_seconds": 0}})
    return Composer(cfg, Store(tmp_path / "t.db"), {ME}, is_ticket=_is_ticket)


def _codes(d: dict[str, Any]) -> list[str]:
    return [i["code"] for i in d["current"]["lint"]]


def test_ticket_ref_takes_the_newest_notice_and_its_last_line():
    assert ticket_ref(_thread()["messages"], _is_ticket, r"Ref:MSG\d{6,12}") == "Ref:MSG10000003"


def test_ticket_ref_ignores_the_operators_own_mail():
    msgs = [
        _notice("Ref:MSG10000001", "Ref:MSG10000002", "n1"),
        {"from_addr": ME, "mine": True, "body": "x\n\nRef:MSG19999999"},
    ]
    assert ticket_ref(msgs, _is_ticket, r"Ref:MSG\d{6,12}") == "Ref:MSG10000002"


def test_ticket_ref_empty_for_ordinary_mail():
    msgs = [
        {"from_addr": "ben@example.org", "mine": False, "body": "Ref:MSG12345678 in a normal mail"}
    ]
    assert ticket_ref(msgs, _is_ticket, r"Ref:MSG\d{6,12}") == ""


def test_with_ref_puts_one_line_last():
    pat = r"Ref:MSG\d{6,12}"
    assert with_ref("Hi\n\nAda", "Ref:MSG10000003", pat) == "Hi\n\nAda\n\nRef:MSG10000003\n"
    body = "Hi\nRef:MSG10000002\n\nAda\n> Ref:MSG10000001\n"
    assert with_ref(body, "Ref:MSG10000003", pat) == "Hi\n\nAda\n\nRef:MSG10000003\n"
    assert with_ref("Hi", "", pat) == "Hi"


def test_reply_all_to_a_notice_remembers_the_ref(comp):
    d = comp.create("reply_all", _thread(), ME)
    assert d["ticket_ref"] == "Ref:MSG10000003"
    assert "ticket_ref" in _codes(d)  # empty body: the line is missing, approval blocked


def test_ai_text_always_carries_the_ref(comp):
    d = comp.create("reply_all", _thread(), ME)
    d = comp.save(d["id"], {"body": "Hi Ben,\n\nThanks, all set.\n\nAda"}, "ai", "Draft Studio")
    assert d["current"]["body"].endswith("Ada\n\nRef:MSG10000003\n")
    assert "ticket_ref" not in _codes(d) and "ticket_ref_place" not in _codes(d)
    # a model that writes a stale reference is corrected
    d = comp.save(d["id"], {"body": "Hi\n\nAda\n\nRef:MSG10000001"}, "ai", "revise")
    assert d["current"]["body"].count("Ref:MSG") == 1 and "Ref:MSG10000003" in d["current"]["body"]


def test_operator_removing_the_ref_blocks_approval(comp):
    d = comp.create("reply_all", _thread(), ME)
    d = comp.save(d["id"], {"body": "Hi Ben,\n\nThanks.\n\nAda"}, "me")
    assert "ticket_ref" in _codes(d)
    assert blocking([Issue(**i) for i in d["current"]["lint"]])
    with pytest.raises(ComposeError, match="Fix the errors"):
        comp.approve(d["id"])
    d = comp.fix_ref(d["id"])
    assert d["current"]["body"].endswith("Ref:MSG10000003\n") and "ticket_ref" not in _codes(d)
    assert comp.approve(d["id"])["state"] == "APPROVED"


def test_wrong_or_duplicate_ref_is_an_error(comp):
    d = comp.create("reply_all", _thread(), ME)
    d = comp.save(d["id"], {"body": "Hi\n\nAda\n\nRef:MSG10000002"}, "me")
    assert "ticket_ref" in _codes(d)
    d = comp.save(d["id"], {"body": "Hi\nRef:MSG10000003\n\nAda\n\nRef:MSG10000003"}, "me")
    assert "ticket_ref" in _codes(d)


def test_ref_in_the_wrong_place_is_only_a_warning(comp):
    d = comp.create("reply_all", _thread(), ME)
    d = comp.save(d["id"], {"body": "Hi\n\nRef:MSG10000003\n\nMore text.\n\nAda"}, "me")
    assert "ticket_ref_place" in _codes(d) and "ticket_ref" not in _codes(d)
    assert not blocking([Issue(**i) for i in d["current"]["lint"]])


def test_ordinary_replies_and_forwards_are_untouched(comp):
    th = {
        "key": "g-x1",
        "messages": [
            {
                "id": "a",
                "from": "Ben <ben@example.org>",
                "from_addr": "ben@example.org",
                "to": ME,
                "cc": "",
                "subject": "Hi",
                "body": "Ref:MSG12345678 quoted in chat",
                "mine": False,
                "message_id": "<a@t>",
                "references": "",
                "ts": "2026-09-29T10:00:00",
            }
        ],
    }
    d = comp.create("reply", th, ME)
    d = comp.save(d["id"], {"body": "Hi Ben\n\nAda"}, "ai", "x")
    assert d["ticket_ref"] == "" and "Ref:MSG" not in d["current"]["body"]
    f = comp.create("forward", _thread(), ME)
    assert f["ticket_ref"] == ""
    with pytest.raises(ComposeError, match="no ticket reference"):
        comp.fix_ref(d["id"])


def test_ref_pattern_is_configurable(tmp_path):
    cfg = Config({"mail": {"send_delay_seconds": 0}, "tickets": {"ref_pattern": r"TRK#\d{4}"}})
    c = Composer(cfg, Store(tmp_path / "t.db"), {ME}, is_ticket=_is_ticket)
    th = {"key": "g-y1", "messages": [{**_notice("x", "y", "n"), "body": "note\n\nTRK#1234"}]}
    d = c.create("reply_all", th, ME)
    assert d["ticket_ref"] == "TRK#1234"


def test_side_conversation_off_the_desk_gets_no_ref(comp):
    """A reply to a person on the ticket thread (not to the desk) is not filed on the
    ticket, so no reference line is added (seen in real mail)."""
    th = _thread(own_reply=False)
    th["messages"].append(
        {
            "id": "p1",
            "from": "Ben Carter <ben@example.org>",
            "from_addr": "ben@example.org",
            "to": ME,
            "cc": "",
            "subject": "Re: Request RITM0000001",
            "mine": False,
            "body": "Quick question off the ticket.",
            "message_id": "<p1@t>",
            "references": "",
            "ts": "2026-09-30T10:00:00",
        }
    )
    d = comp.create("reply", th, ME)
    assert d["current"]["to_addrs"].endswith("<ben@example.org>")
    assert d["ticket_ref"] == "" and "ticket_ref" not in _codes(d)
    d2 = comp.create("reply_all", _thread(), ME)  # the desk is on it: ref required
    assert d2["ticket_ref"] == "Ref:MSG10000003"
