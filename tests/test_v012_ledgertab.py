"""v0.12 Ledger tab: reads, the review -> (confirm x2) -> commit write flow, read-back.

Runs against DemoLedgerCLI (in-memory, invented records). No real ledger is touched.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import pytest

from ultra.demo import DemoAI, DemoLedgerCLI
from ultra.ledger_write import WriteError
from ultra.ledgertab import LedgerTab, LedgerTabWriter
from ultra.server import ApiError
from ultra.store import Store

P = "aaaaaaaa-0000-4000-8000-0000000000"
BEN, DEE, ADA = P + "01", P + "03", P + "04"
LAB, IX1, T_OVERDUE, T_BLOCKED = P + "11", P + "71", P + "61", P + "63"


@pytest.fixture
def lt(tmp_path):
    led = DemoLedgerCLI()
    store = Store(tmp_path / "s.db")
    calls: list[tuple[list[str], str]] = []

    def run(argv: list[str], stdin: str) -> tuple[int, str]:
        calls.append((list(argv), stdin))
        return led.write(argv, stdin)

    w = LedgerTabWriter(led, store, run=run)  # type: ignore[arg-type]
    tab = LedgerTab(led, w, store, DemoAI(), "Ada", now=lambda: dt.datetime(2026, 9, 30, 10, 0))  # type: ignore[arg-type]
    return tab, led, calls, store


M: Any = re.match("", "")


def get(tab: LedgerTab, fn: str, **q: str) -> Any:
    return getattr(tab, fn)({k: [v] for k, v in q.items()}, None, M)


def commit(tab: LedgerTab, body: dict[str, Any], destructive: bool = False) -> dict[str, Any]:
    rv = tab.r_write_review({}, body, M)
    extra: dict[str, Any] = {}
    if destructive:
        c2 = tab.r_write_confirm({}, {"token": rv["token"], "confirm_1": rv["confirm_1"]}, M)
        extra["confirm_2"] = c2["confirm_2"]
    return tab.r_write_commit({}, {"token": rv["token"], **extra}, M)


# ------------------------------------------------------------------ reads
def test_home_uses_the_clock_for_overdue(lt) -> None:
    tab, *_ = lt
    h = get(tab, "r_home")
    assert h["now"] == "2026-09-30 10:00"
    assert [t["id"] for t in h["overdue"]] == [T_OVERDUE]  # due 09-28, today 09-30
    assert all(t["id"] != T_OVERDUE for t in h["upcoming"])
    assert [t["id"] for t in h["blocked"]] == [T_BLOCKED]
    assert h["stats"]["counts"]["researchers"] == 4


def test_lists_search_and_filter(lt) -> None:
    tab, *_ = lt
    assert get(tab, "r_search", q="ben")["results"][0]["id"] == BEN
    assert get(tab, "r_search", q="-x")["results"] == []
    labs = tab.r_list({"q": ["lovelace"]}, None, re.match(r"(labs)", "labs"))
    assert [r["name"] for r in labs["rows"]] == ["Lovelace Lab"]


def test_entity_page_groups_links_and_open_tasks(lt) -> None:
    tab, *_ = lt
    pg = get(tab, "r_entity", kind="people", key="bcarter")
    assert pg["record"]["id"] == BEN and "bio_vector" not in pg["record"]
    assert {x["name"] for x in pg["groups"]["Lab"]} == {"Research Computing"}
    assert [t["id"] for t in pg["tasks_open"]] == [T_OVERDUE]
    assert pg["interaction_count"] == 2
    assert {a["action"] for a in pg["write_actions"]} >= {"people_update", "log", "people_delete"}
    with pytest.raises(ApiError):
        get(tab, "r_entity", kind="people", key="nobody")
    with pytest.raises(ApiError):
        get(tab, "r_entity", kind="people", key="--json")  # not a netid
    with pytest.raises(ApiError):
        get(tab, "r_entity", kind="secrets", key="x")


def test_brief_cites_only_given_ids(lt) -> None:
    tab, *_ = lt
    b = tab.r_brief({}, {"kind": "people", "key": "bcarter"}, M)
    assert "## Who or what this is" in b["markdown"]
    assert b["uncited_ids"] == []
    for pre in re.findall(r"\[(?:L|T|E):([0-9a-f]{8})\]", b["markdown"]):
        assert pre in b["cites"]
    assert tab.r_brief({}, {"kind": "people", "key": "bcarter"}, M).get("cached")


def test_tasks_flag_overdue_from_the_clock(lt) -> None:
    tab, *_ = lt
    rows = {t["id"]: t for t in get(tab, "r_tasks")["tasks"]}
    assert rows[T_OVERDUE]["overdue"] and not rows[P + "62"]["overdue"]


def test_interactions_filters_are_checked(lt) -> None:
    tab, *_ = lt
    assert len(get(tab, "r_interactions")["interactions"]) == 3
    with pytest.raises(ApiError):
        get(tab, "r_interactions", since="yesterday")
    with pytest.raises(ApiError):
        get(tab, "r_interactions", contains="--fix")


def test_reads_refuse_dangerous_flags(lt) -> None:
    from ultra.ledger import LedgerError

    _, led, *_ = lt
    for args in (
        ["doctor", "--fix"],
        ["doctor", "--fix-safe"],
        ["ship", "status", "--draft"],
        ["gcp", "audit-report", "-o", "/tmp/x"],
        ["people", "delete", "x"],
        ["db-reset"],
    ):
        with pytest.raises(LedgerError):
            led._run(args)


def test_reports_run_in_the_background_and_cache(lt) -> None:
    import time

    tab, *_ = lt
    m = re.match(r"(doctor)", "doctor")
    first = tab.r_report({}, None, m)
    assert first["state"] in ("running", "done")
    for _ in range(50):
        r = tab.r_report({}, None, m)
        if r["state"] == "done":
            break
        time.sleep(0.02)
    assert r["state"] == "done" and r["data"]["edges_total"] > 0
    assert tab.r_report({}, None, m).get("cached")


# ------------------------------------------------------------------ write flow
def test_add_task_review_then_commit_with_read_back(lt) -> None:
    tab, _led, calls, store = lt
    rv = tab.r_write_review(
        {},
        {
            "action": "task_add",
            "summary": "Call Dee [re budget]",
            "priority": "HIGH",
            "due": "2026-10-03",
        },
        M,
    )
    assert "confirm_1" not in rv and calls == []  # nothing ran at review
    assert "Task: Call Dee (re budget)" in rv["card"]["lines"]  # brackets neutralised
    out = tab.r_write_commit({}, {"token": rv["token"]}, M)
    assert out["ok"] and out["result"]["read_back"].endswith("exists")
    argv = calls[-1][0]
    assert argv[:2] == ["tasks", "add"] and argv[argv.index("--") + 1] == "Call Dee (re budget)"
    assert store.journal_recent(5, "ledger_tab_")[0]["action"] == "ledger_tab_task_add"


def test_token_is_single_use_and_bound(lt) -> None:
    tab, _, calls, _ = lt
    rv = tab.r_write_review({}, {"action": "task_update", "id": T_OVERDUE, "status": "DONE"}, M)
    tab.r_write_commit({}, {"token": rv["token"]}, M)
    with pytest.raises(ApiError):
        tab.r_write_commit({}, {"token": rv["token"]}, M)
    with pytest.raises(ApiError):
        tab.r_write_commit({}, {"token": "made-up"}, M)
    # the browser cannot change the command at commit: extra fields are ignored
    rv = tab.r_write_review({}, {"action": "task_update", "id": T_OVERDUE, "priority": "LOW"}, M)
    tab.r_write_commit(
        {},
        {"token": rv["token"], "action": "people_delete", "key": "bcarter", "argv": ["db-reset"]},
        M,
    )
    assert calls[-1][0] == ["tasks", "update", T_OVERDUE, "--priority", "LOW"]


def test_tampered_pending_argv_is_refused(lt) -> None:
    tab, _, calls, _ = lt
    rv = tab.r_write_review({}, {"action": "task_update", "id": T_OVERDUE, "status": "DONE"}, M)
    tab.writer.pending[rv["token"]]["argv"] = ["db-reset", "--force"]
    with pytest.raises(ApiError):
        tab.r_write_commit({}, {"token": rv["token"]}, M)
    assert calls == []


def test_expired_review_is_refused(lt) -> None:
    tab, _, calls, _ = lt
    rv = tab.r_write_review({}, {"action": "task_update", "id": T_OVERDUE, "status": "DONE"}, M)
    tab.writer.pending[rv["token"]]["exp"] = 0
    with pytest.raises(ApiError):
        tab.r_write_commit({}, {"token": rv["token"]}, M)
    assert calls == []


def test_destructive_needs_both_confirmations(lt) -> None:
    tab, led, calls, _ = lt
    body = {"action": "people_delete", "key": "devans"}
    rv = tab.r_write_review({}, body, M)
    assert rv["card"]["destructive"] and rv["confirm_1"]
    with pytest.raises(ApiError):  # no confirm_2
        tab.r_write_commit({}, {"token": rv["token"]}, M)
    rv = tab.r_write_review({}, body, M)
    with pytest.raises(ApiError):  # confirm_1 is not confirm_2
        tab.r_write_commit({}, {"token": rv["token"], "confirm_2": rv["confirm_1"]}, M)
    rv = tab.r_write_review({}, body, M)
    with pytest.raises(ApiError):  # wrong confirm_1
        tab.r_write_confirm({}, {"token": rv["token"], "confirm_1": "x" * 64}, M)
    rv = tab.r_write_review({}, body, M)
    c2 = tab.r_write_confirm({}, {"token": rv["token"], "confirm_1": rv["confirm_1"]}, M)
    with pytest.raises(ApiError):  # confirm_1 is single use
        tab.r_write_confirm({}, {"token": rv["token"], "confirm_1": rv["confirm_1"]}, M)
    assert calls == []
    out = tab.r_write_commit({}, {"token": rv["token"], "confirm_2": c2["confirm_2"]}, M)
    assert out["ok"] and out["result"]["read_back"] == "record is gone"
    assert "devans" not in led.people
    assert calls[-1][0] == ["people", "delete", "devans", "--force"]


def test_non_destructive_cannot_use_confirm(lt) -> None:
    tab, *_ = lt
    rv = tab.r_write_review({}, {"action": "task_update", "id": T_OVERDUE, "status": "DONE"}, M)
    with pytest.raises(ApiError):
        tab.r_write_confirm({}, {"token": rv["token"], "confirm_1": "x"}, M)


@pytest.mark.parametrize(
    "body",
    [
        {"action": "db_reset"},
        {"action": "doctor_fix"},
        {"action": "task_update", "id": "not-a-uuid", "status": "DONE"},
        {"action": "task_update", "id": T_OVERDUE, "status": "WONTFIX"},
        {"action": "task_update", "id": T_OVERDUE},
        {"action": "task_add", "summary": "", "priority": "HIGH"},
        {"action": "task_add", "summary": "x", "priority": "URGENT"},
        {"action": "task_add", "summary": "x", "priority": "LOW", "due": "next tuesday"},
        {"action": "log", "text": "x", "date": "today"},
        {"action": "log", "text": "", "date": "2026-09-30"},
        {"action": "log", "text": "x", "date": "2026-09-30", "links": ["bcarter"]},
        {"action": "link", "source": BEN, "target": BEN, "type": "LINKS_TO"},
        {"action": "link", "source": BEN, "target": LAB, "type": "OWNS_EVERYTHING"},
        {"action": "people_add", "netid": "gwu", "name": "Grace Wu"},
        {"action": "people_add", "netid": "--fix", "name": "X Y"},
        {"action": "people_update", "key": "bcarter", "set": {"netid": "evil"}},
        {"action": "people_update", "key": "bcarter", "set": {"details": "x"}},
        {"action": "people_update", "key": "bcarter", "set": {}},
        {"action": "people_tag", "key": "bcarter", "tag": "bad tag!"},
        {"action": "people_delete", "key": "nobody"},
        {"action": "labs_delete", "key": "--force"},
        {"action": "interaction_edit", "id": IX1, "summary": "new"},  # no note
        {
            "action": "projects_doc_add",
            "key": "Campus Storage",
            "url": "javascript:alert(1)",
            "title": "x",
        },
        {"action": "projects_doc_rm", "key": "Campus Storage", "url": "https://nope.example.org"},
        {"action": "projects_add", "name": "New Thing"},  # no summary
        {"action": "gcp_add", "project_id": "Bad_ID"},
        {"action": "grants_add", "c_number": "G-1", "title": "T", "amount": "1.5"},
        {"action": "assets_add", "name": "x", "type": "SPACESHIP"},
    ],
)
def test_bad_requests_are_refused_before_anything_runs(lt, body) -> None:
    tab, _, calls, _ = lt
    with pytest.raises(ApiError):
        tab.r_write_review({}, body, M)
    assert calls == []


def test_log_goes_on_stdin_with_links_and_reads_back(lt) -> None:
    tab, _led, calls, _ = lt
    out = commit(
        tab,
        {
            "action": "log",
            "text": "Met Ben; $5 & 'quotes' stay text.",
            "date": "2026-09-30 09:00",
            "links": [BEN, LAB],
            "link_names": ["Ben Carter", "Lovelace Lab"],
        },
    )
    assert out["ok"]
    argv, stdin = calls[-1]
    assert argv[-1] == "-" and "--strict-links" in argv and "--yes" in argv
    assert stdin == "Met Ben; $5 & 'quotes' stay text.\n"
    assert argv.count("--link") == 2 and "with 2 links" in out["result"]["read_back"]


def test_task_update_reads_back_each_field(lt) -> None:
    tab, led, *_ = lt
    out = commit(
        tab,
        {
            "action": "task_update",
            "id": T_OVERDUE,
            "status": "IN_PROGRESS",
            "priority": "CRITICAL",
            "due": "2026-10-02",
        },
    )
    assert out["ok"] and "did not change" not in out["result"]["read_back"]
    out = commit(tab, {"action": "task_update", "id": T_OVERDUE, "clear_due": True})
    assert out["ok"] and led.tasks[T_OVERDUE]["due_date"] is None


def test_read_back_catches_a_write_that_did_not_stick(lt) -> None:
    tab, _led, _calls, _ = lt
    tab.writer._run_fn = lambda argv, stdin: (0, "Updated Task")  # says OK, changes nothing
    out = commit(tab, {"action": "task_update", "id": T_OVERDUE, "status": "DONE"})
    assert not out["ok"] and "did not change: status" in out["result"]["read_back"]


def test_link_unlink_people_update_tag_and_docs(lt) -> None:
    tab, led, *_ = lt
    assert commit(
        tab, {"action": "link", "source": ADA, "target": LAB, "type": "MEMBER_OF", "role": "PI"}
    )["ok"]
    assert commit(tab, {"action": "unlink", "source": ADA, "target": LAB}, destructive=True)["ok"]
    out = commit(
        tab,
        {"action": "people_update", "key": "adal", "set": {"title": "Chair", "office": "Room 4"}},
    )
    assert out["ok"] and led.people["adal"]["title"] == "Chair"
    assert commit(tab, {"action": "people_tag", "key": "adal", "tag": "#VIP"})["ok"]
    assert "VIP" in led.people["adal"]["details"]["tags"]
    url = "https://docs.example.org/budget"
    assert commit(
        tab, {"action": "projects_doc_add", "key": "Campus Storage", "url": url, "title": "Budget"}
    )["ok"]
    assert commit(
        tab, {"action": "projects_doc_rm", "key": "Campus Storage", "url": url}, destructive=True
    )["ok"]


def test_interaction_edit_keeps_history(lt) -> None:
    tab, led, calls, _ = lt
    out = commit(
        tab,
        {
            "action": "interaction_edit",
            "id": IX1,
            "summary": "Kickoff for ada-lab; Ben is admin.",
            "note": "typo",
        },
    )
    assert out["ok"] and led.ix[IX1]["details"]["superseded"].startswith("Kickoff for the")
    assert "--note" in calls[-1][0]


def test_adds_for_every_kind(lt) -> None:
    tab, _led, *_ = lt
    assert commit(tab, {"action": "labs_add", "name": "Wu Lab"})["ok"]
    assert commit(
        tab, {"action": "projects_add", "name": "Cold Storage", "summary": "Archive tier"}
    )["ok"]
    assert commit(tab, {"action": "gcp_add", "project_id": "wu-lab-01", "name": "Wu"})["ok"]
    assert commit(
        tab, {"action": "grants_add", "c_number": "NIH-9", "title": "Imaging", "amount": "50000"}
    )["ok"]
    assert commit(tab, {"action": "assets_add", "name": "gpu-01", "type": "GPU"})["ok"]


def test_every_write_action_is_wired(lt) -> None:
    tab, *_ = lt
    for a in tab.writer.WRITES:
        with pytest.raises((WriteError, ApiError)) as e:
            tab.writer.build({"action": a})  # empty body: refused, but never "not wired"
        assert "not wired" not in str(e.value)


def test_write_actions_never_include_bulk_or_reset() -> None:
    names = " ".join(LedgerTabWriter.WRITES)
    for bad in ("reset", "init", "doctor", "sync", "scan", "triage", "email", "convert"):
        assert bad not in names


def test_pending_reviews_are_capped(lt) -> None:
    tab, *_ = lt
    for _ in range(51):
        tab.r_write_review({}, {"action": "task_update", "id": T_OVERDUE, "status": "DONE"}, M)
    with pytest.raises(ApiError):
        tab.r_write_review({}, {"action": "task_update", "id": T_OVERDUE, "status": "DONE"}, M)


def test_commit_clears_caches(lt) -> None:
    tab, _, _, store = lt
    get(tab, "r_home")
    assert store.cache_get("lt:stats") is not None
    commit(tab, {"action": "task_update", "id": T_OVERDUE, "status": "DONE"})
    assert store.cache_get("lt:stats") is None


def test_log_and_edit_text_brackets_cannot_break_the_ledgers_print(lt) -> None:
    """`[/x]` in printed text makes the ledger's Rich print raise and roll back."""
    tab, _, calls, _ = lt
    commit(tab, {"action": "log", "text": "Ben said [/bold] ok", "date": "2026-09-30"})
    assert calls[-1][1] == "Ben said (/bold) ok\n"
    commit(tab, {"action": "interaction_edit", "id": IX1, "summary": "fixed [/x]", "note": "n"})
    argv = calls[-1][0]
    assert argv[argv.index("--summary") + 1] == "fixed (/x)"


def test_delete_read_back_reports_a_record_that_survived(lt) -> None:
    tab, *_ = lt
    tab.writer._run_fn = lambda argv, stdin: (0, "Deleted")  # says deleted, is not
    out = commit(tab, {"action": "grants_delete", "key": "NSF-2400001"}, destructive=True)
    assert not out["ok"] and out["result"]["read_back"] == "record still exists"


def test_the_tab_never_adds_people(lt) -> None:
    """Operator rule: the Add to ledger button is the only way a person is added."""
    tab, led, *_ = lt
    assert "people_add" not in tab.writer.WRITES
    with pytest.raises(WriteError):
        tab.writer.review({"action": "people_add", "netid": "gwu", "name": "Grace Wu"})
    assert "gwu" not in led.people


def test_task_update_drops_fields_that_would_not_change(lt) -> None:
    """Found live: a priority set to what it already was showed "HIGH -> HIGH"."""
    tab, led, *_ = lt
    cur = led.tasks[T_OVERDUE]
    with pytest.raises(WriteError, match="nothing to change"):
        tab.writer.review({"action": "task_update", "id": T_OVERDUE, "priority": cur["priority"]})
    with pytest.raises(WriteError, match="nothing to change"):
        tab.writer.review({"action": "task_update", "id": T_BLOCKED, "clear_due": True})
    rv = tab.writer.review(
        {"action": "task_update", "id": T_OVERDUE, "priority": cur["priority"], "status": "DONE"}
    )
    assert "--priority" not in rv["card"]["command"]
    assert "--status DONE" in rv["card"]["command"]
    assert len(rv["card"]["lines"]) == 2


def test_long_task_summary_is_one_short_line_on_the_card(lt) -> None:
    tab, led, *_ = lt
    led.tasks[T_OVERDUE]["summary"] = "word " * 200
    rv = tab.writer.review({"action": "task_update", "id": T_OVERDUE, "status": "DONE"})
    assert len(rv["card"]["lines"][0]) <= 170 and rv["card"]["lines"][0].endswith("...")


def test_brief_context_is_bounded_for_a_heavily_linked_record(lt) -> None:
    """Found live: 146 links and 2,722 logs made the briefing hit the output limit."""
    tab, *_ = lt
    page = {
        "kind": "people",
        "record": {"id": BEN, "name": "Ben", "links": ["x"] * 500},
        "groups": {"Asset": [{"id": f"a{i:07x}-0000", "name": f"a{i}"} for i in range(97)]},
        "tasks_open": [{"id": f"b{i:07x}-1111", "name": "t " * 400} for i in range(40)],
        "tasks_done": 3,
        "interactions": [],
        "interaction_count": 2722,
    }
    text, cites = tab._brief_context(page)
    assert len(cites) == 56  # record + 25 links + 30 tasks
    assert cites[BEN[:8]] == BEN
    assert text.count("[E:") == 26 and text.count("[T:") == 30  # record + 25 links
    assert "Linked Asset: 97 in total, first 25 listed" in text
    assert "Interactions: 2722 in total" in text
    assert '"links"' not in text and len(text) <= 60_000
