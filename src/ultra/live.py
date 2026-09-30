"""Live API routes (v0.2): stream, threads, context, refresh jobs, source status.

The server never blocks a request on a slow source. /api/stream returns the cached
stream at once and, if it is older than the poll interval, starts a refresh job in
the background; the page re-asks when /api/status says the job finished.
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ultra import google_auth
from ultra.config import Config, expand
from ultra.ledger import Ledger, LedgerError
from ultra.mail import Mail
from ultra.rules import Rules
from ultra.slack import Slack, SlackError
from ultra.store import Store

if TYPE_CHECKING:
    from ultra.server import Api

FILTERS = {
    "mine": lambda it: it["court"] == "MINE",
    "waiting": lambda it: it["court"] == "WAITING",
    "all": lambda it: it["court"] in ("MINE", "WAITING", "FYI"),
    "slack": lambda it: it["source"] == "slack",
    "tickets": lambda it: it["source"] == "ticket",
    "vip": lambda it: "VIP" in it.get("badges", []),
    "low": lambda it: it["court"] == "LOW",
}


def load_vips(cfg: Config) -> set[str]:
    p = expand(str(cfg.get("mail", "vip_file", "")))
    out: set[str] = set()
    try:
        for raw in p.read_text().splitlines():
            line = raw.split("#", 1)[0].strip().lower()
            if "@" in line:
                out.add(line)
    except OSError:
        pass
    return out


class Live:
    def __init__(self, cfg: Config, store: Store | None = None):
        self.cfg = cfg
        self.store = store or Store()
        self.rules = Rules.from_config(cfg, load_vips(cfg))
        self.mail = Mail(cfg, self.store, self.rules)
        self.ledger = Ledger(cfg, self.store)
        self.slack = Slack(cfg, self.store)
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="ultra")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.jl = threading.Lock()
        self.poll_mail = int(cfg.get("server", "poll_mail_seconds", 120))
        self.poll_slack = int(cfg.get("server", "poll_slack_minutes", 15)) * 60

    # ---------------------------------------------------------------- jobs
    def _job(self, name: str, fn: Any, *args: Any) -> None:
        """Run fn in the pool unless a job of that name is already running."""
        with self.jl:
            j = self.jobs.get(name)
            if j and j["state"] == "running":
                return
            self.jobs[name] = {"state": "running", "started": time.time(), "error": ""}

        def run() -> None:
            try:
                fn(*args)
                state, err = "done", ""
            except Exception as e:  # noqa: BLE001 - reported to the page as status
                state, err = "error", str(e)
            with self.jl:
                self.jobs[name] = {
                    "state": state,
                    "started": self.jobs[name]["started"],
                    "ended": time.time(),
                    "error": err,
                }

        self.pool.submit(run)

    def warm(self) -> None:
        """Kick off the first refreshes when the server starts."""
        self._job("mail", self.mail.refresh)
        if self.slack.enabled:
            self._job("slack", self.slack.refresh)

    # ---------------------------------------------------------------- routes
    def register(self, api: Api) -> None:
        api.add("GET", r"/api/stream", self.r_stream)
        api.add("POST", r"/api/refresh", self.r_refresh)
        api.add("GET", r"/api/status", self.r_status)
        api.add("GET", r"/api/thread/g-([A-Za-z0-9]+)", self.r_thread)
        api.add("GET", r"/api/thread/k-([A-Z]+\d+)", self.r_ticket)
        api.add("GET", r"/api/thread/s-([A-Za-z0-9_.:-]+)", self.r_slack_thread)
        api.add("GET", r"/api/context/([^/]+)", self.r_context)

    def _merged(self) -> tuple[list[dict[str, Any]], float | None]:
        mail, age = self.mail.stream(self.poll_mail)
        items = list((mail or {}).get("items") or [])
        if self.slack.enabled:
            s_items, _ = self.slack.items()
            items += Slack.to_stream(s_items)
        items.sort(key=lambda x: x.get("ts_ms", 0), reverse=True)
        return items, age

    def r_stream(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        f = (q.get("filter") or ["mine"])[0]
        items, age = self._merged()
        if age is None or age > self.poll_mail:
            self._job("mail", self.mail.refresh)
        s_age = self.slack.items()[1]
        if self.slack.enabled and (s_age is None or s_age > self.poll_slack):
            self._job("slack", self.slack.refresh)
        counts = {k: sum(1 for it in items if fn(it)) for k, fn in FILTERS.items()}
        keep = FILTERS.get(f, FILTERS["mine"])
        rows = [
            {k: v for k, v in it.items() if k != "messages"} for it in items if keep(it)
        ]
        return {"items": rows, "counts": counts, "age": age, "loading": age is None}

    def r_refresh(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        what = (body or {}).get("what", "mail")
        if what in ("mail", "all"):
            self._job("mail", self.mail.refresh)
        if what in ("slack", "all") and self.slack.enabled:
            self._job("slack", self.slack.refresh)
        return {"started": what}

    def r_status(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        with self.jl:
            jobs = {k: dict(v) for k, v in self.jobs.items()}
        mail_age = self.mail.stream(0)[1]
        slack_age = self.slack.items()[1]
        return {
            "jobs": jobs,
            "sources": {
                "mail": {**self.mail.state, "age": mail_age},
                "slack": {
                    **self.slack.state,
                    "enabled": self.slack.enabled,
                    "age": slack_age,
                },
                "ledger": {**self.ledger.state, "enabled": self.ledger.enabled},
            },
        }

    def r_thread(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            return self.mail.thread(m.group(1))
        except google_auth.AuthNeeded as e:
            return {"error": str(e), "auth": e.capability, "messages": []}

    def r_ticket(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        num = m.group(1)
        stream, _ = self.mail.stream(0)
        tk = next(
            (it for it in (stream or {}).get("items", []) if it["key"] == f"k-{num}"),
            None,
        )
        if not tk:
            return {"key": f"k-{num}", "messages": [], "events": []}
        msgs: list[dict[str, Any]] = []
        for tid in tk.get("threads", [])[:10]:
            msgs += self.mail.thread(tid)["messages"]
        msgs.sort(key=lambda x: x["ts"])
        return {"key": f"k-{num}", "messages": msgs, "events": tk.get("events", [])}

    def r_slack_thread(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        key = "s-" + m.group(1)
        items, _ = self.slack.items()
        row = next((r for r in Slack.to_stream(items) if r["key"] == key), None)
        if not row:
            return {"key": key, "messages": []}
        msgs = sorted(row["messages"], key=lambda x: float(x.get("ts") or 0))
        return {
            "key": key,
            "permalink": row.get("permalink", ""),
            "messages": [
                {
                    "from": x.get("from", ""),
                    "ts": row["ts"] if not x.get("ts") else _slack_time(x["ts"]),
                    "body": x.get("text", ""),
                    "quoted": "",
                    "permalink": x.get("permalink", ""),
                }
                for x in msgs
            ],
        }

    def r_context(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        addr = m.group(1).lower()
        name = (q.get("name") or [""])[0]
        if self.rules.is_me(addr):
            return {"self": True}
        try:
            return self.ledger.context(addr, name)
        except (LedgerError, OSError) as e:
            return {"unavailable": True, "reason": str(e)}


def _slack_time(ts: str) -> str:
    from datetime import datetime

    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat()
    except ValueError:
        return ""


__all__ = ["Live", "SlackError"]
