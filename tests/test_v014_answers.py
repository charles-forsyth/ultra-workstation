"""v0.14: Ask Hermes answers into Ultra's normal cards (log, task) and drafts.

Staging never writes: the card is inert until Commit, exactly like every other card.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

import pytest
from test_v04 import BEN, CY, LAB, _desk, _m, _wait

from ultra.server import ApiError

TASK = "44444444-4444-4444-8444-444444444444"
ANSWER = (
    "Ben wants 10 TB on ada-lab-prod by Friday \u2014 reply that we can do it.\n\n"
    "- Check quota first\n- Then confirm with Cy"
)


def test_stage_answer_log_uses_the_items_people_and_the_answer_text(tmp_path):
    desk, w = _desk(tmp_path)
    card = desk.r_stage_answer(
        {}, {"action": "log", "text": ANSWER, "key": "g-t1", "title": "Storage"}, None
    )
    assert card["action"] == "log" and card["state"] == "staged"
    ids = {c["id"] for c in card["chips"]}
    assert {BEN, CY} <= ids  # the thread's people resolve like any card
    assert "10 TB" in card["text"] and "\u2014" not in card["text"]  # ASCII-fixed
    assert card["items"][0]["key"] == "g-t1"
    assert w.calls == []  # staging writes nothing


def test_stage_answer_task_takes_the_first_line(tmp_path):
    desk, w = _desk(tmp_path)
    card = desk.r_stage_answer({}, {"action": "task", "text": "\n\n## Reply to Ben\nmore"}, None)
    assert card["action"] == "task" and card["text"] == "Reply to Ben"
    assert card["items"][0]["kind"] == "note"  # no item: a note-sourced card
    assert w.calls == []


def test_stage_answer_about_a_task_references_it(tmp_path):
    desk, _ = _desk(tmp_path)
    card = desk.r_stage_answer(
        {}, {"action": "log", "text": "Progress made.", "task": TASK, "title": "Quota"}, None
    )
    assert any(i["key"] == TASK for i in card["items"])


def test_stage_answer_then_commit_goes_through_the_normal_path(tmp_path):
    desk, w = _desk(tmp_path)
    card = desk.r_stage_answer({}, {"action": "log", "text": ANSWER, "key": "g-t1"}, None)
    chips = [c for c in card["chips"] if c["id"] in (BEN, CY, LAB)]
    desk.r_commit(
        {}, {"card": card["id"], "text": card["text"], "chips": chips, "date": card["date"]}, None
    )
    p = _wait(desk, card["id"])
    assert p["state"] == "done"
    assert w.calls[0][0] == "log"
    assert "10 TB" in w.calls[0][1]["text"]
    with pytest.raises(ApiError, match="already"):  # single use, like every card
        desk.r_commit(
            {}, {"card": card["id"], "text": "x", "chips": chips, "date": card["date"]}, None
        )


@pytest.mark.parametrize(
    ("body", "msg"),
    [
        ({"action": "delete", "text": "x"}, "log or task"),
        ({"action": "log", "text": "   "}, "empty"),
        ({"action": "log", "text": "x" * 20001}, "over 20000"),
        ({"action": "log", "text": "ok", "key": "../../etc"}, None),  # ignored, not an error
    ],
)
def test_stage_answer_validation(tmp_path, body, msg):
    desk, w = _desk(tmp_path)
    if msg is None:
        card = desk.r_stage_answer({}, body, None)
        assert card["items"][0]["kind"] == "note"  # a bad key is never read
    else:
        with pytest.raises(ApiError, match=msg):
            desk.r_stage_answer({}, body, None)
    assert w.calls == []


def test_stage_answer_route_is_registered_and_csrf_guarded():
    import http.client

    from ultra.config import Config
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        body = json.dumps({"action": "log", "text": "From Hermes", "key": "g-100"})
        h = {"Host": f"127.0.0.1:{port}", "Content-Type": "application/json"}
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/ledger/stage-answer", body, h)
        assert c.getresponse().status == 403  # no token
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("POST", "/api/ledger/stage-answer", body, h | {"X-Ultra-Token": api.token})
        r = c.getresponse()
        card = json.loads(r.read())
        assert r.status == 200 and card["action"] == "log" and card["text"] == "From Hermes"
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_hermes_draft_lands_as_ai_version_and_voids_approval(tmp_path):
    """The panel's Use as reply saves through the Studio route: author ai, linted, and
    still needs both approvals (any new version voids an earlier approval)."""
    from ultra.compose import Composer
    from ultra.config import Config
    from ultra.store import Store

    comp = Composer(Config({}), Store(tmp_path / "c.db"), {"ada@example.org"})
    from test_v03 import _thread

    thread = _thread()
    d = comp.create("reply_all", thread, "ada@example.org")
    d = comp.save(d["id"], {"body": "Yes, we can."}, "me", "edit")
    assert comp.approve(d["id"])["state"] == "APPROVED"
    out = comp.save(d["id"], {"body": "Hermes says: yes, by Friday."}, "ai", "Ask Hermes")
    assert out["current"]["author"] == "ai" and out["current"]["instruction"] == "Ask Hermes"
    assert out["state"] == "DRAFT"  # a new version always needs approval again


def test_panel_offers_reply_only_for_mail_and_slack():
    js = (Path(__file__).parents[1] / "src/ultra/static/ask.js").read_text()
    # the reply target is limited to email (g-) and Slack (s-) keys; tickets reply via
    # Draft Studio (Ref line) and other items have nothing to reply to
    assert re.search(r"/\^\(g\|s\)-/\.test\(k", js)
    # every action opens a card or the composer; the panel itself never posts a write
    for bad in ("/api/send", "/api/ledger/commit", "/api/drafts/\\d+/approve"):
        assert not re.search(bad, js), bad


def test_spec_documents_answer_actions():
    spec = (Path(__file__).parents[1] / "SPEC.md").read_text()
    assert "Use as reply" in spec and "/api/ledger/stage-answer" in spec


def _unused(_: Any) -> None:  # keep imports honest for linters
    _m("x")
