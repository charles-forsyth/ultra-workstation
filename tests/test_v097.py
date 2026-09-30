"""v0.9.7 Draft Studio task mode: an email from a task, with the same gather/brief/check."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from ultra.sources import HouseFacts, Sources
from ultra.store import Store
from ultra.studio import KEY_RE, Studio, StudioError

TID = "12345678-1234-4234-8234-123456789abc"
KEY = "t-" + TID
ME = "ada@example.org"


def _task(**kw: Any) -> dict[str, Any]:
    t = {
        "task": {
            "id": TID,
            "summary": "Follow up with Ben Carter on the storage quota move for ada-lab "
            "(RITM0123456) (Jira ZZQ-9)",
            "status": "IN_PROGRESS",
            "priority": "HIGH",
            "due_date": "2026-10-07 00:00:00",
            "details": {"jira_key": "ZZQ-9", "note": "quota 20 TB"},
        },
        "links": [
            {
                "id": "a",
                "name": "Ben Carter (bcarter)",
                "type": "Researcher",
                "edge": "REFERENCED_IN",
            },
            {"id": "b", "name": "Ada Lovelace (adal)", "type": "Researcher", "edge": "ASSIGNED_TO"},
        ],
    }
    t["task"].update(kw)
    return t


def _mail() -> list[dict[str, Any]]:
    return [
        {
            "id": "r1",
            "thread_id": "thr1",
            "from": "Ben Carter <ben@example.org>",
            "to": ME,
            "cc": "Cy Dunn <cy@example.org>",
            "subject": "Storage quota move for ada-lab",
            "date": "2026-09-28",
            "mine": False,
            "body": "The storage quota move for ada-lab is waiting on the 20 TB approval. RITM0123456.",
        },
        {
            "id": "r2",
            "thread_id": "news",
            "from": "Weekly Digest <newsletter@example.com>",
            "to": ME,
            "cc": "",
            "subject": "Storage news",
            "date": "2026-09-29",
            "mine": False,
            "body": "storage quota move ada-lab storage quota move",
        },
        {
            "id": "r3",
            "thread_id": "noise",
            "from": "Eve <eve@example.org>",
            "to": ME,
            "cc": "",
            "subject": "Lunch",
            "date": "2026-09-29",
            "mine": False,
            "body": "storage lunch",
        },
    ]


class FakeAI:
    enabled = True
    hide_prefix = "ZZQ-"

    def __init__(self, brief: dict[str, Any] | None = None, body: str | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.brief_out = brief
        self.body = body

    def _gen(self, prompt: str, system: str, *a: Any, **k: Any) -> Any:
        self.calls.append((prompt, system))
        if '"picks"' in system:
            out: Any = {"picks": []}
        elif '"status"' in system and "work task" in system:
            out = self.brief_out or {
                "status": "Ben is waiting on the 20 TB approval (ZZQ-9).",
                "purpose": "Follow up with Ben",
                "asks": ["Ask Ben whether the approval came through (Jira ZZQ-9)"],
                "to": ["Ben Carter <ben@example.org>", "mallory@evil.example"],
                "cc": ["cy@example.org", "ben@example.org"],
                "thread": "{thr1}",
                "subject": "Storage quota",
                "audience": "staff",
                "known": [{"fact": "Waiting on approval", "source": "[H1]"}],
                "unknown": [],
                "risks": [],
                "plan": [],
            }
        elif "verdict" in system:
            out = {
                "claims": [
                    {
                        "text": "The move is waiting on the 20 TB approval.",
                        "verdict": "supported",
                        "sources": ["H1"],
                    }
                ]
            }
        else:
            out = {
                "body": self.body
                or "Hi Ben,\n\nThe move is waiting on the 20 TB approval (ZZQ-9). Has it come "
                "through?\n\nHappy to set up a quick call if that helps.\n\nAda",
                "claims": [
                    {"text": "The move is waiting on the 20 TB approval.", "sources": ["T", "H1"]}
                ],
            }
        return type(
            "R", (), {"text": json.dumps(out), "model": "fake", "seconds": 0.1, "tokens": 1}
        )()


def _studio(
    tmp_path: Path,
    ai: Any = None,
    task: Any = None,
    mail: Any = None,
    ledger: str = "Ben Carter (bcarter) ben@example.org; Dee <dee@example.org>",
) -> Studio:
    store = Store(tmp_path / "s.db")
    facts = HouseFacts(store)
    facts.add("Storage quotas above 10 TB need the dean's approval.", "storage quota", "test")
    rows = mail if mail is not None else _mail()
    seen: list[str] = []

    def search(q: str, n: int) -> list[dict[str, Any]]:
        """A small Gmail stand-in: quoted phrases must appear, (a OR b) needs one word,
        bare words must all appear."""
        seen.append(q)
        if "in:sent" in q:
            return []
        q2 = re.sub(r"newer_than:\S+|-in:\S+", "", q)
        phrases = re.findall(r'"([^"]+)"', q2)
        q2 = re.sub(r'"[^"]+"', "", q2)
        ors = re.findall(r"\(([^)]*)\)", q2)
        q2 = re.sub(r"\([^)]*\)", "", q2)
        alls = q2.split()
        out = []
        for r in rows:
            text = (r["subject"] + " " + r["body"] + " " + r["from"] + " " + r["to"]).lower()
            if not all(p.lower() in text for p in phrases):
                continue
            if any(not any(w.lower() in text for w in o.split(" OR ")) for o in ors):
                continue
            if not all(w.lower() in text for w in alls):
                continue
            out.append(r)
        return out

    st = Studio(
        store,
        ai or FakeAI(),
        lambda k: {"messages": []},
        search,
        lambda k, m: ledger,
        Sources([], store),
        facts,
        {ME},
        "Ada Lovelace",
        "",
        lambda: "",
        lambda: "Ada",
        task_fn=(lambda tid: task if task is not None else _task()),
    )
    st._queries = seen  # type: ignore[attr-defined]
    return st


def test_key_accepts_task_uuids_only():
    assert KEY_RE.match(KEY) and KEY_RE.match("g-abc123")
    assert not KEY_RE.match("t-not-a-uuid") and not KEY_RE.match("t-" + TID.upper())


def test_task_gather_finds_related_mail_and_people(tmp_path):
    st = _studio(tmp_path)
    g = st.gather(KEY)
    assert g["mode"] == "task" and g["task"]["status"] == "IN_PROGRESS"
    assert [h["thread_id"] for h in g["history"]] == [
        "thr1"
    ]  # newsletter and one-word noise dropped
    assert [p["addr"] for p in g["people"]] == ["ben@example.org", "cy@example.org"]
    assert any('"RITM0123456"' in q for q in st._queries)  # type: ignore[attr-defined]
    rec = [r["addr"] for r in g["recipients"]]
    assert "ben@example.org" in rec and "dee@example.org" in rec  # mail + ledger text
    assert ME not in rec and not any("newsletter" in a for a in rec)
    assert any("dean" in f["text"] for f in g["facts"])


def test_internal_keys_never_leave_the_task(tmp_path):
    st = _studio(tmp_path)
    g = st.gather(KEY)
    assert "ZZQ-" not in g["subject"] and "ZZQ-" not in json.dumps(g["task"])
    assert "jira_key" not in g["task"]["details"]
    br = st.brief(KEY, g)
    assert "ZZQ-" not in br["status"] and all("ZZQ-" not in a for a in br["asks"])
    _p, system = st.ai.calls[-1]
    assert "Never write ticket keys that start with ZZQ-" in system
    d = st.draft(g, br, [])
    assert "ZZQ-" not in d["body"] and "20 TB approval" in d["body"]


def test_brief_recipients_come_only_from_the_material(tmp_path):
    st = _studio(tmp_path)
    g = st.gather(KEY)
    br = st.brief(KEY, g)
    assert br["to"] == ["ben@example.org"]  # mallory was never in the mail or the ledger
    assert br["cc"] == ["cy@example.org"]  # the To address is not repeated in Cc
    assert br["thread"] == "thr1"


def test_brief_thread_must_be_a_related_thread(tmp_path):
    ai = FakeAI(brief={"status": "x", "to": [], "thread": "{zzz999}", "asks": []})
    st = _studio(tmp_path, ai=ai)
    br = st.brief(KEY, st.gather(KEY))
    assert br["thread"] == "" and br["to"] == []


def test_task_draft_envelope_and_task_prompt(tmp_path):
    st = _studio(tmp_path)
    g = st.gather(KEY)
    br = st.brief(KEY, g)
    d = st.draft(g, br, [])
    assert d["envelope"]["to"] == ["ben@example.org"] and d["envelope"]["thread"] == "thr1"
    prompt, system = st.ai.calls[-1]
    assert "[T] THE WORK TASK" in prompt and "moves the work task [T] forward" in system
    assert "continues the existing thread" in system
    assert d["claims"][0]["sources"] == ["T", "H1"]


def test_new_email_subject_never_falls_back_to_the_task_text(tmp_path):
    ai = FakeAI(
        brief={"status": "x", "to": ["ben@example.org"], "thread": "", "subject": "", "asks": ["a"]}
    )
    st = _studio(tmp_path, ai=ai)
    g = st.gather(KEY)
    d = st.draft(g, st.brief(KEY, g), [])
    assert d["envelope"]["subject"] == ""


def test_reply_subject_comes_from_the_thread(tmp_path):
    ai = FakeAI(
        brief={
            "status": "x",
            "to": ["ben@example.org"],
            "thread": "{thr1}",
            "subject": "",
            "asks": ["a"],
        }
    )
    st = _studio(tmp_path, ai=ai)
    g = st.gather(KEY)
    d = st.draft(g, st.brief(KEY, g), [])
    assert d["envelope"]["subject"] == "Re: Storage quota move for ada-lab"


def test_rule_flags_cut_meeting_offers_even_if_the_checker_approved(tmp_path):
    st = _studio(tmp_path)
    g = st.gather(KEY)
    body = "Hi Ben,\n\nThe move is waiting on the 20 TB approval.\n\nHappy to set up a quick call if that helps.\n\nAda"
    ck = st.check(g, body, [], [])
    by = {c["text"]: c for c in ck["claims"]}
    assert by["Happy to set up a quick call if that helps."]["action"] == "cut"
    assert by["Happy to set up a quick call if that helps."].get("rule") is True
    assert by["The move is waiting on the 20 TB approval."]["action"] == "keep"


def test_rule_flags_do_not_fire_on_ordinary_text(tmp_path):
    st = _studio(tmp_path)
    assert st._rule_flags("Hi Ben,\n\nThe call for proposals closes Friday.\n\nAda") == []
    assert st._rule_flags("I'm sorry for the delay.")[0]["note"].startswith("Rule: no apolog")


def test_task_gather_survives_a_mail_failure(tmp_path):
    st = _studio(tmp_path)

    def boom(q: str, n: int) -> list[dict[str, Any]]:
        raise RuntimeError("gmail down")

    st.search_fn = boom
    stages: dict[str, str] = {}
    g = st.gather(KEY, fresh=True, stage=stages.__setitem__)
    assert stages["history"] == "failed" and g["history"] == [] and "history" in g["failures"]
    assert stages["ledger"] == "done"


def test_missing_task_is_an_error(tmp_path):
    st = _studio(tmp_path, task={"task": None, "error": "not found"})
    with pytest.raises(StudioError, match="not found"):
        st.gather(KEY)


def test_task_cache_changes_when_the_task_changes(tmp_path):
    st = _studio(tmp_path)
    g1 = st.gather(KEY)
    st.task_fn = lambda tid: _task(status="BLOCKED")
    g2 = st.gather(KEY)
    assert g1["version"] != g2["version"] and not g2.get("cached")


def test_start_route_accepts_tasks_and_needs_the_ledger(tmp_path):
    import time

    st = _studio(tmp_path)
    st.r_start({}, {"key": KEY}, None)  # type: ignore[arg-type]
    for _ in range(100):
        if st.status(KEY)["state"] != "running":
            break
        time.sleep(0.05)
    s = st.status(KEY)
    assert s["state"] == "done" and s["summary"]["mode"] == "task" and s["brief"]["to"]
    st.task_fn = None
    with pytest.raises(Exception, match="needs the ledger"):
        st.r_start({}, {"key": KEY}, None)  # type: ignore[arg-type]


def test_brief_edit_task_envelope(tmp_path):
    import time

    st = _studio(tmp_path)
    st.start(KEY)
    for _ in range(100):
        if st.status(KEY)["state"] != "running":
            break
        time.sleep(0.05)
    out = st.r_brief_edit(
        {},
        {
            "key": KEY,
            "to": ["dee@example.org", "not an address"],
            "cc": [],
            "thread": "bogus",
            "subject": "  Quota   update ",
            "purpose": "p",
        },
        None,  # type: ignore[arg-type]
    )["brief"]
    assert (
        out["to"] == ["dee@example.org"]
        and out["thread"] == ""
        and out["subject"] == "Quota update"
    )
    out = st.r_brief_edit({}, {"key": KEY, "to": ["zed@other.example"]}, None)["brief"]  # type: ignore[arg-type]
    assert out["typed_recipients"] == ["zed@other.example"]  # allowed, but shown as typed


# ---------------------------------------------------------------- hand-off to the composer (Live)
class _Comp:
    def __init__(self) -> None:
        self.made: list[tuple[str, Any]] = []
        self.saved: list[tuple[int, dict[str, Any], str, str]] = []
        self.drafts: dict[int, dict[str, Any]] = {}

    def create(self, kind: str, thread: Any, frm: str) -> dict[str, Any]:
        i = len(self.drafts) + 1
        self.made.append((kind, thread))
        self.drafts[i] = {
            "id": i,
            "kind": kind,
            "state": "DRAFT",
            "thread_id": (thread or {}).get("key", "")[2:] or None,
        }
        return self.drafts[i]

    def save(self, did: int, fields: dict[str, Any], author: str, label: str) -> dict[str, Any]:
        self.saved.append((did, fields, author, label))
        return {**self.drafts[did], "current": fields}

    def get(self, did: int) -> dict[str, Any]:
        return self.drafts[did]


def _live(tmp_path: Path) -> Any:
    from ultra.live import Live

    lv = Live.__new__(Live)
    lv.store = Store(tmp_path / "l.db")
    lv.compose = _Comp()
    lv.from_default = ME
    lv.mail = type(
        "M",
        (),
        {"thread": staticmethod(lambda tid: {"key": "g-" + tid, "messages": [{"id": "x"}]})},
    )()
    lv._wrap = lambda fn, *a: fn(*a)
    return lv


def test_from_task_new_email_validates_and_saves_as_ai(tmp_path):
    lv = _live(tmp_path)
    env = {"to": ["ben@example.org"], "cc": ["cy@example.org"], "subject": "Quota", "thread": ""}
    d = lv.r_draft_from_task({}, {"task": TID, "body": "Hi Ben \u2014 ok", "envelope": env}, None)
    kind, _t = lv.compose.made[0]
    did, fields, author, _label = lv.compose.saved[0]
    assert kind == "new" and author == "ai" and d["task_id"] == TID
    assert fields["to_addrs"] == "ben@example.org" and fields["cc"] == "cy@example.org"
    assert fields["body"] == "Hi Ben - ok"  # ASCII-fixed
    # same task, same shape: the draft is reused, not duplicated
    lv.r_draft_from_task({}, {"task": TID, "body": "v2", "envelope": env}, None)
    assert len(lv.compose.made) == 1 and lv.compose.saved[-1][0] == did


@pytest.mark.parametrize(
    "body,err",
    [
        ({"task": "nope", "body": "x", "envelope": {"to": ["a@b.org"], "subject": "s"}}, "task id"),
        ({"task": TID, "body": "  ", "envelope": {"to": ["a@b.org"], "subject": "s"}}, "empty"),
        (
            {"task": TID, "body": "x", "envelope": {"to": ["not-an-address"], "subject": "s"}},
            "not an email",
        ),
        ({"task": TID, "body": "x", "envelope": {"to": [], "subject": "s"}}, "To address"),
        ({"task": TID, "body": "x", "envelope": {"to": ["a@b.org"], "subject": ""}}, "subject"),
        ({"task": TID, "body": "x", "envelope": {"thread": "../x", "to": []}}, "thread"),
    ],
)
def test_from_task_refuses_bad_input(tmp_path, body, err):
    lv = _live(tmp_path)
    with pytest.raises(Exception, match=err):
        lv.r_draft_from_task({}, body, None)
    assert lv.compose.made == []


def test_from_task_reply_all_keeps_thread_recipients(tmp_path):
    lv = _live(tmp_path)
    lv.r_draft_from_task(
        {},
        {
            "task": TID,
            "body": "x",
            "envelope": {"thread": "thr1abc", "to": ["ignored@x.org"], "subject": "ignored"},
        },
        None,
    )
    kind, thread = lv.compose.made[0]
    fields = lv.compose.saved[0][1]
    assert kind == "reply_all" and thread["key"] == "g-thr1abc"
    assert set(fields) == {"body"}  # recipients and subject come from the thread
    r = lv.r_drafts_for_task({}, None, re.match(r"(.+)", TID))
    assert [d["id"] for d in r["drafts"]] == [1] and r["drafts"][0]["task_id"] == TID
