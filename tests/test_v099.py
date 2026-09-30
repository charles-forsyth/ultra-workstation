"""v0.9.9 drafts: sourced composer AI, check kept with versions, before/after, Tidy,
learning from edits."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

import pytest

from ultra.compose import Composer
from ultra.config import Config
from ultra.drafttools import compare, cut_sentences, tidy, word_diff
from ultra.learn import MIN_REPEATS, EditLearner, edits_between
from ultra.store import Store

ME = "ada@example.org"
DESK = "desk@tickets.example.org"
PAT = r"Ref:MSG\d{6,12}"


def _thread(key: str = "g-thr1") -> dict[str, Any]:
    return {
        "key": key,
        "messages": [
            {
                "id": "m1",
                "from": "Ben <ben@example.org>",
                "from_addr": "ben@example.org",
                "to": ME,
                "cc": "",
                "subject": "Quota",
                "body": "Is the 20 TB quota approved?",
                "mine": False,
                "message_id": "<m1@x>",
                "references": "",
                "ts": "2026-09-29T10:00:00",
            },
        ],
    }


@pytest.fixture
def comp(tmp_path: Path) -> Composer:
    return Composer(Config({"mail": {"send_delay_seconds": 0}}), Store(tmp_path / "t.db"), {ME})


# ---------------------------------------------------------------- drafttools
def test_word_diff_rebuilds_both_texts():
    a, b = (
        "Hi Ben,\n\nThe quota is approved.\n\nAda",
        "Hi Ben,\n\nThe 20 TB quota is approved today.\n\nAda",
    )
    ops = word_diff(a, b)
    assert "".join(o["text"] for o in ops if o["op"] != "del") == b
    kept = "".join(o["text"] for o in ops if o["op"] != "add")
    assert kept.split() == a.split()  # same words; whitespace may come from the new text
    assert any(o["op"] == "add" and "20 TB" in o["text"] for o in ops)


def test_compare_reports_fields_and_counts():
    c = compare(
        {"version": 1, "to_addrs": "a@x.org", "subject": "S", "body": "one two three"},
        {"version": 2, "to_addrs": "b@x.org", "subject": "S", "body": "one three four"},
    )
    assert c["fields"] == [{"field": "To", "old": "a@x.org", "new": "b@x.org"}]
    assert c["added_words"] == 1 and c["removed_words"] == 1 and not c["same"]
    assert compare({"body": "x"}, {"body": "x"})["same"]


def test_cut_sentences_exact_and_flexible_whitespace():
    body = "Hi Ben,\n\nThe quota is approved. Happy to set up a call.\n\nAda"
    assert (
        cut_sentences(body, ["Happy to set up a call."])
        == "Hi Ben,\n\nThe quota is approved.\n\nAda"
    )
    body2 = "Hi,\n\nIt usually takes\nabout a day. Thanks.\n\nAda"
    assert cut_sentences(body2, ["It usually takes about a day."]) == "Hi,\n\nThanks.\n\nAda"


def test_tidy_fixes_form_only():
    body = "Hi Ben,\n\nHi Ben,\n\nThe quota  is approved \u2014 done.   \n\n\n\nAda\n\nAda\n"
    text, changes = tidy(body, "Ada")
    assert text == "Hi Ben,\n\nThe quota is approved - done.\n\nAda\n"
    assert {
        "typographic characters to plain ASCII",
        "repeated greeting",
        "repeated signature",
        "double spaces",
        "extra blank lines",
        "trailing spaces removed",
    } <= set(changes)
    # facts are never touched: every content word survives
    assert set(re.findall(r"[a-z]+", text.lower())) <= set(re.findall(r"[a-z]+", body.lower()))


def test_tidy_places_the_ref_line_and_is_idempotent():
    text, changes = tidy("Hi\nRef:MSG10000003\n\nAda", "Ada", "Ref:MSG10000003", PAT)
    assert text == "Hi\n\nAda\n\nRef:MSG10000003\n" and changes
    again, changes2 = tidy(text, "Ada", "Ref:MSG10000003", PAT)
    assert again == text and changes2 == []


def test_tidy_leaves_clean_text_alone():
    assert tidy("Hi Ben,\n\nDone.\n\nAda\n", "Ada") == ("Hi Ben,\n\nDone.\n\nAda\n", [])


# ---------------------------------------------------------------- check kept with versions
def test_check_is_kept_per_version_and_filtered(comp):
    d = comp.create("reply", _thread(), ME)
    d = comp.save(
        d["id"],
        {"body": "Hi Ben,\n\nThe quota is approved. It usually takes about a day.\n\nAda"},
        "ai",
        "x",
    )
    v = d["current"]["version"]
    comp.put_check(
        d["id"],
        v,
        {
            "claims": [
                {
                    "text": "The quota is approved.",
                    "verdict": "supported",
                    "sources": ["M1"],
                    "action": "keep",
                },
                {"text": "It usually takes about a day.", "verdict": "unclear", "action": "cut"},
                {"text": "A sentence that is not in the draft at all.", "verdict": "unsupported"},
            ]
        },
    )
    d = comp.get(d["id"])
    ck = d["current"]["check"]
    assert ck["version"] == v and len(ck["claims"]) == 2
    assert ck["counts"] == {"supported": 1, "unsupported": 0, "unclear": 1}
    d2 = comp.save(d["id"], {"body": "Hi Ben,\n\nThe quota is approved.\n\nAda"}, "me")
    assert d2["current"]["check"] is None  # a new version is not checked until you check it


def test_review_shows_check_and_changes_since_ai(comp):
    d = comp.create("reply", _thread(), ME)
    d = comp.save(
        d["id"], {"body": "Hi Ben,\n\nApproved. Happy to set up a call.\n\nAda"}, "ai", "x"
    )
    d = comp.save(d["id"], {"body": "Hi Ben,\n\nApproved.\n\nAda"}, "me")
    comp.put_check(
        d["id"], d["current"]["version"], {"claims": [{"text": "Approved.", "verdict": "unclear"}]}
    )
    comp.approve(d["id"])
    r = comp.review(d["id"])
    assert r["check"]["counts"]["unclear"] == 1
    assert r["last_ai"]["removed_words"] == 6 and r["last_ai"]["added_words"] == 0


# ---------------------------------------------------------------- composer AI through Draft Studio
class _Studio:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gather = {"mode": "thread"}

    def ensure(self, key: str, need_brief: bool = False, timeout: float = 120) -> dict[str, Any]:
        self.calls.append(f"ensure {key} {need_brief}")
        return {"gather": self.gather, "brief": {"asks": ["a"]}}

    def draft(self, g: Any, br: Any, answers: Any, extra: str = "") -> dict[str, Any]:
        self.calls.append(f"draft {extra}")
        return {
            "body": "Hi Ben,\n\nThe quota is approved \u2014 yes.\n\nAda",
            "claims": [],
            "model": "m",
            "seconds": 1,
        }

    def revise(self, g: Any, current: str, instruction: str) -> dict[str, Any]:
        self.calls.append(f"revise {instruction}")
        return {
            "body": current.replace("approved", "approved as of today"),
            "claims": [],
            "model": "m",
            "seconds": 1,
        }

    def check(self, g: Any, body: str, claims: Any, answers: Any) -> dict[str, Any]:
        self.calls.append("check")
        return {
            "claims": [
                {"text": s, "verdict": "unclear" if "today" in s else "supported"}
                for s in re.split(r"(?<=[.])\s+|\n+", body)
                if len(s.split()) > 3
            ]
        }


def _live(tmp_path: Path, comp: Composer) -> Any:
    from ultra.live import Live

    lv = Live.__new__(Live)
    lv.store = comp.store
    lv.compose = comp
    lv.studio = _Studio()
    lv.ai = type("A", (), {"enabled": True})()
    lv._wrap = lambda fn, *a: fn(*a)
    return lv


def test_composer_draft_and_revise_go_through_studio_and_are_checked(tmp_path, comp):
    lv = _live(tmp_path, comp)
    d = comp.create("reply", _thread(), ME)
    out = lv.r_draft_ai({}, {"instruction": ""}, re.match(r"(\d+)", str(d["id"])))
    assert lv.studio.calls[:3] == ["ensure g-thr1 True", "draft ", "check"]
    assert out["ai"]["sourced"] and out["current"]["author"] == "ai"
    assert "\u2014" not in out["current"]["body"]  # ASCII-fixed
    assert out["current"]["check"]["counts"]["supported"] >= 1
    out = lv.r_draft_ai({}, {"instruction": "say today"}, re.match(r"(\d+)", str(d["id"])))
    assert "revise say today" in lv.studio.calls and lv.studio.calls[-1] == "check"
    assert out["current"]["check"]["counts"]["unclear"] == 1


def test_new_email_and_forward_keep_the_plain_path(tmp_path, comp):
    lv = _live(tmp_path, comp)
    d = comp.create("new", None, ME)
    assert lv._studio_ai(d, "") is None
    f = comp.create("forward", _thread(), ME)
    assert lv._studio_ai(f, "") is None
    assert lv.studio.calls == []


def test_task_draft_uses_the_task_key(tmp_path, comp):
    lv = _live(tmp_path, comp)
    d = comp.create("new", None, ME)
    comp.store.cache_put(f"draft:{d['id']}:task", "12345678-1234-4234-8234-123456789abc")
    assert lv._studio_key(comp.get(d["id"])) == "t-12345678-1234-4234-8234-123456789abc"


def test_cut_route_cuts_only_flagged_sentences(tmp_path, comp):
    lv = _live(tmp_path, comp)
    d = comp.create("reply", _thread(), ME)
    d = comp.save(
        d["id"],
        {"body": "Hi Ben,\n\nThe quota is approved. It usually takes about a day.\n\nAda"},
        "ai",
        "x",
    )
    comp.put_check(
        d["id"],
        d["current"]["version"],
        {
            "claims": [
                {"text": "The quota is approved.", "verdict": "supported"},
                {"text": "It usually takes about a day.", "verdict": "unclear"},
            ]
        },
    )
    m = re.match(r"(\d+)", str(d["id"]))
    out = lv.r_draft_cut(
        {}, {"texts": ["It usually takes about a day.", "The quota is approved."]}, m
    )
    assert (
        out["current"]["body"] == "Hi Ben,\n\nThe quota is approved.\n\nAda"
    )  # supported one kept
    assert out["current"]["author"] == "me"
    with pytest.raises(Exception, match="nothing to cut"):
        lv.r_draft_cut({}, {"texts": ["not flagged"]}, m)


def test_studio_hand_off_keeps_its_check(tmp_path, comp):
    lv = _live(tmp_path, comp)
    d = comp.create("reply", _thread(), ME)
    ck = {
        "claims": [
            {"text": "The quota is approved.", "verdict": "supported", "action": "keep"},
            {"text": "Cut me.", "verdict": "unsupported", "action": "cut"},
            {"text": "Hi Ben,", "verdict": "maybe"},
        ]
    }
    out = lv.r_draft_studio(
        {},
        {"body": "Hi Ben,\n\nThe quota is approved.\n\nAda", "check": ck},
        re.match(r"(\d+)", str(d["id"])),
    )
    kept = out["current"]["check"]["claims"]
    assert [c["text"] for c in kept] == [
        "The quota is approved."
    ]  # cut text and bad verdicts dropped


# ---------------------------------------------------------------- learning from edits
def test_edits_between_finds_removed_and_replaced():
    ai = "Hi Ben,\n\nThe quota is approved. Happy to set up a quick call if that helps.\n\nBest regards,\nAda"
    sent = "Hi Ben,\n\nThe quota is approved.\n\nThanks,\nAda"
    e = edits_between(ai, sent)
    assert "happy to set up a quick call if that helps" in e["removed"]
    assert ["best regards", "thanks"] in e["replaced"]


def _sent_draft(comp: Composer, ai_body: str, sent_body: str) -> dict[str, Any]:
    d = comp.create("reply", _thread(), ME)
    comp.save(d["id"], {"body": ai_body}, "ai", "x")
    d = comp.save(d["id"], {"body": sent_body}, "me")
    comp.approve(d["id"])
    return comp.get(d["id"])


def test_suggestion_after_repeats_and_accept_writes_style(tmp_path, comp):
    style = tmp_path / "style.toml"
    style.write_text('[signature]\ntext = "Ada"\n')
    L = EditLearner(comp.store, style)
    variants = [
        "Happy to set up a call if useful.",
        "I am happy to set up a call this week.",
        "Happy to set up a call anytime.",
    ]
    for i, v in enumerate(variants):
        assert L.suggestions() == []  # nothing until the edit repeats MIN_REPEATS times
        L.record(
            _sent_draft(comp, f"Hi Ben,\n\nDone {i}. {v}\n\nAda", f"Hi Ben,\n\nDone {i}.\n\nAda")
        )
    s = L.suggestions()
    assert (
        s and s[0]["kind"] == "cut" and "happy to set up a" in s[0]["phrase"] and s[0]["count"] == 3
    )
    rule = L.accept_rule(s[0]["key"])
    cfg = tomllib.loads(style.read_text())
    assert cfg["signature"]["text"] == "Ada"  # existing settings kept
    assert (
        cfg["forbid"][-1]["pattern"] == rule["pattern"] and cfg["forbid"][-1]["level"] == "warning"
    )
    assert re.search(rule["pattern"], "Hi, happy to set up a call.")
    assert list(tmp_path.glob("style.toml.bak_*"))  # backed up first
    assert L.suggestions() == []  # accepted: not offered again


def test_dismissed_suggestion_does_not_return(tmp_path, comp):
    L = EditLearner(comp.store, tmp_path / "style.toml")
    for i in range(MIN_REPEATS):
        L.record(
            _sent_draft(
                comp,
                f"Hi,\n\nX {i}. Sorry for the slow reply here.\n\nAda",
                f"Hi,\n\nX {i}.\n\nAda",
            )
        )
    s = L.suggestions()
    assert s
    L.dismiss(s[0]["key"])
    assert all(x["key"] != s[0]["key"] for x in L.suggestions())
    assert not (tmp_path / "style.toml").exists()  # nothing written without accept


def test_no_ai_version_means_nothing_recorded(tmp_path, comp):
    L = EditLearner(comp.store, tmp_path / "style.toml")
    d = comp.create("reply", _thread(), ME)
    d = comp.save(d["id"], {"body": "Hi,\n\nMine.\n\nAda"}, "me")
    assert L.record(comp.get(d["id"])) is None
    assert L.stats()["sends"] == 0


def test_generic_short_edits_are_not_learned(tmp_path, comp):
    L = EditLearner(comp.store, tmp_path / "style.toml")
    for i in range(MIN_REPEATS + 1):
        L.record(
            _sent_draft(comp, f"Hi,\n\nPoint {i}. Thanks.\n\nAda", f"Hi,\n\nPoint {i}.\n\nAda")
        )
    assert L.suggestions() == []


def test_learned_rule_file_always_parses(tmp_path, comp):
    style = tmp_path / "style.toml"
    L = EditLearner(comp.store, style)
    for i in range(MIN_REPEATS):
        L.record(
            _sent_draft(
                comp,
                f"Hi,\n\nY {i}. Let me know if you'd like to chat.\n\nAda",
                f"Hi,\n\nY {i}.\n\nAda",
            )
        )
    s = L.suggestions()[0]
    L.accept_rule(s["key"])
    tomllib.loads(style.read_text())
    assert json.dumps(s)  # suggestion is JSON-serializable for the API


def test_accept_refuses_to_write_a_broken_style_file(tmp_path, comp):
    style = tmp_path / "style.toml"
    style.write_text("[signature\ntext = broken")  # already unparseable
    L = EditLearner(comp.store, style)
    for i in range(MIN_REPEATS):
        L.record(
            _sent_draft(
                comp, f"Hi,\n\nZ {i}. Happy to jump on a call.\n\nAda", f"Hi,\n\nZ {i}.\n\nAda"
            )
        )
    s = L.suggestions()[0]
    with pytest.raises(tomllib.TOMLDecodeError):
        L.accept_rule(s["key"])
    assert style.read_text() == "[signature\ntext = broken"  # untouched
    assert L.suggestions()  # still open


def test_review_does_not_report_a_recipient_in_to_as_dropped(comp):
    """Python 3.13 getaddresses drops everything when a list holds two blank fields;
    To with empty Cc and Bcc used to be reported as 'not included from the thread'."""
    from ultra.compose import addresses

    assert addresses("Ben Carter <ben@example.org>", "", "") == [("Ben Carter", "ben@example.org")]
    assert addresses("", "a@x.org, B <b@y.org>", "") == [("", "a@x.org"), ("B", "b@y.org")]
    d = comp.create("reply_all", _thread(), ME)
    d = comp.save(d["id"], {"body": "Hi Ben,\n\nOk.\n\nAda"}, "me")
    comp.approve(d["id"])
    assert comp.review(d["id"])["dropped"] == []


def test_reply_all_keeps_everyone_when_cc_is_blank(comp):
    th = _thread()
    th["messages"][0]["to"] = f"{ME}, Cy Dunn <cy@example.org>, dee@example.org"
    th["messages"][0]["cc"] = ""
    d = comp.create("reply_all", th, ME)
    assert "cy@example.org" in d["current"]["cc"] and "dee@example.org" in d["current"]["cc"]
