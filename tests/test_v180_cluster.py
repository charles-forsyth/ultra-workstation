"""v1.8: cluster facts in the mail loop (SPEC 8.9).

What must hold:
- job ids are found only where the text says "job" (or slurm-N.out), never a bare number;
- only read tools are ever called, through an allow-list, and results are cached briefly;
- unreachable / signed-out servers give a clear error, never a crash;
- the thread gets a `cluster` field only when there is something to show;
- the page sets cluster text as text (no innerHTML) and a cluster draft only ever becomes
  a new draft version.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from ultra.cluster import (
    READ_TOOLS,
    Cluster,
    ClusterApi,
    ClusterError,
    has_script,
    job_ids,
    with_cluster,
)
from ultra.mcpclient import McpAuthNeeded, McpUnreachable

ROOT = Path(__file__).resolve().parents[1]
M = re.match("x", "x")


@pytest.mark.parametrize(
    ("text", "want"),
    [
        ("My job 315 died", ["315"]),
        ("JobId=4471 JobName=fold", ["4471"]),
        ("see slurm-88123.out please", ["88123"]),
        ("job #12 and Job ID: 13 and job number 14", ["12", "13", "14"]),
        ("job 315 again: job 315", ["315"]),
        ("Room 315, call 555 1234, version 2.3, 2026-09-29", []),
        ("job 3.14 is not a job", []),
        ("jobs: 1 2 3", []),
        ("job 1 job 2 job 3 job 4 job 5 job 6 job 7", ["1", "2", "3", "4", "5"]),
        ("job 000", []),
    ],
)
def test_job_ids(text, want):
    assert job_ids(text) == want


def test_script_detection():
    assert has_script("#!/bin/bash\n#SBATCH -p computehigh\npython x.py")
    assert not has_script("we discussed SBATCH options yesterday")


class Fake:
    def __init__(self, raise_: Exception | None = None):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.raise_ = raise_
        self.state = {"ok": True, "error": ""}

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        self.calls.append((tool, dict(args or {})))
        if self.raise_:
            raise self.raise_
        return {"data": {"tool": tool, "job_id": (args or {}).get("job_id"), "reply_draft": "Hi"}}


def test_job_calls_only_read_tools_and_caches():
    f = Fake()
    c = Cluster(f)  # type: ignore[arg-type]
    out = c.job("315", "my job died")
    assert [t for t, _ in f.calls] == ["job_show_any", "job_explain_any", "ticket_draft"]
    assert all(t in READ_TOOLS for t, _ in f.calls)
    assert out["draft"]["reply_draft"] == "Hi"
    c.job("315", "my job died")
    assert len(f.calls) == 3  # cached for the next look


def test_no_ticket_text_no_draft():
    f = Fake()
    Cluster(f).job("315")  # type: ignore[arg-type]
    assert "ticket_draft" not in [t for t, _ in f.calls]


def test_the_allow_list_refuses_anything_else():
    c = Cluster(Fake())  # type: ignore[arg-type]
    for tool in ("job_submit", "job_cancel", "job_cancel_confirm", "job_hold", "files_read"):
        with pytest.raises(ClusterError):
            c._call(tool, {})


def test_read_tools_hold_no_writes():
    assert not {
        t
        for t in READ_TOOLS
        if any(w in t for w in ("submit", "cancel", "hold", "release", "upload"))
    }


@pytest.mark.parametrize("bad", ["", "31a", "1234567890", "-1", "315; rm"])
def test_job_id_validation(bad):
    with pytest.raises(ClusterError):
        Cluster(Fake()).job(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("exc", "status"), [(McpUnreachable("down"), 503), (McpAuthNeeded("out"), 401)]
)
def test_server_errors_are_clear(exc, status):
    with pytest.raises(ClusterError) as e:
        Cluster(Fake(exc)).job("315")  # type: ignore[arg-type]
    assert e.value.status == status


def test_not_configured():
    c = Cluster(None)
    assert not c.enabled
    with pytest.raises(ClusterError) as e:
        c.job("315")
    assert e.value.status == 503


def test_script_check_limits():
    c = Cluster(Fake())  # type: ignore[arg-type]
    with pytest.raises(ClusterError):
        c.check_script("")
    with pytest.raises(ClusterError):
        c.check_script("#SBATCH -p x\n" * 3000)
    assert c.check_script("#SBATCH -p computehigh\n")["tool"] == "script_check"


def test_with_cluster_only_adds_when_there_is_something():
    t = {"messages": [{"subject": "hi", "body": "nothing here"}]}
    assert "cluster" not in with_cluster(t, True)
    t2 = {"messages": [{"subject": "job 315", "body": "#SBATCH -p x"}]}
    assert with_cluster(t2, True)["cluster"] == {"jobs": ["315"], "script": True}
    assert "cluster" not in with_cluster(t2, False)  # server not configured


def test_api_journals_without_text():
    class Store:
        rows: list = []  # noqa: RUF012

        def journal(self, *a):
            self.rows.append(a)

    s = Store()
    out = ClusterApi(Cluster(Fake()), s).r_job(
        {}, {"job_id": "315", "ticket_text": "secret words"}, M
    )  # type: ignore[arg-type]
    assert out["job_id"] == "315"
    assert s.rows and "secret" not in repr(s.rows)


def test_page_sets_cluster_text_as_text():
    app = (ROOT / "src/ultra/static/app.js").read_text()
    i = app.index("function wireCluster(")
    body = app[i : app.index("\n}\n", i)]
    assert "innerHTML" not in body
    assert "applyStudioDraft(mailKey, d.reply_draft" in body  # a new AI version, both approvals
    assert "/api/cluster/job" in body and "/api/cluster/script" in body


def test_demo_thread_shows_the_chip():
    from ultra.config import Config
    from ultra.server import Api

    api = Api(Config({}), "tok", demo=True)
    st, t = api.dispatch("GET", "/api/thread/g-800", {}, None)
    assert st == 200 and t["cluster"]["jobs"] == ["315"] and t["cluster"]["script"]
    st, r = api.dispatch("POST", "/api/cluster/job", {}, {"job_id": "315", "ticket_text": "help"})
    assert st == 200 and r["show"]["state"] == "OUT_OF_MEMORY" and r["draft"]["reply_draft"]


def test_health_line_is_cached_and_never_reads_healthy_on_failure():
    class H(Fake):
        def call(self, tool, args=None):
            self.calls.append((tool, dict(args or {})))
            if self.raise_:
                raise self.raise_
            return {
                "data": {
                    "ok": True,
                    "issues": [{"message": "node x drained"}],
                    "failure_rate_24h_percent": 2,
                }
            }

    h = H()
    c = Cluster(h)  # type: ignore[arg-type]
    line = c.health()
    assert line["ok"] is False and line["issues"] == ["node x drained"]
    c.health()
    assert len(h.calls) == 1  # five-minute cache
    bad = Cluster(H(McpUnreachable("down")))  # type: ignore[arg-type]
    assert bad.health()["ok"] is None  # unknown, not healthy


def test_today_shows_the_cluster_only_when_it_matters():
    today = (ROOT / "src/ultra/static/today.js").read_text()
    assert 'api("/api/cluster/health")' in today
    assert "h.ok === false" in today and "h.ok === null" in today
