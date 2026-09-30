"""Tasks and Slack as stream items (operator request, 2026-09-30).

- Ledger tasks show in the stream next to mail and Slack, colour-coded by priority
  and status, with the same row treatment. Their "archive" is Complete (DONE in the
  ledger), plus Start / Block / Reopen, priority change, and a local Snooze.
- Slack conversations get a local "Done" mark (like archive): the row hides until a
  newer message arrives in that conversation. Nothing is written to Slack.

Snoozes and Slack marks are local only (kv_cache), never written anywhere else.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from ultra.store import Store

PRIORITY_RANK = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
DAY = 86400


def task_rows(tasks: list[dict[str, Any]], store: Store, now: float | None = None) -> list[dict]:
    """Open tasks -> stream rows. Sorted inside the stream by a synthetic ts so HIGH
    and CRITICAL tasks sit near the top of the Tasks filter."""
    now = now or time.time()
    rows: list[dict[str, Any]] = []
    for t in tasks:
        if t.get("status") == "DONE":
            continue
        tid = t["id"]
        snooze = store.cache_get(f"snooze:task:{tid}")
        if snooze and snooze[0].get("until", 0) > now:
            continue
        pri = str(t.get("priority") or "MEDIUM")
        status = str(t.get("status") or "TODO")
        badges = [pri] if pri in ("HIGH", "CRITICAL") else []
        if status == "BLOCKED":
            badges.append("BLOCKED")
        if status == "IN_PROGRESS":
            badges.append("STARTED")
        rank = PRIORITY_RANK.get(pri, 2)
        rows.append(
            {
                "key": f"t-{tid}",
                "source": "task",
                "from": "Task",
                "addr": "",
                "subject": t.get("summary", ""),
                "snippet": f"{status.replace('_', ' ').lower()} - {pri.lower()} priority",
                # a stable order: priority first, then the ledger's own order
                "ts": "",
                "ts_ms": int((now - rank * DAY) * 1000) - len(rows),
                "count": 0,
                "court": "TASK",
                "reason": f"ledger task, {pri.lower()} priority",
                "badges": badges,
                "priority": pri,
                "status": status,
                "waiting_days": 0,
                "unread": False,
            }
        )
    return rows


def snooze_task(store: Store, tid: str, days: int) -> float:
    until = time.time() + max(1, min(days, 30)) * DAY
    store.cache_put(f"snooze:task:{tid}", {"until": until})
    return until


def unsnooze_task(store: Store, tid: str) -> None:
    store.cache_del_prefix(f"snooze:task:{tid}")


# ---------------------------------------------------------------- slack done marks
def slack_done(store: Store, key: str, last_ts_ms: int) -> None:
    store.cache_put(f"slackdone:{key}", {"ts_ms": int(last_ts_ms)})


def slack_undone(store: Store, key: str) -> None:
    store.cache_del_prefix(f"slackdone:{key}")


def slack_visible(store: Store, row: dict[str, Any]) -> bool:
    """Hidden once marked done, until a newer message arrives in the conversation."""
    hit = store.cache_get(f"slackdone:{row['key']}")
    return not hit or int(row.get("ts_ms") or 0) > int(hit[0].get("ts_ms") or 0)


def fmt_due(due: Any) -> str:
    if not due:
        return ""
    try:
        return datetime.fromisoformat(str(due)).strftime("%Y-%m-%d")
    except ValueError:
        return str(due)[:10]
