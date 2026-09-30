"""v0.7.1 tests: whole-thread audio text, complete summaries, link cleanup, full context."""

from __future__ import annotations

import json
from typing import Any

import pytest

from ultra.ai import AI, AIError
from ultra.audio import Audio, speakable
from ultra.config import Config
from ultra.ledger import Ledger
from ultra.store import Store


# ---------------------------------------------------------------- speakable
def test_speakable_keeps_every_sentence_and_drops_link_noise():
    body = (
        "Hi all,  \r\n   NSF is hosting a webinar on Monday, October 5.  \r\n"
        "   Details and registration:  https://www.nsf.gov/events/x <https://www.nsf.gov/events/x>   \r\n"
        "   The next deadline is January 21, 2027:  https://nsf.gov/f <https://nsf.gov/f>  \r\n"
        "   Please help spread the word!  \r\n   Dana  \r\n\r\n"
        "Reply to Sender : https://connect.example.org/reply?x=1\n"
        "Reply to Discussion : https://connect.example.org/d\n"
    )
    s = speakable(body)
    for keep in ("NSF is hosting a webinar", "January 21, 2027", "spread the word", "Dana"):
        assert keep in s
    assert "http" not in s and "<" not in s and "\r" not in s
    assert "Reply to Sender" not in s and "Reply to Discussion" not in s


# ---------------------------------------------------------------- complete answers
class _Resp:
    def __init__(self, text: str, reason: str) -> None:
        self.text = text
        self.candidates = [type("C", (), {"finish_reason": reason})()]
        self.usage_metadata = type("U", (), {"total_token_count": 10})()


class _Models:
    def __init__(self, replies: list[_Resp]) -> None:
        self.replies = replies
        self.configs: list[Any] = []

    def generate_content(self, model: str, contents: str, config: Any) -> _Resp:
        self.configs.append(config)
        return self.replies.pop(0)


def _ai(replies: list[_Resp]) -> tuple[AI, _Models]:
    ai = AI(Config({"ai": {"model": "m-main", "fallback_model": "m-fallback"}}))
    ai.key = "x"
    ai.enabled = True
    models = _Models(replies)
    ai._client = type("Client", (), {"models": models})()
    return ai, models


def test_cut_off_answer_is_refused_and_not_retried_on_another_model():
    """v0.9.2: one model only (operator rule). A cut-off answer is refused outright."""
    ai, models = _ai(
        [
            _Resp("Hi Ada, first item. Ben Carter has the same", "MAX_TOKENS"),
            _Resp("Complete briefing. Two items. Done.", "STOP"),
        ]
    )
    with pytest.raises(AIError, match="stopped early"):
        ai._gen("x", "sys", 800, require_complete=True)
    assert len(models.configs) == 1


def test_cut_off_everywhere_raises_instead_of_voicing_half():
    ai, _ = _ai([_Resp("half", "MAX_TOKENS"), _Resp("half again", "MAX_TOKENS")])
    with pytest.raises(AIError, match="stopped early"):
        ai._gen("x", "sys", 800, require_complete=True)


def test_thinking_allowance_is_added_on_top_of_the_answer_budget():
    from ultra.ai import THINKING_BUDGET

    ai, models = _ai([_Resp("ok.", "STOP")])
    ai._gen("x", "sys", 900)
    cfg = models.configs[0]
    assert cfg.max_output_tokens == 900 + THINKING_BUDGET
    assert cfg.thinking_config.thinking_budget == THINKING_BUDGET


def test_summary_script_sends_whole_text_and_scales_length(tmp_path):
    seen: dict[str, Any] = {}

    class FakeAI:
        enabled = True

        def _gen(self, prompt: str, system: str, max_tokens: int = 0, **kw: Any) -> Any:
            seen.update(prompt=prompt, system=system, max_tokens=max_tokens, **kw)
            return type("R", (), {"text": "Briefing."})()

    a = Audio(Config({}), Store(tmp_path / "s.db"), FakeAI())
    long_text = " ".join(f"word{i}" for i in range(5000)) + " THE-LAST-WORD"
    a.summary_script("T", long_text)
    assert "THE-LAST-WORD" in seen["prompt"] and "word0 " in seen["prompt"]  # nothing cut
    assert seen["require_complete"] is True
    assert "700 words" in seen["system"] and "WHOLE" in seen["system"]
    assert Audio.summary_words("a b c") == 250


def test_thread_text_for_speech_has_every_message(tmp_path, monkeypatch):
    from ultra.live import Live

    live = Live(Config({"ledger": {"enabled": False}}), Store(tmp_path / "s.db"))
    msgs = [
        {
            "from": f"Person {i} <p{i}@x.org>",
            "ts": "2026-09-29T10:00:00-04:00",
            "body": f"Body of message {i}. " + "x" * 30000,
            "subject": "Big thread",
        }
        for i in range(1, 6)
    ]
    monkeypatch.setattr(live, "thread_any", lambda key: {"messages": msgs})
    s = live.thread_text_for("g-abc", for_speech=True)
    assert len(s) > 150_000  # the old 60,000-character tail cut is gone
    for i in range(1, 6):
        assert f"Message {i} of 5, from Person {i}" in s and f"Body of message {i}." in s
    assert "p1@x.org" not in s  # addresses are not read out


# ---------------------------------------------------------------- full context
FAKE = r"""
import json, sys
args = [a for a in sys.argv[1:] if a != "--json"]
if args[:2] == ["people", "show"] and args[2:3] == ["bcarter"]:
    print(json.dumps({"id": "11111111-1111-4111-8111-111111111111", "netid": "bcarter",
                      "name": "Ben Carter (bcarter)", "title": "Deputy", "dept": "RC",
                      "email_alias": "ben@example.org"}))
elif args[:1] == ["dossier"]:
    print(json.dumps({
        "researcher": {"id": "11111111-1111-4111-8111-111111111111", "netid": "bcarter",
                       "name": "Ben Carter", "title": "Deputy", "dept": "RC",
                       "email_alias": "ben@example.org"},
        "labs": [{"id": "L1", "name": "Lovelace Lab", "entity_type": "Lab"}],
        "assets": [], "grants": [], "projects": [],
        "interactions": [{"id": "I1", "date": "2026-09-28T10:00:00", "summary": "A" * 3000},
                         {"id": "I2", "date": "2026-09-01T10:00:00", "summary": "kickoff"}],
        "connections": [{"id": "G1", "name": "ada-lab", "entity_type": "GCPProject",
                         "connection_type": "USES", "role": None}],
        "external_state": ["entity_uuid=x tool_name='skywalker' data_json={'project_id': 'ada-lab', 'scan_time': '2026-09-20T01:00:00'}"]}))
elif args[:1] == ["search"]:
    print(json.dumps([
        {"id": "I1", "type": "Interaction", "name": "dup of linked", "score": 0.9, "reason": "x"},
        {"id": "I9", "type": "Interaction", "name": "Weekly sync: Ben to review", "score": 0.8,
         "reason": "Semantic (dist=0.4)"},
        {"id": "T2", "type": "Task", "name": "related task", "score": 0.7, "reason": "x"}]))
elif args[:2] == ["tasks", "list"]:
    print(json.dumps([
        {"id": "T1", "summary": "Send Ben Carter the plan", "status": "TODO", "priority": "HIGH"},
        {"id": "T2", "summary": "related task", "status": "TODO", "priority": "LOW"},
        {"id": "T3", "summary": "Courtney Carter-Smith quota", "status": "TODO", "priority": "HIGH"},
        {"id": "T4", "summary": "Ben from another team", "status": "TODO", "priority": "HIGH"},
        {"id": "T5", "summary": "Carter Lab storage", "status": "TODO", "priority": "MEDIUM"},
        {"id": "T6", "summary": "Send Ben Carter the old plan", "status": "DONE", "priority": "HIGH"}]))
else:
    print("unknown")
"""


def _ledger(tmp_path) -> Ledger:
    b = tmp_path / "nexus"
    b.write_text("#!/usr/bin/env python3\n" + FAKE)
    b.chmod(0o755)
    cfg = {"binary": str(b), "org_email_domain": "example.org", "netid_from_local_part": True}
    return Ledger(Config({"ledger": cfg}), Store(tmp_path / "s.db"))


def test_full_context_merges_dossier_search_and_tasks(tmp_path):
    lg = _ledger(tmp_path)
    c = lg.full_context("bcarter@example.org", "Ben Carter")
    assert [i["id"] for i in c["interactions"]] == ["I1", "I2"]
    assert len(c["interactions"][0]["summary"]) == 3000  # full text, not a 120-char name
    assert [m["id"] for m in c["mentions"]] == ["I9"]  # linked I1 not repeated
    why = {t["id"]: t["why"] for t in c["tasks"]}
    assert why == {"T1": "mentions", "T2": "search", "T5": "mentions"}
    assert "T3" not in why and "T4" not in why and "T6" not in why  # not him / done
    assert c["tasks"][0]["id"] == "T1"  # HIGH first
    assert c["gcp_projects"][0]["name"] == "ada-lab"
    assert c["cloud"] == [{"project": "ada-lab", "scanned": "2026-09-20"}]
    assert c["counts"] == {"linked": 2, "mentions": 1, "tasks": 3}
    txt = lg.full_context_text(c)
    assert "Logged interactions" in txt and "Weekly sync" in txt and "A" * 3000 in txt


def test_full_context_is_cached_and_read_only(tmp_path):
    lg = _ledger(tmp_path)
    lg.full_context("bcarter@example.org", "Ben Carter")
    lg.binary = "/nonexistent"  # a second call must not touch the CLI
    assert lg.full_context("bcarter@example.org", "Ben Carter")["counts"]["linked"] == 2
    with pytest.raises(Exception, match="not an allowed read command"):
        lg._run(["log", "x"])


def test_full_context_route_in_demo():
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    httpd.server_close()
    r = api.dispatch(
        "GET", "/api/person/full", {"addr": ["ben@example.org"], "name": ["Ben Carter"]}, None
    )[1]
    assert r["counts"]["linked"] == 2 and r["tasks"][0]["priority"] == "CRITICAL"
    assert json.dumps(r)  # serializable


# ---------------------------------------------------------------- row actions (v0.7.2)
def test_every_stream_row_offers_bucket_actions_without_dragging():
    from pathlib import Path

    js = (Path(__file__).parent.parent / "src/ultra/static/app.js").read_text()
    assert "${rowActs(it)}" in js  # rendered on every row
    for act in ('data-ra="bucket"', 'data-ra="log"', 'data-ra="task"', 'data-ra="block"'):
        assert act in js
    assert 'draggable="true"' in js  # dragging still available
    css = (Path(__file__).parent.parent / "src/ultra/static/app.css").read_text()
    assert "@media (hover: none)" in css  # phones always see them


def test_row_bucket_actions_accept_every_stream_kind():
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    httpd.server_close()
    for key in ("g-100", "s-D0DEMO1", "k-200"):
        st, r = api.dispatch("POST", "/api/bucket", {}, {"kind": "conversation", "key": key})[:2]
        assert st == 200, (key, r)
    st, r = api.dispatch(
        "POST",
        "/api/bucket",
        {},
        {
            "kind": "entity",
            "type": "Task",
            "id": "22222222-2222-4222-8222-222222222222",
            "name": "a task",
        },
    )[:2]
    assert st == 200 and len(r["items"]) == 4
