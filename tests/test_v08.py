"""v0.8 tests: item People (headers + bodies), item Full context, briefing, suggestions,
and Add to ledger (strict input, double confirmation, the only `people add` path)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from ultra.config import Config
from ultra.itemctx import Catalog, ItemContext, context_text, rank_people
from ultra.ledger_write import LedgerWriter, WriteError, check_person
from ultra.rules import Rules
from ultra.store import Store

CAT = {
    "people": [
        {"netid": "bcarter", "name": "Ben Carter", "title": "Deputy"},
        {"netid": "devans", "name": "Dee Evans", "title": "Budget"},
        {"netid": "mkent", "name": "Michael Kent", "title": "Prof"},
        {"netid": "jsmith1", "name": "Jo Smith", "title": ""},
        {"netid": "jsmith2", "name": "Jo Smith", "title": ""},
        {"netid": "ghopper", "name": "Grace Hopper", "title": ""},
        {"netid": "adal", "name": "Ada Lovelace", "title": "me"},
    ],
    "labs": [
        {"name": "Hopper Lab (ghopper)"},
        {"name": "Statistics"},
        {"name": "Research Computing"},
    ],
    "gcp": [{"project_id": "ada-lab", "name": "Ada Lab"}],
    "projects": [{"name": "Campus Storage"}, {"name": "Data"}],
    "grants": [{"c_number": "NSF-2400001", "title": "Storage"}],
    "assets": [{"name": "lab-share"}, {"name": "home"}],
}


def cat() -> Catalog:
    return Catalog(CAT, me={"adal"})


# ---------------------------------------------------------------- matching
def test_body_names_match_exactly_and_me_is_skipped():
    f = cat().find("Dee Evans approved it; Ada Lovelace wrote this; ask bcarter@example.org")
    assert set(f["people"]) == {"devans", "bcarter"}  # me (adal) is never a match
    assert f["people"]["bcarter"]["how"] == "address"


def test_nickname_and_shared_names_are_suggestions_not_matches():
    f = cat().find("Mike Kent will call. Jo Smith too.")
    assert "mkent" not in f["people"] and not any(p.startswith("jsmith") for p in f["people"])
    whys = {s["text"]: s["why"] for s in f["suggest"]}
    assert whys["Mike Kent"] == "nickname of a ledger name"
    assert "share this name" in whys["Jo Smith"]


def test_surname_only_suggests_but_capitalised_phrases_do_not():
    f = cat().find("Talk to Hopper about it. The Hopper Lab has space. Long Beach is fine.")
    texts = [s["text"] for s in f["suggest"]]
    assert "Hopper" in texts
    assert f["labs"] == {"hopper lab": "Hopper Lab (ghopper)"}


def test_org_units_and_common_words_are_not_entities():
    f = cat().find("Statistics and Research Computing met; the data is in home.")
    assert f["labs"] == {} and f["projects"] == {} and f["assets"] == {}


def test_ids_and_grants_match_on_word_boundaries():
    f = cat().find("Move ada-lab to lab-share under NSF-2400001; not ada-lab2 or xNSF-2400001.")
    assert list(f["gcp"]) == ["ada-lab"] and list(f["grants"]) == ["nsf-2400001"]
    assert list(f["assets"]) == ["lab-share"]


def test_rank_senders_named_to_cc_and_operator_recipient_counts_as_sender():
    rows = [
        {"name": "C", "roles": ["cc"], "addrs": ["c@x"], "sent": 0, "named_in": []},
        {"name": "N", "roles": ["named"], "addrs": [], "sent": 0, "named_in": [1]},
        {"name": "T", "roles": ["to"], "addrs": ["t@x"], "sent": 0, "named_in": []},
        {"name": "F", "roles": ["from"], "addrs": ["f@x"], "sent": 2, "named_in": []},
        {"name": "R", "roles": ["to"], "addrs": ["r@x"], "sent": 0, "named_in": []},
    ]
    assert [r["name"] for r in rank_people(rows, {"r@x"})] == ["F", "R", "N", "T", "C"]


# ---------------------------------------------------------------- the builder
class FakeLedger:
    enabled = True

    def __init__(self, n_people: int = 3) -> None:
        self.calls: list[tuple] = []
        self.n = n_people

    def catalog(self) -> dict:
        extra = [
            {"netid": f"p{i:02d}", "name": f"Person Number{chr(65 + i)}", "title": ""}
            for i in range(self.n)
        ]
        return {**CAT, "people": CAT["people"] + extra}

    def resolve(self, addr: str, name: str = "") -> dict | None:
        local = addr.split("@")[0]
        return (
            {"id": f"id-{local}", "netid": local, "name": name or local}
            if local in ("bcarter", "devans")
            else None
        )

    def dossier(self, netid: str) -> dict:
        self.calls.append(("dossier", netid))
        return {
            "researcher": {"id": f"id-{netid}", "netid": netid, "name": netid},
            "interactions": [
                {
                    "id": f"ix-{netid}-0001",
                    "date": f"2026-09-0{len(netid) % 9 + 1}T10:00:00",
                    "summary": f"log about {netid}",
                },
                {
                    "id": "ix-shared-0001",
                    "date": "2026-09-01T09:00:00",
                    "summary": "a meeting with several people",
                },
            ],
            "labs": [],
            "grants": [],
            "projects": [],
            "assets": [],
            "connections": [{"id": "task-1", "entity_type": "Task", "name": "t"}],
        }

    def tree(self, ident: str) -> dict:
        self.calls.append(("tree", ident))
        if ident == "ada-lab":
            return {
                "root": {"id": "g1", "type": "GCPProject", "name": "ada-lab"},
                "connections": [
                    {"id": "ix-tree-0001", "entity_type": "Interaction", "name": "budget alert set"}
                ],
            }
        return {
            "root": {"id": "big", "type": "ResearchProject", "name": ident},
            "connections": [
                {"id": f"x{i}", "entity_type": "Interaction", "name": "n"} for i in range(60)
            ],
        }

    def search_raw(self, term: str, limit: int = 25) -> list:
        self.calls.append(("search", term))
        return [{"id": "ix-topic-0001", "type": "Interaction", "name": "topic log"}]

    def open_tasks(self) -> dict:
        return {
            "task-1": {"summary": "linked task", "status": "TODO", "priority": "LOW"},
            "task-2": {"summary": "Call Dee Evans about it", "status": "TODO", "priority": "HIGH"},
            "task-3": {"summary": "unrelated", "status": "TODO", "priority": "HIGH"},
        }


def _ic(tmp_path, n: int = 3) -> tuple[ItemContext, FakeLedger]:
    led = FakeLedger(n)
    return ItemContext(led, Store(tmp_path / "s.db"), Rules(me={"ada@example.org"}), {"adal"}), led


MSGS = [
    {
        "from": "Ben Carter <bcarter@example.org>",
        "to": "Ada Lovelace <ada@example.org>",
        "cc": "",
        "subject": "Handover",
        "ts": "2026-09-28T17:54:00",
        "body": "Dee Evans signed off. Move ada-lab to lab-share. Campus Storage is fine.",
    },
    {
        "from": "Ada Lovelace <ada@example.org>",
        "to": "bcarter@example.org",
        "subject": "Re: Handover",
        "ts": "2026-09-29T09:00:00",
        "body": "Thanks.",
        "mine": True,
    },
]


def test_people_include_body_names_with_where(tmp_path):
    ic, _ = _ic(tmp_path)
    e = ic.entities("g-1", MSGS)
    by = {p["netid"]: p for p in e["people"]}
    assert set(by) == {"bcarter", "devans"}
    assert by["bcarter"]["roles"] == ["from", "to"] and by["devans"]["named_in"] == [1]
    assert by["devans"]["source"] == "body"
    assert [x["name"] for x in e["things"]["gcp"]] == ["ada-lab"]


def test_dossier_cap_is_ten(tmp_path):
    ic, led = _ic(tmp_path, n=15)
    body = " ".join(f"Person Number{chr(65 + i)}," for i in range(15))
    msgs = [{**MSGS[0], "body": body}]
    e = ic.entities("g-2", msgs)
    assert e["counts"]["dossiers"] == 10 and e["counts"]["people"] == 16
    ic.full("g-2", msgs)
    assert sum(1 for c in led.calls if c[0] == "dossier") == 10


def test_full_merges_dedupes_and_skips_platform_history(tmp_path):
    ic, _led = _ic(tmp_path)
    c = ic.full("g-1", MSGS)
    ids = [h["id"] for h in c["history"]]
    assert ids.count("ix-shared-0001") == 1  # same log from two dossiers: once
    shared = next(h for h in c["history"] if h["id"] == "ix-shared-0001")
    assert set(shared["who"]) == {"bcarter", "devans"}
    assert "ix-tree-0001" in ids and "ix-topic-0001" in ids  # tree + topic search
    assert "g-1#1" in ids and "g-1#2" in ids  # the item's own messages
    assert not any(i.startswith("x") and i[1:].isdigit() for i in ids)  # platform not merged
    assert c["broad"] and c["broad"][0]["name"] == "Campus Storage"
    tasks = {t["id"]: t["why"] for t in c["tasks"]}
    assert set(tasks) == {"task-1", "task-2"} and tasks["task-2"] == ["names someone here"]
    txt = context_text(c)
    assert "THIS ITEM" in txt and "task task-2" in txt and "Dee Evans signed off" in txt


def test_full_is_cached_per_item_version(tmp_path):
    ic, led = _ic(tmp_path)
    ic.full("g-1", MSGS)
    n = len(led.calls)
    ic.full("g-1", MSGS)
    assert len(led.calls) == n  # cached
    ic.full("g-1", [*MSGS, {**MSGS[1], "ts": "2026-09-30T09:00:00", "body": "new"}])
    assert len(led.calls) > n  # new message -> rebuilt


def test_confirmed_suggestion_joins_the_group(tmp_path):
    ic, led = _ic(tmp_path)
    c = ic.full("g-1", MSGS, confirmed=["ghopper", "not-in-ledger", "-x"])
    assert "ghopper" in {p["netid"] for p in c["people"]}
    assert ("dossier", "ghopper") in led.calls
    assert not any(p["netid"] in ("not-in-ledger", "-x") for p in c["people"])


# ---------------------------------------------------------------- add to ledger: checks
@pytest.mark.parametrize(
    ("netid", "name", "title"),
    [
        ("-rf", "Zoe Doe", ""),  # option-looking netid
        ("Zdoe", "Zoe Doe", ""),  # uppercase
        ("z", "Zoe Doe", ""),  # too short
        ("zdoe", "zoe", ""),  # one word, lowercase
        ("zdoe", "Zoe Doe [/x]", ""),  # markup
        ("zdoe", "Zoe D0e", ""),  # digit
        ("zdoe", "Zoë Doe", ""),  # non-ASCII
        ("zdoe", "Zoe Doe", "--force"),  # option-looking title
        ("zdoe", "Zoe Doe", "a" * 130),  # too long
        ("zdoe", "Zoe Doe <z@x>", ""),
    ],
)
def test_check_person_rejects(netid, name, title):
    with pytest.raises(WriteError):
        check_person(netid, name, title)


def test_check_person_accepts_and_normalises():
    f = check_person("zdoe", "  Zoe   O'Neil-Doe ", "Research Analyst II", "Research Computing")
    assert f == {
        "netid": "zdoe",
        "name": "Zoe O'Neil-Doe",
        "title": "Research Analyst II",
        "dept": "Research Computing",
    }


def test_people_add_is_not_reachable_through_the_general_write_path(tmp_path):
    w = LedgerWriter(Config({"ledger": {"binary": "true"}}), Store(tmp_path / "s.db"))
    w.enabled = True
    with pytest.raises(WriteError, match="only allowed through the Add to ledger"):
        w._run(["people", "add", "--", "zdoe", "Zoe Doe"])
    with pytest.raises(WriteError, match="not an allowed write command"):
        w._run(["people", "delete", "zdoe"])


def test_person_add_runs_argv_with_double_dash_and_reads_back(tmp_path):
    w = LedgerWriter(Config({"ledger": {"binary": "true"}}), Store(tmp_path / "s.db"))
    w.enabled = True
    seen: dict[str, Any] = {}
    state = {"exists": False}

    def fake_run(args, stdin=None, timeout=0, _people_add=False):
        seen.update(args=args, flag=_people_add)
        state["exists"] = True
        return 0, "Added Zoe Doe"

    w._run = fake_run  # type: ignore[method-assign]
    exists = lambda n: {"id": "u1", "netid": n, "name": "Zoe Doe"} if state["exists"] else None  # noqa: E731
    r = w.person_add({"netid": "zdoe", "name": "Zoe Doe", "title": "Analyst"}, exists)
    assert r["ok"] and r["record"]["netid"] == "zdoe"
    assert seen["args"] == ["people", "add", "--title", "Analyst", "--", "zdoe", "Zoe Doe"]
    assert seen["flag"] is True
    with pytest.raises(WriteError, match="already in the ledger"):
        w.person_add({"netid": "zdoe", "name": "Zoe Doe"}, exists)


# ---------------------------------------------------------------- add to ledger: routes
def _demo():
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    httpd.server_close()
    return api


def _post(api, path, body):
    """(status, body); an ApiError becomes its status and {"error": message}."""
    from ultra.server import ApiError

    try:
        st, r = api.dispatch("POST", path, {}, body)[:2]
    except ApiError as e:
        return e.status, {"error": str(e.args[-1] if e.args else e)}
    return st, r


def test_add_needs_both_confirmations_and_tokens_are_single_use():
    api = _demo()
    f = {"netid": "zdoe", "name": "Zoe Doe", "title": "Analyst", "dept": ""}
    st, r1 = _post(api, "/api/person/add/check", f)
    assert st == 200 and r1["token"]
    # skipping step 2: the check token cannot commit
    st, r = _post(api, "/api/person/add/commit", {**f, "token": r1["token"]})
    assert st == 409
    assert not [c for c in api.demo_writer.calls if c[0] == "person_add"]
    # the check token was spent by that attempt; start again
    st, r1 = _post(api, "/api/person/add/check", f)
    st, r2 = _post(api, "/api/person/add/confirm", {**f, "token": r1["token"]})
    assert st == 200
    # changing a field after confirming is refused
    st, r = _post(api, "/api/person/add/commit", {**f, "name": "Zed Doe", "token": r2["token"]})
    assert st == 409 and "changed" in r["error"]
    assert not [c for c in api.demo_writer.calls if c[0] == "person_add"]
    st, r1 = _post(api, "/api/person/add/check", f)
    st, r2 = _post(api, "/api/person/add/confirm", {**f, "token": r1["token"]})
    st, r3 = _post(api, "/api/person/add/commit", {**f, "token": r2["token"]})
    assert st == 200 and r3["ok"] and r3["record"]["netid"] == "zdoe"
    assert [c for c in api.demo_writer.calls if c[0] == "person_add"] == [("person_add", f)]
    # replaying the same token does nothing: the token is gone, nothing runs again
    st, r = _post(api, "/api/person/add/commit", {**f, "token": r2["token"]})
    assert st == 409 and "already used" in r["error"]
    assert len([c for c in api.demo_writer.calls if c[0] == "person_add"]) == 1
    # and the netid now exists: a new check is refused
    st, r = _post(api, "/api/person/add/check", f)
    assert st == 409


def test_add_check_rejects_bad_input_and_existing_netids():
    api = _demo()
    st, r = _post(api, "/api/person/add/check", {"netid": "-rf", "name": "Zoe Doe"})
    assert st == 400
    st, r = _post(api, "/api/person/add/check", {"netid": "bcarter", "name": "Ben Carter"})
    assert st == 409 and "already in the ledger" in r["error"]
    st, r = _post(api, "/api/person/add/check", {"netid": "bcarter2", "name": "Ben Carter"})
    assert st == 200 and any("exact name" in w for w in r["warnings"])


def test_add_tokens_expire(monkeypatch):
    api = _demo()
    f = {"netid": "zdoe", "name": "Zoe Doe"}
    st, r1 = _post(api, "/api/person/add/check", f)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 400)
    st, _r = _post(api, "/api/person/add/confirm", {**f, "token": r1["token"]})
    assert st == 409


# ---------------------------------------------------------------- routes: people/full/briefing
def test_item_routes_in_demo():
    api = _demo()
    st, p = api.dispatch("GET", "/api/item/people/g-100", {}, None)[:2]
    assert st == 200
    names = {x["netid"]: x for x in p["people"]}
    assert {"bcarter", "devans", "efox"} <= set(names) and names["devans"]["named_in"] == [1]
    st, j = _post(api, "/api/item/full", {"key": "g-100"})
    for _ in range(50):
        st, s = api.dispatch("GET", f"/api/item/full/job/{j['job']}", {}, None)[:2]
        if s["state"] != "running":
            break
        time.sleep(0.05)
    assert s["state"] == "done" and s["result"]["counts"]["dossiers_loaded"] >= 3
    st, b = _post(api, "/api/item/briefing", {"key": "g-100"})
    assert st == 200 and b["markdown"].startswith("## What this is about")
    st, sg = _post(api, "/api/item/suggest", {"key": "g-100"})
    assert st == 200 and sg["suggest"][0]["name"] == "Campus Storage"
    from ultra.server import ApiError

    with pytest.raises(ApiError):
        api.dispatch("GET", "/api/item/people/x-1", {}, None)


def test_briefing_can_only_become_a_staged_card():
    api = _demo()
    st, card = _post(
        api,
        "/api/ledger/stage-briefing",
        {
            "key": "g-100",
            "text": "Briefing: x\n\nEdited.",
            "entities": [
                {"id": "11111111-1111-4111-8111-111111111111", "name": "Ben", "type": "Researcher"},
                {"id": "not-a-uuid", "name": "bad"},
            ],
        },
    )
    assert st == 200 and card["state"] == "staged" and card["text"].endswith("Edited.")
    assert not [c for c in api.demo_writer.calls if c[0] == "log"]  # nothing written yet
    st, r = _post(api, "/api/ledger/stage-briefing", {"key": "g-100", "text": "  "})
    assert st == 400 and "empty" in r["error"]


def test_briefing_prompt_requires_citations_and_no_internal_keys():
    from ultra.ai import AI

    seen: dict[str, Any] = {}
    ai = AI(Config({}))
    ai._gen = lambda p, s, m=0, **kw: (
        seen.update(p=p, s=s, kw=kw)
        or type(  # type: ignore[method-assign]
            "R", (), {"text": "ok", "model": "m", "tokens": 1, "seconds": 0}
        )()
    )
    ai.briefing("data", "Ada")
    assert "[L:abcd1234]" in seen["s"] and "[M3]" in seen["s"] and "RESCMP" in seen["s"]
    assert seen["p"].startswith("<mail>") and seen["kw"]["require_complete"] is True


def test_possible_matches_only_returns_candidates():
    from ultra.ai import AI

    ai = AI(Config({}))
    ai._gen = lambda p, s, m=0, **kw: type(
        "R",
        (),
        {  # type: ignore[method-assign]
            "text": '[{"id": "c1", "why": "the storage"}, {"id": "invented", "why": "x"}]'
        },
    )()
    out = ai.possible_matches("text", [{"id": "c1", "name": "Campus Storage", "type": "Asset"}], [])
    assert [o["id"] for o in out] == ["c1"]
