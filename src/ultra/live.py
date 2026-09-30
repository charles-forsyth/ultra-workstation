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
from ultra.ai import AI, AIError
from ultra.audio import Audio
from ultra.compose import ComposeError, Composer, gmail_send
from ultra.config import Config, expand
from ultra.desk import Desk
from ultra.ledger import Ledger, LedgerError
from ultra.ledger_write import LedgerWriter
from ultra.lint import ascii_fix, load_style
from ultra.mail import Mail
from ultra.research import Research
from ultra.rules import Rules
from ultra.slack import Slack, SlackError
from ultra.store import Store
from ultra.tools import Tools

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
        self.ai = AI(cfg)
        self.compose = Composer(cfg, self.store, cfg.my_addresses)
        self.send_fn = gmail_send(cfg)
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
        self.research = Research(cfg, self.store)
        self.audio = Audio(cfg, self.store, self.ai)
        self.tools = Tools(self.research, self.ai, self.audio, self.thread_text_for, self.operator)
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
        self.desk.register(api)
        self.tools.register(api)
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
        api.add("POST", r"/api/drafts/(\d+)/restore", self.r_draft_restore)
        api.add("POST", r"/api/drafts/(\d+)/approve", self.r_draft_approve)
        api.add("POST", r"/api/drafts/(\d+)/unapprove", self.r_draft_unapprove)
        api.add("POST", r"/api/drafts/(\d+)/review", self.r_draft_review)
        api.add("POST", r"/api/drafts/(\d+)/discard", self.r_draft_discard)
        api.add("POST", r"/api/drafts/(\d+)/gmail", self.r_draft_gmail)
        api.add("POST", r"/api/send", self.r_send)
        api.add("POST", r"/api/send/(\d+)/cancel", self.r_send_cancel)

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
                "ai": {**self.ai.state, "enabled": self.ai.enabled, "model": self.ai.model},
                "research": {**self.research.state, "enabled": self.research.enabled},
            },
        }

    def r_thread(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            return self.mail.thread(m.group(1))
        except google_auth.AuthNeeded as e:
            return {"error": str(e), "auth": e.capability, "messages": []}

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
        self.store.journal("archive", ",".join(done), True)
        return {"archived": done}

    def r_unarchive(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        tids = _thread_ids(body)
        self.mail.unarchive(tids)
        self.store.journal("unarchive", ",".join(tids), True)
        self._job("mail", self.mail.refresh)
        return {"unarchived": tids}

    # ---------------------------------------------------------------- AI
    def thread_text_for(self, key: str, for_speech: bool = False) -> str:
        """Plain text of any conversation, for research uploads, web context, audio."""
        t = self.thread_any(key)
        msgs = t.get("messages") or []
        if for_speech:
            return "\n\n".join(f"From {x.get('from', '')}. {x.get('body', '')}" for x in msgs)[
                -60000:
            ]
        subject = next((x.get("subject") for x in msgs if x.get("subject")), "")
        parts = [
            f"From: {x.get('from', '')}\nTo: {x.get('to', '')}\nCc: {x.get('cc', '')}\n"
            f"Date: {x.get('ts', '')}\n\n{x.get('body', '')}"
            for x in msgs
        ]
        return (f"Subject: {subject}\n\n" + "\n\n-----\n\n".join(parts))[-60000:]

    def _thread_text(self, tid: str, limit: int = 30000) -> str:
        t = self.mail.thread(tid)
        parts = [
            f"From: {x['from']}\nTo: {x.get('to', '')}\nDate: {x['ts']}\n"
            f"Subject: {x.get('subject', '')}\n\n{x['body']}"
            for x in t["messages"]
        ]
        text = "\n\n-----\n\n".join(parts)
        return text[-limit:]

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
        return d

    def r_draft_for_thread(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return {"drafts": self.compose.for_thread(m.group(1))}

    def r_draft_save(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._wrap(self.compose.save, self._did(m), body or {}, "me", "edit")

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
        thread_text = self._thread_text(d["thread_id"]) if d["thread_id"] else ""
        style = load_style()
        notes = "; ".join(
            str(r.get("message")) for r in style.get("forbid") or [] if r.get("message")
        )
        sig = str((style.get("signature") or {}).get("text", ""))
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
        r = self.send_fn(d, v)
        self._job("mail", self.mail.refresh)
        return r

    def r_send_cancel(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self._wrap(self.compose.cancel, self._did(m))


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
    from datetime import datetime

    try:
        return datetime.fromtimestamp(float(ts)).astimezone().isoformat()
    except ValueError:
        return ""


__all__ = ["Live", "SlackError"]
