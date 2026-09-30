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
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ultra import google_auth
from ultra.ai import AI, AIError
from ultra.audio import Audio
from ultra.calendar import Calendar
from ultra.compose import ComposeError, Composer, gmail_send
from ultra.config import Config, expand
from ultra.day import Day
from ultra.desk import Desk
from ultra.itemctx import ItemContext
from ultra.itemdesk import ItemDesk
from ultra.ledger import Ledger, LedgerError
from ultra.ledger_write import LedgerWriter
from ultra.lint import ascii_fix, load_style
from ultra.mail import Mail
from ultra.research import Research
from ultra.rules import Rules
from ultra.slack import Slack, SlackError, SlackSender, slack_reply_target
from ultra.sources import HouseFacts, Sources, set_extra_generic
from ultra.store import Store
from ultra.studio import Studio
from ultra.tasks import (
    slack_done,
    slack_undone,
    slack_visible,
    snooze_task,
    task_rows,
    unsnooze_task,
)
from ultra.today import Today
from ultra.tools import Tools

if TYPE_CHECKING:
    from ultra.server import Api

FILTERS = {
    "mine": lambda it: it["court"] == "MINE",
    "waiting": lambda it: it["court"] == "WAITING",
    "all": lambda it: it["court"] in ("MINE", "WAITING", "FYI", "TASK"),
    "slack": lambda it: it["source"] == "slack",
    "tasks": lambda it: it["source"] == "task",
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
        self.ai = AI(cfg)
        self.compose = Composer(cfg, self.store, cfg.my_addresses)
        self.send_fn = gmail_send(cfg)
        self.slack_sender = SlackSender(self.slack) if self.slack.enabled else None
        self.operator = str(cfg.get("operator", "name", ""))
        addrs = list(cfg.get("operator", "addresses", []) or [])
        self.from_default = addrs[0] if addrs else "me"
        self.writer = LedgerWriter(cfg, self.store)
        self.desk = Desk(
            self.store,
            self.rules,
            self.ledger,
            self.writer,
            self.thread_any,
            cfg.timezone,
            me_netid=str(cfg.get("ledger", "my_id", "") or ""),
            ai=self.ai,
            operator=self.operator,
        )
        me_ids = {str(cfg.get("ledger", "my_id", "") or "")}
        me_ids |= {a.split("@")[0] for a in addrs if "@" in a}
        self.ictx = ItemContext(self.ledger, self.store, self.rules, me_ids)
        self.itemdesk = ItemDesk(
            self.ictx,
            self.thread_any,
            lambda tid: self.r_task_thread({}, None, re.match(r"(.+)", tid)),  # type: ignore[arg-type]
            self.writer,
            self.ai,
            self.store,
            self.operator,
        )
        self.research = Research(cfg, self.store)
        self.audio = Audio(cfg, self.store, self.ai)
        self.tools = Tools(self.research, self.ai, self.audio, self.thread_text_for, self.operator)
        self.calendar = Calendar(cfg, self.store)
        self.today = Today(self.calendar, self.block_info, self.rules)
        wh = [int(str(x).split(":")[0]) for x in self.calendar.work_hours]
        self.day = Day(
            self.store,
            self.calendar.tz,
            self.calendar.day,
            lambda: self._merged()[0],
            self._open_tasks_cached,
            self.ai,
            self.operator,
            (int(wh[0]), int(wh[1])),
        )
        set_extra_generic(list(cfg.get("draft", "generic_words", []) or []))
        self.facts = HouseFacts(self.store)
        self.studio = Studio(
            self.store,
            self.ai,
            self.thread_any,
            self.mail.search_messages,
            self._studio_ledger,
            Sources(list(cfg.get("draft", "sources", []) or []), self.store),
            self.facts,
            cfg.my_addresses,
            self.operator,
            str(cfg.get("draft", "notes_dir", "") or ""),
            self._style_notes,
            lambda: str((load_style().get("signature") or {}).get("text", "")),
            task_fn=self._studio_task,
        )
        self.show_tasks = bool(cfg.get("ledger", "tasks_in_stream", True))
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
        api.add("GET", r"/api/thread/t-([0-9a-f-]{36})", self.r_task_thread)
        api.add("POST", r"/api/task/action", self.r_task_action)
        api.add("POST", r"/api/slack/done", self.r_slack_done)
        api.add("POST", r"/api/slack/undone", self.r_slack_undone)
        api.add("GET", r"/api/context/([^/]+)", self.r_context)
        self.desk.register(api)
        self.itemdesk.register(api)
        self.tools.register(api)
        self.today.register(api)
        self.day.register(api)
        self.studio.register(api)
        # triage
        api.add("POST", r"/api/mail/archive", self.r_archive)
        api.add("POST", r"/api/mail/unarchive", self.r_unarchive)
        # AI
        api.add("POST", r"/api/ai/summary", self.r_summary)
        # composer
        api.add("POST", r"/api/drafts", self.r_draft_new)
        api.add("GET", r"/api/drafts/(\d+)", self.r_draft_get)
        api.add("GET", r"/api/drafts/thread/([A-Za-z0-9]+)", self.r_draft_for_thread)
        api.add("POST", r"/api/drafts/(\d+)/versions", self.r_draft_save)
        api.add("POST", r"/api/drafts/(\d+)/ai", self.r_draft_ai)
        api.add("POST", r"/api/drafts/(\d+)/fix-ascii", self.r_draft_ascii)
        api.add("POST", r"/api/drafts/(\d+)/studio", self.r_draft_studio)
        api.add("POST", r"/api/drafts/from-task", self.r_draft_from_task)
        api.add("GET", r"/api/drafts/task/([0-9a-f-]{36})", self.r_drafts_for_task)
        api.add("POST", r"/api/drafts/(\d+)/restore", self.r_draft_restore)
        api.add("POST", r"/api/drafts/(\d+)/approve", self.r_draft_approve)
        api.add("POST", r"/api/drafts/(\d+)/unapprove", self.r_draft_unapprove)
        api.add("POST", r"/api/drafts/(\d+)/review", self.r_draft_review)
        api.add("POST", r"/api/drafts/(\d+)/discard", self.r_draft_discard)
        api.add("POST", r"/api/drafts/(\d+)/gmail", self.r_draft_gmail)
        api.add("POST", r"/api/send", self.r_send)
        api.add("POST", r"/api/slack/draft", self.r_slack_draft)
        api.add("GET", r"/api/slack/draft/(s-[A-Za-z0-9_-]+)", self.r_slack_draft_find)
        api.add("POST", r"/api/send/(\d+)/cancel", self.r_send_cancel)

    # ---------------------------------------------------------------- Draft Studio glue
    def _studio_ledger(self, key: str, msgs: list[dict[str, Any]]) -> str:
        """The item's Full context as text (cached by the Full tab), or "" if the
        ledger is unavailable. Draft Studio runs without it rather than failing."""
        from ultra.itemctx import context_text

        if not self.ledger.enabled:
            return ""
        return context_text(self.ictx.full(key, msgs, None))

    def _studio_task(self, tid: str) -> dict[str, Any]:
        """Task + links for Draft Studio task mode (a fresh ledger read)."""
        r = self.r_task_thread({}, None, re.match(r"(.+)", tid))  # type: ignore[arg-type]
        if not r.get("task"):
            raise LedgerError(str(r.get("error") or "task not found"))
        return r

    @staticmethod
    def _style_notes() -> str:
        style = load_style()
        return "; ".join(
            str(r.get("message")) for r in style.get("forbid") or [] if r.get("message")
        )

    def _merged(self) -> tuple[list[dict[str, Any]], float | None]:
        mail, age = self.mail.stream(self.poll_mail)
        items = list((mail or {}).get("items") or [])
        if self.slack.enabled:
            s_items, _ = self.slack.items()
            items += [r for r in Slack.to_stream(s_items) if slack_visible(self.store, r)]
        items += self._task_rows()
        items.sort(key=lambda x: x.get("ts_ms", 0), reverse=True)
        return items, age

    def _task_rows(self) -> list[dict[str, Any]]:
        """Open ledger tasks from cache; refreshed in the background (5 min TTL)."""
        if not self.ledger.enabled or not self.show_tasks:
            return []
        hit = self.store.cache_get("tasks:open")
        if hit is None or hit[1] > 300:
            self._job("tasks", self._refresh_tasks)
        return task_rows((hit or [[]])[0] or [], self.store)

    def _open_tasks_cached(self) -> dict[str, dict[str, Any]]:
        hit = self.store.cache_get("tasks:open")
        rows: list[dict[str, Any]] = (hit or [[]])[0] or []
        return {t["id"]: t for t in rows if t.get("status") != "DONE"}

    def _refresh_tasks(self) -> None:
        self.store.cache_put("tasks:open", self.ledger._run(["tasks", "list"]) or [])

    def _kick_stale(self) -> None:
        """Start a refresh for any source older than its interval.

        Called from /api/stream and from the page's 5 s status poll, so sources stay
        fresh while a tab is open and nothing polls when no tab is open (Slack reads
        run a model, so no background polling without a viewer). _job never runs two
        of the same name at once, so several open tabs do not double up.
        """
        age = self.mail.stream(0)[1]
        if age is None or age > self.poll_mail:
            self._job("mail", self.mail.refresh)
        s_age = self.slack.items()[1]
        if self.slack.enabled and (s_age is None or s_age > self.poll_slack):
            self._job("slack", self.slack.refresh)
        if self.ledger.enabled and self.show_tasks:
            hit = self.store.cache_get("tasks:open")
            if hit is None or hit[1] > 300:
                self._job("tasks", self._refresh_tasks)

    def r_stream(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        f = (q.get("filter") or ["mine"])[0]
        items, age = self._merged()
        self._kick_stale()
        counts = {k: sum(1 for it in items if fn(it)) for k, fn in FILTERS.items()}
        keep = FILTERS.get(f, FILTERS["mine"])
        rows = [{k: v for k, v in it.items() if k != "messages"} for it in items if keep(it)]
        return {"items": rows, "counts": counts, "age": age, "loading": age is None}

    def r_refresh(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        what = (body or {}).get("what", "mail")
        if what in ("mail", "all"):
            self._job("mail", self.mail.refresh)
        if what in ("slack", "all") and self.slack.enabled:
            self._job("slack", self.slack.refresh)
        return {"started": what}

    def r_status(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        self._kick_stale()
        with self.jl:
            jobs = {k: dict(v) for k, v in self.jobs.items()}
        mail_age = self.mail.stream(0)[1]
        slack_age = self.slack.items()[1]
        t_hit = self.store.cache_get("tasks:open")
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
                "tasks": {"age": t_hit[1] if t_hit else None},
                "calendar": {**self.calendar.state},
                "ai": {**self.ai.state, "enabled": self.ai.enabled, "model": self.ai.model},
                "research": {**self.research.state, "enabled": self.research.enabled},
            },
        }

    def r_thread(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            return self.mail.thread(m.group(1))
        except google_auth.AuthNeeded as e:
            return {"error": str(e), "auth": e.capability, "messages": []}

    # ---------------------------------------------------------------- tasks
    def r_task_thread(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        tid = m.group(1)
        try:
            t = self.ledger.task(tid) or {}
            tree = self.ledger.tree_fresh(tid)
        except (LedgerError, OSError) as e:
            return {"key": f"t-{tid}", "task": None, "error": str(e), "messages": []}
        links = [
            {
                "id": c["id"],
                "name": c.get("name", ""),
                "type": c.get("entity_type", ""),
                "edge": c.get("type", ""),
            }
            for c in tree.get("connections") or []
        ]
        snooze = self.store.cache_get(f"snooze:task:{tid}")
        return {
            "key": f"t-{tid}",
            "task": {
                "id": tid,
                "summary": t.get("summary", ""),
                "status": t.get("status", ""),
                "priority": t.get("priority", ""),
                "due_date": t.get("due_date") or "",
                "assigned_to": t.get("assigned_to", ""),
                "details": {
                    k: v
                    for k, v in (t.get("details") or {}).items()
                    if isinstance(v, (str, int, float))
                },
                "snoozed_until": snooze[0]["until"] if snooze else None,
            },
            "links": links,
            "messages": [],
        }

    def r_task_action(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """complete / start / block / reopen / priority / due / snooze / unsnooze."""
        from ultra.ledger_write import UUID, WriteError

        b = body or {}
        tid = str(b.get("id", "")).lower()
        if not UUID.match(tid):
            raise _bad("task id must be a UUID")
        action = b.get("action")
        status = {
            "complete": "DONE",
            "start": "IN_PROGRESS",
            "block": "BLOCKED",
            "reopen": "TODO",
        }.get(str(action))
        try:
            if status:
                r = self.writer.task_status(tid, status)
            elif action == "priority":
                r = self.writer.task_priority(tid, str(b.get("priority", "")))
            elif action == "due":
                want = str(b.get("due") or "").strip()
                r = self.writer.task_due(tid, want)
                if r.get("ok"):
                    # read it back from the ledger before reporting success
                    got = str((self.ledger.task(tid) or {}).get("due_date") or "")[:10]
                    r["due_date"] = got
                    if got != want:
                        r["ok"] = False
                        r["output_tail"] = (
                            f"read back {got or 'no due date'}, expected {want or 'none'}"
                        )
            elif action == "snooze":
                return {"ok": True, "until": snooze_task(self.store, tid, int(b.get("days", 1)))}
            elif action == "unsnooze":
                unsnooze_task(self.store, tid)
                return {"ok": True}
            else:
                raise _bad("unknown task action")
        except WriteError as e:
            raise _bad(str(e)) from e
        if r.get("ok"):
            self._job("tasks", self._refresh_tasks)
        return r

    def r_slack_done(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        key = str((body or {}).get("key", ""))
        if not re.fullmatch(r"s-[A-Za-z0-9_.:-]{1,80}", key):
            raise _bad("bad Slack key")
        items, _ = self.slack.items()
        row = next((r for r in Slack.to_stream(items) if r["key"] == key), None)
        slack_done(self.store, key, (row or {}).get("ts_ms", int(time.time() * 1000)))
        self.store.journal("slack_done", key, True)
        return {"ok": True}

    def r_slack_undone(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        key = str((body or {}).get("key", ""))
        if not re.fullmatch(r"s-[A-Za-z0-9_.:-]{1,80}", key):
            raise _bad("bad Slack key")
        slack_undone(self.store, key)
        return {"ok": True}

    def thread_any(self, key: str) -> dict[str, Any]:
        """Messages for any stream key (email thread, ticket card, Slack row)."""
        if key.startswith("g-"):
            return self.mail.thread(key[2:])
        rx = re.match(r"^(k|s)-(.+)$", key)
        if not rx:
            return {"key": key, "messages": []}
        if rx.group(1) == "k":
            return self.r_ticket({}, None, re.match(r"(.+)", rx.group(2)))  # type: ignore[arg-type]
        items, _ = self.slack.items()
        row = next((r for r in Slack.to_stream(items) if r["key"] == key), None)
        msgs = sorted((row or {}).get("messages") or [], key=lambda x: float(x.get("ts") or 0))
        return {
            "key": key,
            "permalink": (row or {}).get("permalink", ""),
            "messages": [
                {
                    "from": x.get("from", ""),
                    "from_email": x.get("from_email", ""),
                    "subject": (row or {}).get("subject", "Slack"),
                    "ts": _slack_time(x["ts"]) if x.get("ts") else "",
                    "body": x.get("text", ""),
                    "mine": False,
                }
                for x in msgs
            ],
        }

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
            "reply_target": slack_reply_target(row),
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

    # ---------------------------------------------------------------- triage
    def r_archive(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        tids = _thread_ids(body)
        done = self.mail.archive(tids)
        subj = ""
        stream, _ = self.mail.stream(0)
        for it in (stream or {}).get("items") or []:
            if set(it.get("threads") or [it.get("key", "")[2:]]) & set(done):
                subj = str(it.get("subject") or "")[:80]
                break
        self.store.journal("archive", ",".join(done), True, {"subject": subj} if subj else None)
        return {"archived": done}

    def r_unarchive(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        tids = _thread_ids(body)
        self.mail.unarchive(tids)
        self.store.journal("unarchive", ",".join(tids), True)
        self._job("mail", self.mail.refresh)
        return {"unarchived": tids}

    # ---------------------------------------------------------------- AI
    def block_info(self, key: str) -> dict[str, Any]:
        """Title and description material for a calendar block from a stream item.

        Deterministic: the subject, the last message trimmed, and the open draft if one
        exists. No AI call (the operator can paste an AI summary in before creating).
        """
        info: dict[str, Any] = {"subject": "", "summary": "", "draft": "", "task_id": "", "url": ""}
        if key.startswith("t-"):
            t = self.ledger.task(key[2:]) or {}
            info["subject"] = t.get("summary", "task")
            info["task_id"] = key[2:]
            return info
        t = self.thread_any(key)
        msgs = t.get("messages") or []
        info["subject"] = next((x.get("subject") for x in msgs if x.get("subject")), "")
        if msgs:
            last = msgs[-1]
            body = re.sub(r"\s+", " ", str(last.get("body") or "")).strip()
            info["summary"] = f"Last from {last.get('from', '')}: {body[:600]}"
        if key.startswith("g-"):
            info["url"] = f"https://mail.google.com/mail/u/0/#inbox/{key[2:]}"
            drafts = self.compose.for_thread(key[2:])
            cur = (drafts[0].get("current") or {}) if drafts else {}
            info["draft"] = str(cur.get("body") or "")[:3000]
        elif t.get("permalink"):
            info["url"] = t["permalink"]
        return info

    def thread_text_for(self, key: str, for_speech: bool = False) -> str:
        """Plain text of the WHOLE conversation, for audio, research uploads, web context.

        Every message in order, oldest first. No character cap here: callers that must
        limit size (audio, uploads) apply their own, far larger, limits. The spoken
        version names the speaker and date for each message so a listener can follow.
        """
        t = self.thread_any(key)
        msgs = t.get("messages") or []
        subject = next((x.get("subject") for x in msgs if x.get("subject")), "")
        if for_speech:
            n = len(msgs)
            parts = [
                f"Message {i} of {n}, from {_spoken_name(x.get('from', ''))}"
                + (f", {_spoken_date(x.get('ts', ''))}" if x.get("ts") else "")
                + f".\n{x.get('body', '')}"
                for i, x in enumerate(msgs, 1)
            ]
            head = f"{subject}.\n\n" if subject else ""
            return head + "\n\n".join(parts)
        parts = [
            f"From: {x.get('from', '')}\nTo: {x.get('to', '')}\nCc: {x.get('cc', '')}\n"
            f"Date: {x.get('ts', '')}\n\n{x.get('body', '')}"
            for x in msgs
        ]
        return f"Subject: {subject}\n\n" + "\n\n-----\n\n".join(parts)

    def _slack_text(self, d: dict[str, Any]) -> str:
        """The Slack conversation a Slack draft answers, as plain text for the AI."""
        channel = (d["thread_id"] or "").removeprefix("slack:")
        items, _ = self.slack.items()
        rows = [
            r
            for r in Slack.to_stream(items)
            if (slack_reply_target(r) or {}).get("channel_id") == channel
            and (slack_reply_target(r) or {}).get("thread_ts") == (d["in_reply_to"] or "")
        ]
        msgs = sorted((rows[0]["messages"] if rows else []), key=lambda x: float(x.get("ts") or 0))
        return "\n\n".join(f"{x.get('from', '')}: {x.get('text', '')}" for x in msgs)[-400_000:]

    def _thread_text(self, tid: str, limit: int = 400_000) -> str:
        """Whole email thread for the AI (summary, draft, revise). The limit is a
        runaway guard far above real threads; when it applies, the OLDEST messages are
        dropped and the text says so, rather than silently losing context."""
        t = self.mail.thread(tid)
        parts = [
            f"From: {x['from']}\nTo: {x.get('to', '')}\nDate: {x['ts']}\n"
            f"Subject: {x.get('subject', '')}\n\n{x['body']}"
            for x in t["messages"]
        ]
        text = "\n\n-----\n\n".join(parts)
        if len(text) > limit:
            text = "[Earlier messages omitted for length]\n\n" + text[-limit:]
        return text

    def r_summary(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        tid = str((body or {}).get("thread", "")).removeprefix("g-")
        if not re.fullmatch(r"[A-Za-z0-9]+", tid):
            raise _bad("thread id required")
        try:
            r = self.ai.summary(self._thread_text(tid), self.operator)
        except AIError as e:
            raise _bad(str(e), 502) from e
        return {"text": r.text, "model": r.model, "tokens": r.tokens, "seconds": r.seconds}

    # ---------------------------------------------------------------- composer
    def _wrap(self, fn: Any, *a: Any) -> Any:
        try:
            return fn(*a)
        except ComposeError as e:
            raise _bad(str(e), e.status) from e

    def _did(self, m: re.Match[str]) -> int:
        return int(m.group(1))

    def r_draft_new(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        body = body or {}
        kind = body.get("kind", "reply")
        if kind not in ("reply", "reply_all", "forward", "new"):
            raise _bad("kind must be reply, reply_all, forward or new")
        thread = None
        if kind != "new":
            tid = str(body.get("thread", "")).removeprefix("g-")
            if not re.fullmatch(r"[A-Za-z0-9]+", tid):
                raise _bad("thread id required")
            thread = self.mail.thread(tid)
        return self._wrap(self.compose.create, kind, thread, self.from_default)

    def r_draft_get(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        d = self._wrap(self.compose.get, self._did(m))
        err = self.store.cache_get(f"draft:{d['id']}:send_error")
        d["send_error"] = err[0] if err and d["state"] == "APPROVED" else ""
        t = self.store.cache_get(f"draft:{d['id']}:task")
        d["task_id"] = t[0] if t else ""
        return d

    def r_draft_for_thread(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return {"drafts": self.compose.for_thread(m.group(1))}

    def r_draft_save(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._wrap(self.compose.save, self._did(m), body or {}, "me", "edit")

    def r_draft_studio(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """Save Draft Studio text as a new AI version (author=ai). The body comes from
        the studio panel after the operator chose what to cut; it is ASCII-fixed and
        linted like any version and still needs both approvals."""
        b = body or {}
        text = ascii_fix(str(b.get("body", "")))[:50_000].strip()
        if not text:
            raise _bad("empty draft")
        label = str(b.get("label", "Draft Studio"))[:200]
        d = self._wrap(self.compose.get, self._did(m))
        if d["kind"] == "forward" and (d["current"] or {}).get("body"):
            text = text + "\n" + d["current"]["body"]
        return self._wrap(self.compose.save, d["id"], {"body": text}, "ai", label)

    def r_draft_from_task(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """Draft Studio task mode: make (or reuse) a composer draft for a task email.

        With ``thread`` it continues that Gmail thread as reply-all (recipients and
        subject come from the thread, like any reply). Without it, a new email with the
        To, Cc and subject from the brief. The body becomes an AI version; it still needs
        both approvals. The draft remembers its task so the page can offer a task log
        after the send.
        """
        from ultra.studio import EMAIL_RE

        b = body or {}
        tid = str(b.get("task", ""))
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", tid):
            raise _bad("task id required")
        text = ascii_fix(str(b.get("body", "")))[:50_000].strip()
        if not text:
            raise _bad("empty draft")
        env = b.get("envelope") or {}
        thread = str(env.get("thread", "") or "")
        if thread and not re.fullmatch(r"[A-Za-z0-9]{6,40}", thread):
            raise _bad("bad thread id")

        def addr_list(v: Any) -> str:
            vals = v if isinstance(v, list) else str(v or "").split(",")
            out: list[str] = []
            for raw in vals:
                x = str(raw).strip()
                if not x:
                    continue
                if not EMAIL_RE.fullmatch(x):
                    raise _bad(f"not an email address: {x[:80]}")
                if x.lower() not in out:
                    out.append(x.lower())
            return ", ".join(out[:20])

        to, cc = addr_list(env.get("to")), addr_list(env.get("cc"))
        subject = ascii_fix(" ".join(str(env.get("subject", "")).split()))[:200]
        label = str(b.get("label", "Draft Studio (task)"))[:200]
        existing = [d for d in self._task_drafts(tid) if d["state"] in ("DRAFT", "APPROVED")]
        want_kind = "reply_all" if thread else "new"
        d = next(
            (
                x
                for x in existing
                if x["kind"] == want_kind and (x.get("thread_id") or "") == (thread or "")
            ),
            None,
        )
        if not d:
            if thread:
                d = self._wrap(
                    self.compose.create, "reply_all", self.mail.thread(thread), self.from_default
                )
            else:
                if not to:
                    raise _bad("a new email needs at least one To address")
                if not subject:
                    raise _bad("a new email needs a subject")
                d = self._wrap(self.compose.create, "new", None, self.from_default)
            ids = self.store.cache_get(f"draft:task:{tid}")
            self.store.cache_put(
                f"draft:task:{tid}", sorted(set((ids[0] if ids else []) + [d["id"]]))
            )
            self.store.cache_put(f"draft:{d['id']}:task", tid)
        fields: dict[str, Any] = {"body": text}
        if not thread:
            fields.update({"to_addrs": to, "cc": cc, "subject": subject})
        d = self._wrap(self.compose.save, d["id"], fields, "ai", label)
        d["task_id"] = tid
        return d

    def _task_drafts(self, tid: str) -> list[dict[str, Any]]:
        ids = self.store.cache_get(f"draft:task:{tid}")
        out = []
        for did in ids[0] if ids else []:
            try:
                out.append(self.compose.get(int(did)))
            except ComposeError:
                continue
        return out

    def r_drafts_for_task(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        drafts = [
            {**d, "task_id": m.group(1)}
            for d in self._task_drafts(m.group(1))
            if d["state"] not in ("SENT", "DISCARDED")
        ]
        return {"drafts": sorted(drafts, key=lambda d: -d["id"])}

    def r_draft_ascii(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        d = self._wrap(self.compose.get, self._did(m))
        cur = d["current"] or {}
        fixed = {k: ascii_fix(cur.get(k) or "") for k in ("subject", "body")}
        return self._wrap(self.compose.save, d["id"], fixed, "me", "fix ASCII")

    def r_draft_restore(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._wrap(self.compose.restore, self._did(m), int((body or {})["version"]))

    def r_draft_ai(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """AI draft (empty body) or AI revise (existing body). Result is a new DRAFT
        version marked author=ai; it still needs both approvals."""
        body = body or {}
        instruction = str(body.get("instruction", "")).strip()[:2000]
        d = self._wrap(self.compose.get, self._did(m))
        cur = d["current"] or {}
        slack = d["kind"] == "slack"
        if slack:
            thread_text = self._slack_text(d)
        else:
            thread_text = self._thread_text(d["thread_id"]) if d["thread_id"] else ""
        style = load_style()
        notes = "; ".join(
            str(r.get("message")) for r in style.get("forbid") or [] if r.get("message")
        )
        # Slack replies are short chat messages: no email signature
        sig = "" if slack else str((style.get("signature") or {}).get("text", ""))
        if slack:
            notes = (notes + "; " if notes else "") + (
                "This is a Slack chat reply, not an email: one to four short sentences, "
                "no greeting line, no sign-off, no signature"
            )
        ctx = ""
        if body.get("context") and d["thread_id"]:
            ctx = str(body["context"])[:4000]
        try:
            if (cur.get("body") or "").strip() and instruction:
                r = self.ai.revise(cur["body"], instruction, thread_text, self.operator, notes)
                label = f"AI revise: {instruction}"
            else:
                r = self.ai.draft(thread_text, self.operator, instruction, ctx, notes, sig)
                label = f"AI draft: {instruction}" if instruction else "AI draft"
        except AIError as e:
            raise _bad(str(e), 502) from e
        text = r.text.strip()
        if d["kind"] == "forward" and cur.get("body"):
            text = text + "\n" + cur["body"]
        out = self._wrap(self.compose.save, d["id"], {"body": text}, "ai", label[:300])
        out["ai"] = {"model": r.model, "tokens": r.tokens, "seconds": r.seconds}
        return out

    def r_draft_approve(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._wrap(self.compose.approve, self._did(m))

    def r_draft_unapprove(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._wrap(self.compose.unapprove, self._did(m))

    def r_draft_review(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._wrap(self.compose.review, self._did(m))

    def r_draft_discard(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._wrap(self.compose.discard, self._did(m))

    def r_draft_gmail(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            gid = self.compose.sync_gmail_draft(self._did(m))
        except google_auth.AuthNeeded as e:
            raise _bad(str(e), 401) from e
        return {"gmail_draft_id": gid}

    def r_send(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        body = body or {}
        try:
            did, token, ver = int(body["draft"]), str(body["token"]), int(body["version"])
        except (KeyError, ValueError, TypeError) as e:
            raise _bad("draft, token and version are required") from e
        self.store.cache_put(f"draft:{did}:send_error", "")
        res = self._wrap(self.compose.confirm, did, token, ver, self._send_and_refresh)
        self.store.journal("send_queued", str(did), True, {"version": ver})
        return res

    def _send_and_refresh(self, d: dict[str, Any], v: dict[str, Any]) -> dict[str, Any]:
        if d["kind"] == "slack":
            if not self.slack_sender:
                raise RuntimeError("Slack sending is not available")
            channel = (d["thread_id"] or "").removeprefix("slack:")
            res = self.slack_sender.send(channel, d["in_reply_to"] or "", v["body"])
            self.store.journal(
                "slack_sent",
                str(d["id"]),
                bool(res.get("verified", True)),
                {"channel": channel, "chars": len(v["body"])},
            )
            return res
        r = self.send_fn(d, v)
        self._job("mail", self.mail.refresh)
        return r

    def r_slack_draft_find(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """Is there an open Slack draft for this conversation? (No draft is created.)"""
        items, _ = self.slack.items()
        row = next((r for r in Slack.to_stream(items) if r["key"] == m.group(1)), None)
        tgt = slack_reply_target(row) if row else None
        if not tgt:
            return {"draft": None}
        ex = self.compose.for_slack(tgt["channel_id"], tgt["thread_ts"])
        return {"draft": ex[0]["id"] if ex else None}

    def r_slack_draft(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """Open (or resume) a reply draft for a Slack conversation in the stream.

        The target comes from the cached stream row, not from the page, so the page
        cannot aim a draft at an arbitrary channel.
        """
        key = str((body or {}).get("key", ""))
        items, _ = self.slack.items()
        row = next((r for r in Slack.to_stream(items) if r["key"] == key), None)
        if not row:
            raise _bad("That Slack conversation is not in the stream", 404)
        tgt = slack_reply_target(row)
        if not tgt:
            raise _bad("No channel id for this conversation; cannot reply from Ultra", 422)
        existing = self.compose.for_slack(tgt["channel_id"], tgt["thread_ts"])
        if existing:
            return existing[0]
        return self._wrap(
            self.compose.create_slack, tgt["channel_id"], tgt["thread_ts"], tgt["label"]
        )

    def r_send_cancel(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._wrap(self.compose.cancel, self._did(m))


def _spoken_name(frm: str) -> str:
    """'Ben Carter <ben@example.org>' -> 'Ben Carter'; a bare address -> its name part."""
    name = re.sub(r"\s*<[^>]*>\s*", "", frm or "").strip().strip('"')
    return name or (frm or "").split("@")[0] or "someone"


def _spoken_date(ts: str) -> str:
    try:
        return datetime.fromisoformat(ts).strftime("%A %B %-d at %-I:%M %p")
    except (TypeError, ValueError):
        return ""


def _bad(msg: str, status: int = 400) -> Exception:
    from ultra.server import ApiError

    return ApiError(status, msg)


def _thread_ids(body: Any) -> list[str]:
    raw = (body or {}).get("threads") or []
    tids = [str(t).removeprefix("g-") for t in raw][:500]
    if not tids or not all(re.fullmatch(r"[A-Za-z0-9]+", t) for t in tids):
        raise _bad("threads must be a list of Gmail thread ids")
    return tids


def _slack_time(ts: str) -> str:
    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat()
    except ValueError:
        return ""


__all__ = ["Live", "SlackError"]
