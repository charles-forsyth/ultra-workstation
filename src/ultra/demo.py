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
        "key": "t-100",
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
        "key": "t-400",
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
    "t-100": [
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
        "interactions": [
            {"id": "i-1", "summary": "Ben asked for the ada-lab handover plan."}
        ],
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
