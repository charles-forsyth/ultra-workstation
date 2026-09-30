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
