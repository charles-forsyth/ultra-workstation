"""Demo mode: synthetic data only, for screenshots, docs and UI tests.

Every name, address and id here is invented (example.org). Real mail is never read
in demo mode: the adapters are not constructed at all.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ultra.server import Api

STREAM: list[dict[str, Any]] = [
    {
        "key": "g-100",
        "source": "email",
        "from": "Ben Carter",
        "addr": "ben@example.org",
        "subject": "Lab project handover",
        "snippet": "The project is ready. Please work with Ada to hand it over.",
        "ts": "2026-09-29T09:14:00-04:00",
        "court": "MINE",
        "badges": ["VIP", "READY"],
        "waiting_days": 1,
    },
    {
        "key": "s-200",
        "source": "slack",
        "from": "Cy Dunn",
        "addr": "",
        "subject": "Group DM",
        "snippet": "Budget updated, lock process is active.",
        "ts": "2026-09-29T08:02:00-04:00",
        "court": "MINE",
        "badges": [],
        "waiting_days": 0,
    },
    {
        "key": "k-300",
        "source": "ticket",
        "from": "Service desk",
        "addr": "desk@example.org",
        "subject": "RITM0000001 comments added",
        "snippet": "Requester asked for an update on storage access.",
        "ts": "2026-09-28T16:40:00-04:00",
        "court": "MINE",
        "badges": [],
        "waiting_days": 1,
    },
    {
        "key": "g-400",
        "source": "email",
        "from": "Dee Evans",
        "addr": "dee@example.org",
        "subject": "Security review items",
        "snippet": "Thanks, I will get back to you on the remaining items.",
        "ts": "2026-09-25T11:00:00-04:00",
        "court": "WAITING",
        "badges": [],
        "waiting_days": 4,
    },
]

THREADS: dict[str, list[dict[str, str]]] = {
    "g-100": [
        {
            "from": "Ben Carter <ben@example.org>",
            "ts": "2026-09-28 17:54",
            "body": "The project is ready. Please work with Ada to hand it over.",
        },
        {
            "from": "Ada Lovelace <ada@example.org>",
            "ts": "2026-09-29 09:14",
            "body": "Great, when will the account be ready for my students?",
        },
    ]
}

CONTEXT = {
    "ben@example.org": {
        "name": "Ben Carter",
        "netid": "bcarter",
        "title": "Deputy Director",
        "labs": ["Lovelace Lab"],
        "projects": ["ada-lab"],
        "open_tasks": [
            {
                "id": "t-1",
                "summary": "Hand over ada-lab to Ada: turn on APIs, add Ada.",
            },
            {
                "id": "t-2",
                "summary": "Confirm the budget alert covers the new account.",
            },
        ],
        "interactions": [{"id": "i-1", "summary": "Ben asked for the ada-lab handover plan."}],
        "interaction_count": 1,
        "matched_by": "address",
    }
}


def register(api: Api) -> None:
    def stream(q: dict, body: Any, m: re.Match[str]) -> dict:
        from ultra.live import FILTERS

        f = (q.get("filter") or ["mine"])[0]
        keep = FILTERS.get(f, FILTERS["mine"])
        counts = {k: sum(1 for s in STREAM if fn(s)) for k, fn in FILTERS.items()}
        return {"items": [s for s in STREAM if keep(s)], "counts": counts, "demo": True}

    def thread(q: dict, body: Any, m: re.Match[str]) -> dict:
        key = m.group(1)
        return {"key": key, "messages": THREADS.get(key, [])}

    def context(q: dict, body: Any, m: re.Match[str]) -> dict:
        return CONTEXT.get(m.group(1), {"unresolved": True})

    api.add("GET", r"/api/stream", stream)
    api.add("GET", r"/api/thread/([A-Za-z0-9_-]+)", thread)
    api.add("GET", r"/api/context/([^/]+)", context)
    _register_composer(api)
    _register_desk(api)
    _register_tools(api)


class DemoResearch:
    """Canned research client: nothing runs and nothing is spent."""

    enabled = True
    dashboard_url = ""
    depth, breadth = 1, 3

    def __init__(self) -> None:
        self.started: list[dict[str, Any]] = []
        self.state: dict[str, Any] = {"ok": True, "error": ""}

    def search(self, q: str, limit: int = 5) -> dict[str, Any]:
        return {
            "query": q,
            "answer": f"Demo: two earlier reports discuss {q[:40]}.",
            "matches": [{"session_id": 7, "score": 0.81, "prompt": "Lab storage options"}],
            "model": "demo",
        }

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        return [
            {
                "id": 7,
                "prompt": "Lab storage options",
                "status": "completed",
                "created_at": "2026-09-28T10:00:00",
            }
        ]

    def show(self, sid: int) -> dict[str, Any]:
        return {
            "id": sid,
            "prompt": "Lab storage options",
            "status": "completed",
            "result": "## Summary\n\nDemo report text about storage tiers.",
        }

    def estimate(self, prompt: str, depth: int, breadth: int, upload: Any) -> dict[str, Any]:
        return {
            "prompt": prompt,
            "depth": depth,
            "breadth": breadth,
            "cost_usd": 0.95,
            "note": "Demo estimate.",
        }

    def start(self, prompt: str, depth: int, breadth: int, thread: str | None) -> dict:
        self.started.append({"prompt": prompt, "thread": bool(thread)})
        return {"session_id": 100 + len(self.started), "status": "running", "demo": True}

    def _thread_file(self, text: str) -> Any:
        import tempfile
        from pathlib import Path

        fd, name = tempfile.mkstemp()
        import os

        os.close(fd)
        return Path(name)


class DemoAI:
    enabled = True
    model = "demo"

    def web_search(self, q: str, context: str = "") -> dict[str, Any]:
        return {
            "text": f"Demo web answer about {q[:60]}.",
            "grounded": True,
            "sources": [{"title": "example.org", "url": "https://example.org/"}],
            "queries": [q],
            "model": "demo",
            "seconds": 0,
        }

    def explain(self, passage: str, thread: str, operator: str) -> Any:
        from ultra.ai import Result

        return Result(f"Demo: '{passage[:40]}' is explained here.", "demo", 0, 0.0)


def _register_tools(api: Api) -> None:
    import tempfile
    from pathlib import Path

    from ultra.audio import Audio
    from ultra.config import Config
    from ultra.store import Store
    from ultra.tools import Tools

    store = Store(Path(tempfile.mkdtemp(prefix="ultra-demo-tools-")) / "t.db")
    research = DemoResearch()
    api.demo_research = research  # type: ignore[attr-defined]
    audio = Audio(Config({}), store, type("Off", (), {"enabled": False})())

    def text(key: str, for_speech: bool = False) -> str:
        return "\n\n".join(f"{m['from']}: {m['body']}" for m in THREADS.get(key, []))

    Tools(research, DemoAI(), audio, text, "Ada").register(api)


# ---------------------------------------------------------------- demo ledger
DEMO_IDS = {
    "ben@example.org": "11111111-1111-4111-8111-111111111111",
    "cy@example.org": "22222222-2222-4222-8222-222222222222",
    "lab": "33333333-3333-4333-8333-333333333333",
    "project": "44444444-4444-4444-8444-444444444444",
}


class DemoLedger:
    """Invented people only. Same method names as ultra.ledger.Ledger."""

    enabled = True

    def __init__(self) -> None:
        self.records: dict[str, dict[str, Any]] = {}

    def resolve(self, addr: str, name: str = "") -> dict[str, Any] | None:
        if addr == "ben@example.org":
            return {
                "id": DEMO_IDS[addr],
                "name": "Ben Carter (bcarter)",
                "netid": "bcarter",
                "title": "Deputy Director",
                "matched_by": "address",
            }
        if addr == "cy@example.org":
            return {
                "id": DEMO_IDS[addr],
                "name": "Cy Dunn (cdunn)",
                "netid": "cdunn",
                "title": "Analyst",
                "matched_by": "address",
            }
        return None

    def context(self, addr: str, name: str = "") -> dict[str, Any]:
        p = self.resolve(addr, name)
        if not p:
            return {"unresolved": True, "addr": addr}
        base = CONTEXT.get(addr, {})
        return {
            **base,
            **p,
            "labs": ["Lovelace Lab"],
            "projects": ["ada-lab"],
            "lab_refs": [{"id": DEMO_IDS["lab"], "name": "Lovelace Lab", "edge": "MEMBER_OF"}],
            "project_refs": [{"id": DEMO_IDS["project"], "name": "ada-lab", "edge": "OPERATES"}],
            "open_tasks": base.get("open_tasks", []),
            "interactions": base.get("interactions", []),
            "interaction_count": base.get("interaction_count", 0),
        }

    def search(self, term: str) -> list[dict[str, Any]]:
        t = term.lower()
        out = []
        if "ben" in t or "carter" in t:
            out.append(
                {
                    "id": DEMO_IDS["ben@example.org"],
                    "name": "Ben Carter (bcarter)",
                    "type": "Researcher",
                    "score": 0.9,
                }
            )
        if "lovelace" in t or "lab" in t:
            out.append({"id": DEMO_IDS["lab"], "name": "Lovelace Lab", "type": "Lab", "score": 0.9})
        if "ada-lab" in t or "project" in t:
            out.append(
                {"id": DEMO_IDS["project"], "name": "ada-lab", "type": "GCPProject", "score": 0.9}
            )
        return out

    def interaction(self, iid: str) -> dict[str, Any] | None:
        return self.records.get(iid)

    def tree_fresh(self, ident: str) -> dict[str, Any]:
        r = self.records.get(ident) or {}
        return {"connections": [{"id": x["id"]} for x in r.get("links", [])]}


class DemoWriter:
    """Records ledger writes in memory. Nothing leaves the process."""

    def __init__(self, ledger: DemoLedger) -> None:
        self.ledger = ledger
        self.calls: list[tuple[str, Any]] = []
        self.n = 0

    def _id(self) -> str:
        self.n += 1
        return f"{self.n:08x}-demo-4000-8000-{self.n:012x}"

    def log(self, text: str, date: str, links: list[str]) -> dict[str, Any]:
        self.calls.append(("log", {"text": text, "date": date, "links": links}))
        iid = self._id()
        self.ledger.records[iid] = {
            "id": iid,
            "summary": text[:120],
            "links": [{"id": x} for x in links],
        }
        return {"id": iid, "explicit": links, "unresolved": [], "ai_error": False, "rc": 0}

    def task_add(self, summary: str, priority: str) -> dict[str, Any]:
        self.calls.append(("task_add", {"summary": summary, "priority": priority}))
        tid = self._id()
        self.ledger.records[tid] = {"id": tid, "summary": summary, "links": []}
        return {"id": tid, "rc": 0, "summary": summary}

    def link(self, source: str, target: str, kind: str) -> dict[str, Any]:
        self.calls.append(("link", {"source": source, "target": target, "type": kind}))
        for a, b in ((source, target), (target, source)):
            if a in self.ledger.records:
                self.ledger.records[a]["links"].append({"id": b})
        return {"ok": True, "rc": 0}

    def unlink(self, a: str, b: str) -> dict[str, Any]:
        self.calls.append(("unlink", {"a": a, "b": b}))
        return {"ok": True, "rc": 0}

    def task_status(self, task_id: str, status: str) -> dict[str, Any]:
        self.calls.append(("task_status", {"id": task_id, "status": status}))
        return {"ok": True, "rc": 0}


def _register_desk(api: Api) -> None:
    import tempfile
    from pathlib import Path

    from ultra.desk import Desk
    from ultra.rules import Rules
    from ultra.store import Store

    store = Store(Path(tempfile.mkdtemp(prefix="ultra-demo-desk-")) / "desk.db")
    ledger = DemoLedger()
    writer = DemoWriter(ledger)
    api.demo_writer = writer  # type: ignore[attr-defined]

    def thread_fn(key: str) -> dict[str, Any]:
        row = next((s for s in STREAM if s["key"] == key), {})
        msgs = [
            {
                **m,
                "subject": row.get("subject", ""),
                "mine": "ada@example.org" in m["from"],
                "to": m.get("to", "ada@example.org, Cy Dunn <cy@example.org>"),
                "ts": m["ts"].replace(" ", "T") + ":00-04:00" if len(m["ts"]) == 16 else m["ts"],
            }
            for m in THREADS.get(key, [])
        ]
        if not msgs and row:
            msgs = [
                {
                    "from": f"{row['from']} <{row.get('addr') or 'desk@example.org'}>",
                    "to": "ada@example.org",
                    "subject": row["subject"],
                    "ts": row["ts"],
                    "body": row["snippet"],
                    "mine": False,
                }
            ]
        return {"key": key, "messages": msgs}

    Desk(
        store,
        Rules(me={"ada@example.org"}),
        ledger,
        writer,
        thread_fn,
        "America/New_York",
        me_netid="adal",
    ).register(api)


class DemoOutbox:
    """Stands in for Gmail send in demo mode and tests: records, never sends."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    def __call__(self, d: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
        self.sent.append({"draft": d["id"], **{k: v[k] for k in ("to_addrs", "subject", "body")}})
        return {"id": f"demo-{len(self.sent)}", "threadId": d.get("thread_id")}


def _register_composer(api: Api) -> None:
    """Demo composer: real state machine on a temp store, fake outbox, demo threads."""
    import tempfile
    from pathlib import Path

    from ultra.compose import ComposeError, Composer
    from ultra.config import Config
    from ultra.lint import ascii_fix
    from ultra.server import ApiError
    from ultra.store import Store

    tmp = Path(tempfile.mkdtemp(prefix="ultra-demo-"))
    store = Store(tmp / "demo.db")
    cfg = Config({"mail": {"send_delay_seconds": 3}})
    me = {"ada@example.org"}
    comp = Composer(cfg, store, me)
    outbox = DemoOutbox()
    api.demo_outbox = outbox  # type: ignore[attr-defined]

    def wrap(fn: Any, *a: Any) -> Any:
        try:
            return fn(*a)
        except ComposeError as e:
            raise ApiError(e.status, str(e)) from e

    def demo_thread(key: str) -> dict[str, Any]:
        msgs = THREADS.get(key, [])
        return {
            "key": key,
            "messages": [
                {
                    **m,
                    "id": f"{key}-{i}",
                    "message_id": f"<{key}-{i}@example.org>",
                    "references": "",
                    "subject": next((s["subject"] for s in STREAM if s["key"] == key), ""),
                    "to": m.get("to", "ada@example.org"),
                }
                for i, m in enumerate(msgs)
            ],
        }

    def new(q: dict, body: Any, m: re.Match[str]) -> dict:
        body = body or {}
        kind = body.get("kind", "reply")
        th = None if kind == "new" else demo_thread(str(body.get("thread", "")))
        if th is not None:
            th["key"] = "g-" + th["key"].removeprefix("g-")
        return wrap(comp.create, kind, th, "ada@example.org")

    did = lambda m: int(m.group(1))  # noqa: E731
    api.add("POST", r"/api/drafts", new)
    api.add(
        "GET", r"/api/drafts/(\d+)", lambda q, b, m: {**wrap(comp.get, did(m)), "send_error": ""}
    )
    api.add(
        "GET",
        r"/api/drafts/thread/([A-Za-z0-9_-]+)",
        lambda q, b, m: {"drafts": comp.for_thread(m.group(1))},
    )
    api.add(
        "POST",
        r"/api/drafts/(\d+)/versions",
        lambda q, b, m: wrap(comp.save, did(m), b or {}, "me", "edit"),
    )
    api.add(
        "POST",
        r"/api/drafts/(\d+)/fix-ascii",
        lambda q, b, m: wrap(
            comp.save,
            did(m),
            {
                k: ascii_fix((comp.get(did(m))["current"] or {}).get(k) or "")
                for k in ("subject", "body")
            },
            "me",
            "fix ASCII",
        ),
    )
    api.add(
        "POST",
        r"/api/drafts/(\d+)/restore",
        lambda q, b, m: wrap(comp.restore, did(m), int((b or {})["version"])),
    )
    api.add("POST", r"/api/drafts/(\d+)/approve", lambda q, b, m: wrap(comp.approve, did(m)))
    api.add("POST", r"/api/drafts/(\d+)/unapprove", lambda q, b, m: wrap(comp.unapprove, did(m)))
    api.add("POST", r"/api/drafts/(\d+)/review", lambda q, b, m: wrap(comp.review, did(m)))
    api.add("POST", r"/api/drafts/(\d+)/discard", lambda q, b, m: wrap(comp.discard, did(m)))

    def ai(q: dict, b: Any, m: re.Match[str]) -> dict:
        text = "Hi,\n\nThanks for the note. (Demo mode: this is canned text, not AI.)\n\nAda"
        return {
            **wrap(comp.save, did(m), {"body": text}, "ai", "demo draft"),
            "ai": {"model": "demo", "tokens": 0, "seconds": 0},
        }

    api.add("POST", r"/api/drafts/(\d+)/ai", ai)
    api.add("POST", r"/api/drafts/(\d+)/gmail", lambda q, b, m: {"gmail_draft_id": None})

    def send(q: dict, b: Any, m: re.Match[str]) -> dict:
        b = b or {}
        try:
            d, t, v = int(b["draft"]), str(b["token"]), int(b["version"])
        except (KeyError, ValueError, TypeError) as e:
            raise ApiError(400, "draft, token and version are required") from e
        return wrap(comp.confirm, d, t, v, outbox)

    api.add("POST", r"/api/send", send)
    api.add("POST", r"/api/send/(\d+)/cancel", lambda q, b, m: wrap(comp.cancel, did(m)))
    api.add("POST", r"/api/mail/archive", lambda q, b, m: {"archived": [], "demo": True})
    api.add("POST", r"/api/mail/unarchive", lambda q, b, m: {"unarchived": [], "demo": True})
    api.add(
        "POST",
        r"/api/ai/summary",
        lambda q, b, m: {
            "text": "Demo: Ben asks for the handover plan by Friday.",
            "model": "demo",
            "tokens": 0,
            "seconds": 0,
        },
    )
