"""v0.9.5 Draft Studio: gather, precedents, sources, house facts, brief, draft, check."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ultra.sources import HouseFacts, Sources, html_text, key_terms, passages, terms
from ultra.store import Store
from ultra.studio import Studio, StudioError, _clean_brief, _json_from, _strip_quoted

KEY = "g-abc123"
ME = "ada@example.org"


def _msgs() -> list[dict[str, Any]]:
    return [
        {
            "id": "m1",
            "from": "Ben Carter <ben@example.org>",
            "to": f"Ada <{ME}>",
            "cc": "Cy Dunn <cy@example.org>",
            "ts": "2026-09-29T10:00:00+00:00",
            "mine": False,
            "subject": "LLM API access for research",
            "body": "Which LLM APIs are supported? How is usage billed, and can API charges "
            "go to a grant? We use only public genomic data.",
        }
    ]


class FakeAI:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def _gen(self, prompt: str, system: str, *a: Any, **k: Any) -> Any:
        self.calls.append((prompt, system))
        if '"picks"' in system:
            out: Any = {"picks": [{"n": 2, "why": "same ask"}, {"n": 1, "why": "billing"}]}
        elif '"asks"' in system:
            out = {
                "asks": ["Which LLM APIs are supported?", "How is usage billed?", "Grant?"],
                "constraints": ["public data only"],
                "audience": "faculty",
                "known": [
                    {"fact": "Public data only", "source": "[M1]"},
                    {"fact": "made up", "source": "Z9"},
                ],
                "unknown": [
                    {
                        "question": "Is provider X covered?",
                        "why": "policy",
                        "options": ["Yes", "No"],
                    }
                ],
                "need_from_sender": ["NetIDs"],
                "risks": ["a", "b", "c"],
                "plan": ["answer each"],
                "precedent_shape": "short answers first",
            }
        elif "verdict" in system:
            out = {
                "claims": [
                    {
                        "text": "Gemini is supported through a lab project.",
                        "verdict": "supported",
                        "sources": ["F1"],
                    },
                    {
                        "text": "Provider X is free for everyone.",
                        "verdict": "unsupported",
                        "sources": [],
                        "note": "no source",
                    },
                    {
                        "text": "Something paraphrased that is not in the draft",
                        "verdict": "unsupported",
                    },
                ]
            }
        else:
            out = {
                "body": "Hi Ben,\n\nGemini is supported through a lab project. Provider X is free "
                "for everyone. I'll confirm the rest.\n\nAda",
                "claims": [
                    {
                        "text": "Gemini is supported through a lab project.",
                        "sources": ["[F1]", "BOGUS"],
                    },
                    {"text": "Not in the body at all", "sources": ["M1"]},
                ],
            }
        return type(
            "R", (), {"text": json.dumps(out), "model": "fake", "seconds": 0.1, "tokens": 1}
        )()


def _studio(
    tmp_path: Path,
    search: Any = None,
    ai: Any = None,
    pages: dict[str, str] | None = None,
    notes_dir: str = "",
) -> Studio:
    store = Store(tmp_path / "s.db")
    facts = HouseFacts(store)
    facts.add("Gemini is supported through a lab project.", "gemini llm api", "test")
    facts.add("No dollar figures unless asked.", "", "test")
    facts.add("Storage is billed per TB.", "storage tb", "test")
    pages = pages or {}
    src = Sources(
        [{"kind": "page", "url": "https://policy.example.org/ai"}],
        store,
        fetch=lambda u: pages.get(
            u,
            "<html><title>AI</title><p>Gemini API access is for research. Grants may pay.</p></html>",
        ),
    )
    return Studio(
        store,
        ai or FakeAI(),
        lambda k: {"messages": _msgs()},
        search or (lambda q, n: []),
        lambda k, m: "Ben Carter (bcarter): lab admin.",
        src,
        facts,
        {ME},
        "Ada",
        notes_dir,
        lambda: "No apologies.",
        lambda: "Ada",
    )


# ---------------------------------------------------------------- text helpers
def test_html_text_drops_scripts_nav_and_keeps_title():
    t, body = html_text(
        "<title>T &amp; C</title><nav>menu</nav><script>x()</script><p>Hello<br>there</p>"
    )
    assert t == "T & C" and "menu" not in body and "x()" not in body and "Hello" in body


def test_key_terms_drop_generic_words_and_names():
    k = key_terms(
        "Dear team, which LLM API is supported for research? Thanks, Ben Carter",
        10,
        {"ben", "carter"},
    )
    assert "llm" in k and "api" in k
    assert not {"research", "dear", "thanks", "ben", "carter", "team"} & set(k)


def test_passages_pick_dense_windows():
    text = (
        ("filler " * 200)
        + "Gemini API access for research is covered by the program. "
        + ("filler " * 200)
    )
    ps = passages(text, ["gemini", "api", "covered"])
    assert ps and "Gemini API access" in ps[0]


def test_strip_quoted_keeps_only_the_new_text():
    body = "Hi Cy,\n\nShort answer.\n\nOn Mon, Sep 1, 2026 at 9:00 AM Cy <cy@example.org> wrote:\n> old"
    assert _strip_quoted(body) == "Hi Cy,\n\nShort answer."


def test_json_from_tolerates_fences_and_rejects_garbage():
    assert _json_from('```json\n{"a": {"b": 1}}\n```') == {"a": {"b": 1}}
    with pytest.raises(StudioError):
        _json_from("no json here")


# ---------------------------------------------------------------- sources
def test_sources_fetch_https_only(tmp_path):
    s = Sources([], Store(tmp_path / "s.db"))
    with pytest.raises(Exception, match="https"):
        s._http_get("file:///etc/passwd")
    assert s.page("http://example.org/").get("error")


def test_site_sources_rank_sitemap_pages_by_topic(tmp_path):
    sm = (
        "<urlset><loc>https://it.example.org/ai-guidance</loc><loc>https://it.example.org/parking</loc>"
        "<loc>https://it.example.org/image/logo.png</loc><loc>https://other.example.com/ai</loc></urlset>"
    )
    pages = {
        "https://it.example.org/sitemap.xml": sm,
        "https://it.example.org/ai-guidance": "<p>LLM API guidance: Gemini is available.</p>",
        "https://it.example.org/parking": "<p>Parking permits.</p>",
    }
    s = Sources(
        [{"kind": "site", "url": "https://it.example.org"}],
        Store(tmp_path / "s.db"),
        fetch=pages.__getitem__,
    )
    urls = s.sitemap("https://it.example.org")
    assert urls == [
        "https://it.example.org/ai-guidance",
        "https://it.example.org/parking",
    ]  # same host, no images
    r = s.relevant("Which LLM API guidance applies to Gemini?")
    assert [p["url"] for p in r["passages"]] == ["https://it.example.org/ai-guidance"]


def test_failed_source_is_reported_not_hidden(tmp_path):
    def boom(u: str) -> str:
        raise OSError("timed out")

    s = Sources(
        [
            {"kind": "page", "url": "https://down.example.org/"},
            {"kind": "site", "url": "https://gone.example.org"},
        ],
        Store(tmp_path / "s.db"),
        fetch=boom,
    )
    r = s.relevant("llm api billing")
    assert {f["url"] for f in r["failed"]} >= {
        "https://down.example.org/",
        "https://gone.example.org",
    }


def test_pages_are_cached(tmp_path):
    n = {"calls": 0}

    def f(u: str) -> str:
        n["calls"] += 1
        return "<p>x</p>"

    s = Sources([], Store(tmp_path / "s.db"), fetch=f)
    s.page("https://a.example.org/")
    s.page("https://a.example.org/")
    assert n["calls"] == 1


def test_kb_article_reads_the_portal_page_api(tmp_path):
    sid = "a" * 32
    payload = {
        "result": {
            "containers": [
                {
                    "rows": [
                        {
                            "columns": [
                                {
                                    "widgets": [
                                        {
                                            "widget": {
                                                "data": {
                                                    "number": "KB0012345",
                                                    "short_description": "Request an AI key",
                                                    "text": "<p>Step one.</p>",
                                                }
                                            }
                                        }
                                    ]
                                }
                            ]
                        }
                    ]
                }
            ]
        }
    }
    seen = {}

    def f(u: str) -> str:
        seen["u"] = u
        return json.dumps(payload)

    s = Sources([], Store(tmp_path / "s.db"), fetch=f)
    a = s.kb_article("https://help.example.org", sid, "portal")
    assert a["title"] == "KB0012345 Request an AI key" and "Step one." in a["text"]
    assert "/api/now/sp/page?id=kb_article&sys_id=" + sid in seen["u"]
    assert s.kb_article("https://help.example.org", "not-an-id", "portal").get("error")


# ---------------------------------------------------------------- house facts
def test_house_facts_crud_and_relevance(tmp_path):
    f = HouseFacts(Store(tmp_path / "s.db"))
    a = f.add("Gemini is supported through a lab project.", "gemini llm")
    f.add("No dollar figures unless asked.", "")
    f.add("Storage is billed per TB.", "storage")
    rel = [x["text"] for x in f.relevant("Which LLM can we use?")]
    assert "Gemini is supported through a lab project." in rel
    assert "No dollar figures unless asked." in rel  # no topics: always applies
    assert "Storage is billed per TB." not in rel
    f.update(a["id"], enabled=False)
    assert "Gemini is supported through a lab project." not in [
        x["text"] for x in f.relevant("llm")
    ]
    with pytest.raises(ValueError):
        f.add("x")
    f.delete(a["id"])
    assert all(x["id"] != a["id"] for x in f.all())


# ---------------------------------------------------------------- gather
def test_gather_collects_everything_and_skips_this_thread(tmp_path):
    def search(q: str, n: int) -> list[dict[str, Any]]:
        if "in:sent" in q:
            return [
                {
                    "id": "s1",
                    "thread_id": "abc123",
                    "mine": True,
                    "subject": "same thread",
                    "to": "x",
                    "cc": "",
                    "date": "2026-09-01",
                    "body": "x" * 400,
                },
                {
                    "id": "s2",
                    "thread_id": "t2",
                    "mine": True,
                    "subject": "Re: LLM API billing question",
                    "to": "Dee <dee@example.org>",
                    "cc": "",
                    "date": "2026-09-10",
                    "body": "Hi Dee,\n\nGemini API access runs through a lab project and is billed to the "
                    "program up to a monthly limit; a grant can pay beyond that. "
                    + "More detail. "
                    * 30,
                },
                {
                    "id": "s3",
                    "thread_id": "t3",
                    "mine": True,
                    "subject": "Parking",
                    "to": "Eli <eli@example.org>",
                    "cc": "",
                    "date": "2026-09-11",
                    "body": "Hi Eli, parking permits are renewed in June. " * 10,
                },
            ]
        return [
            {
                "id": "h1",
                "thread_id": "t9",
                "subject": "Earlier question",
                "from": "Ben <ben@example.org>",
                "date": "2026-08-01",
                "body": "hello",
                "mine": False,
            }
        ]

    st = _studio(tmp_path, search)
    stages: list[tuple[str, str]] = []
    g = st.gather(KEY, stage=lambda s, x: stages.append((s, x)))
    assert [p["addr"] for p in g["people"]] == ["ben@example.org", "cy@example.org"]  # never me
    assert g["history"] and g["history"][0]["thread_id"] == "t9"
    assert [p["id"] for p in g["precedents"]][:1] in (["s2"], ["s3"])
    assert all(p["thread_id"] != "abc123" for p in g["precedents"])
    assert g["policy"] and g["policy"][0]["url"] == "https://policy.example.org/ai"
    assert any("Gemini" in f["text"] for f in g["facts"])
    assert "Storage is billed" not in json.dumps(g["facts"])
    assert g["ledger"].startswith("Ben Carter")
    assert ("brief", "running") not in stages and ("thread", "done") in stages
    again = st.gather(KEY)
    assert again.get("cached") is True


def test_precedent_picker_uses_the_model_choice_and_falls_back(tmp_path):
    body = "Hi, Gemini API access and billing through the lab project, grant charges allowed. " * 6
    rows = [
        {
            "id": f"s{i}",
            "thread_id": f"t{i}",
            "mine": True,
            "subject": f"Re: API billing {i}",
            "to": f"p{i}@example.org",
            "cc": "",
            "date": f"2026-09-1{i}",
            "body": body + str(i),
        }
        for i in range(1, 4)
    ]
    st = _studio(tmp_path, lambda q, n: rows if "in:sent" in q else [])
    g = st.gather(KEY)
    ids = [p["id"] for p in g["precedents"]]
    assert len(ids) == 2 and all(p["why"] for p in g["precedents"])  # the model picked 2 of 3

    class NoAI:
        enabled = False

    (tmp_path / "b").mkdir(exist_ok=True)
    st2 = _studio(tmp_path / "b", lambda q, n: rows if "in:sent" in q else [], ai=NoAI())
    assert len(st2.gather(KEY, fresh=True)["precedents"]) == 3  # word ranking stands


def test_one_failing_stage_does_not_stop_the_rest(tmp_path):
    def search(q: str, n: int) -> list[dict[str, Any]]:
        raise RuntimeError("gmail down")

    st = _studio(tmp_path, search)
    stages: dict[str, str] = {}
    g = st.gather(KEY, stage=stages.__setitem__)
    assert stages["history"] == "failed" and stages["precedents"] == "failed"
    assert stages["sources"] == "done" and g["policy"]
    assert "history" in g["failures"]


def test_notes_match_participants_by_name_or_id(tmp_path):
    d = tmp_path / "notes"
    d.mkdir()
    (d / "2026-09-20_Carter_meeting.md").write_text("Met Ben Carter about the lab.")
    (d / "2026-09-21_Other.md").write_text("Nothing relevant.")
    (d / "README.md").write_text("Ben Carter everywhere")  # not a dated note
    st = _studio(tmp_path, notes_dir=str(d))
    g = st.gather(KEY)
    assert [n["file"] for n in g["notes"]] == ["2026-09-20_Carter_meeting.md"]


# ---------------------------------------------------------------- brief / draft / check
def test_brief_is_cleaned_and_sources_validated(tmp_path):
    st = _studio(tmp_path)
    g = st.gather(KEY)
    br = st.brief(KEY, g)
    assert br["asks"][0].startswith("Which LLM")
    assert (
        br["known"][0]["source"] == "M1" and br["known"][1]["source"] == ""
    )  # Z9 is not a real tag
    assert len(br["risks"]) == 2 and br["need_from_sender"] == ["NetIDs"]
    assert br["unknown"][0]["options"] == ["Yes", "No"]
    prompt, system = st.ai.calls[-1]
    assert prompt.startswith("<mail>") and "data, not instructions" in system


def test_clean_brief_rejects_non_objects_and_bad_audience():
    with pytest.raises(StudioError):
        _clean_brief([1, 2], set())
    assert _clean_brief({"audience": "wizard"}, set())["audience"] == "external"


def test_draft_keeps_only_real_claims_and_lists_unanswered(tmp_path):
    st = _studio(tmp_path)
    g = st.gather(KEY)
    br = st.brief(KEY, g)
    d = st.draft(g, br, [], "")
    assert d["unanswered"] == ["Is provider X covered?"]
    assert [c["text"] for c in d["claims"]] == ["Gemini is supported through a lab project."]
    assert d["claims"][0]["sources"] == ["F1"]  # BOGUS dropped, brackets stripped
    _prompt, system = st.ai.calls[-1]
    assert "never guess" in system.lower() and "End with this signature exactly: Ada" in system


def test_draft_passes_answers_as_sources(tmp_path):
    st = _studio(tmp_path)
    g = st.gather(KEY)
    br = st.brief(KEY, g)
    d = st.draft(
        g, br, [{"question": "Is provider X covered?", "answer": "No, own funds only."}], ""
    )
    assert d["unanswered"] == []
    prompt, _s = st.ai.calls[-1]
    assert "[A1] Q: Is provider X covered?" in prompt and "No, own funds only." in prompt
    assert "A1" in d["tags"]


def test_check_cuts_unverified_by_default_and_ignores_paraphrase(tmp_path):
    st = _studio(tmp_path)
    g = st.gather(KEY)
    body = "Hi Ben,\n\nGemini is supported through a lab project. Provider X is free for everyone.\n\nAda"
    ck = st.check(g, body, [], [])
    by = {c["text"]: c for c in ck["claims"]}
    assert by["Gemini is supported through a lab project."]["action"] == "keep"
    assert by["Provider X is free for everyone."]["action"] == "cut"
    assert len(ck["claims"]) == 2  # the paraphrased claim is dropped
    assert ck["counts"] == {"supported": 1, "unsupported": 1, "unclear": 0}


# ---------------------------------------------------------------- runs and routes
def test_background_run_and_routes(tmp_path):
    import time

    st = _studio(tmp_path)
    r = st.r_start({}, {"key": KEY}, None)  # type: ignore[arg-type]
    s = st.status(KEY)
    for _ in range(100):
        s = st.status(KEY)
        if s["state"] != "running":
            break
        time.sleep(0.05)
    assert s["state"] == "done" and s["brief"]["asks"] and s["summary"]["policy"]
    assert r["key"] == KEY
    with pytest.raises(Exception, match="email threads"):
        st.r_start({}, {"key": "s-slack1"}, None)  # type: ignore[arg-type]
    d = st.r_draft({}, {"key": KEY, "answers": [{"question": "q", "answer": ""}]}, None)  # type: ignore[arg-type]
    assert d["body"].startswith("Hi Ben")
    with pytest.raises(Exception, match="not ready"):
        st.r_draft({}, {"key": "g-other"}, None)  # type: ignore[arg-type]


def test_brief_edits_are_bounded(tmp_path):
    import time

    st = _studio(tmp_path)
    st.start(KEY)
    for _ in range(100):
        if st.status(KEY)["state"] != "running":
            break
        time.sleep(0.05)
    out = st.r_brief_edit(
        {}, {"key": KEY, "asks": ["  one  ", "", "two"], "audience": "nope"}, None
    )  # type: ignore[arg-type]
    assert out["brief"]["asks"] == ["one", "two"] and out["brief"]["audience"] == "faculty"


def test_house_fact_routes(tmp_path):
    st = _studio(tmp_path)
    n = len(st.r_facts({}, None, None)["facts"])  # type: ignore[arg-type]
    f = st.r_fact_add(
        {}, {"text": "Credits means the central program.", "topics": "credits"}, None
    )["fact"]  # type: ignore[arg-type]
    assert f["source"].startswith("operator, ")
    import re as _re

    fm = _re.match(r"(\d+)", str(f["id"]))
    assert fm
    st.r_fact_update({}, {"enabled": False}, fm)
    assert not next(x for x in st.facts.all() if x["id"] == f["id"])["enabled"]
    st.r_fact_delete({}, None, fm)
    assert len(st.r_facts({}, None, None)["facts"]) == n  # type: ignore[arg-type]
    with pytest.raises(Exception, match="3 to 600"):
        st.r_fact_add({}, {"text": "x"}, None)  # type: ignore[arg-type]


def test_terms_are_stable():
    assert terms("api api llm the and")[:2] == ["api", "llm"]


def test_precedents_exclude_this_thread_and_mail_not_sent_by_me(tmp_path):
    long = "Hi Dee, Gemini API access and billing run through the lab project; grants can pay. " * 6
    rows = [
        {
            "id": "same",
            "thread_id": "abc123",
            "mine": True,
            "subject": "Re: LLM API billing",
            "to": "dee@example.org",
            "cc": "",
            "date": "2026-09-20",
            "body": long,
        },
        {
            "id": "theirs",
            "thread_id": "t5",
            "mine": False,
            "subject": "Re: LLM API billing",
            "to": "dee@example.org",
            "cc": "",
            "date": "2026-09-19",
            "body": long,
        },
        {
            "id": "ok",
            "thread_id": "t6",
            "mine": True,
            "subject": "Re: LLM API billing",
            "to": "eve@example.org",
            "cc": "",
            "date": "2026-09-18",
            "body": long + "x",
        },
    ]

    class NoAI:
        enabled = False

    st = _studio(tmp_path, lambda q, n: rows if "in:sent" in q else [], ai=NoAI())
    ids = [p["id"] for p in st.gather(KEY)["precedents"]]
    assert ids == ["ok"]


def test_house_facts_are_never_served_from_the_gather_cache(tmp_path):
    st = _studio(tmp_path)
    g1 = st.gather(KEY)
    gem = next(f for f in g1["facts"] if f["text"].startswith("Gemini"))
    st.facts.update(gem["id"], enabled=False)
    new = st.facts.add("Credits means the central program.", "", "test")
    g2 = st.gather(KEY)
    assert g2.get("cached") is True
    texts = [f["text"] for f in g2["facts"]]
    assert "Gemini is supported through a lab project." not in texts
    assert "Credits means the central program." in texts
    import sqlite3

    with sqlite3.connect(tmp_path / "s.db") as c:
        raw = c.execute("SELECT value FROM kv_cache WHERE key LIKE 'studio:gather:%'").fetchall()
    assert raw and all('"facts":' not in r[0] for r in raw)
    st.facts.delete(new["id"])
