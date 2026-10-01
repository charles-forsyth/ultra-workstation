"""v0.13 routes: Ask Hermes from any item, page or selection (SPEC 7.12).

    GET  /api/hermes/status          enabled, toolsets, whether web may be ticked
    POST /api/hermes/preview         {target} -> {title, kind, chars, text}: what will be sent
    POST /api/hermes/ask             {target, question, session?, web, full, resend} -> {job}
    GET  /api/hermes/job/<id>        running | done {result} | error

A target names what the operator is looking at; the server rebuilds its text from its
own readers (never from text the page sends, except a highlighted selection):

    {"type": "item", "key": "g-..."|"k-..."|"s-..."|"t-<uuid>"|"c-<cal>~<event>"}
    {"type": "day", "view": "plan"|"report", "day": "YYYY-MM-DD"}
    {"type": "entity", "kind": "people"|"labs"|..., "key": "..."}
    {"type": "report", "id": 123}
    {"type": "text", "text": "...", "key": optional parent item, "title": "..."}
    {"type": "none"}

Asking is read-only and runs only on the Ask button (see hermes.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ultra.hermes import Hermes, HermesError

if TYPE_CHECKING:
    from ultra.server import Api

ITEM_RE = re.compile(
    r"^(g|k|s)-[A-Za-z0-9_-]{1,80}$|^t-[0-9a-f-]{36}$|^c-[A-Za-z0-9_.@#-]{1,200}~[A-Za-z0-9_-]{5,300}$"
)
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
KINDS = ("people", "labs", "gcp", "projects", "grants", "assets", "tasks", "interactions")
MAX_SELECTION = 20_000
LEDGER_HEAD = "LEDGER CONTEXT (people, labs, history, tasks)"

# (title, kind label, text)
Built = tuple[str, str, str]


def _bad(msg: str, status: int = 400) -> Exception:
    from ultra.server import ApiError

    return ApiError(status, msg)


def messages_text(messages: list[dict[str, Any]]) -> str:
    """Every message of an item, oldest first, with its headers."""
    parts = []
    for i, m in enumerate(messages, 1):
        head = [f"[Message {i}] From: {m.get('from', '')}"]
        for k, label in (("to", "To"), ("cc", "Cc"), ("ts", "Date"), ("subject", "Subject")):
            if m.get(k):
                head.append(f"{label}: {m[k]}")
        parts.append("\n".join(head) + "\n\n" + str(m.get("body") or ""))
    return "\n\n-----\n\n".join(parts)


def item_kind(key: str) -> str:
    return {"g": "email", "k": "ticket", "s": "Slack", "t": "task", "c": "calendar event"}.get(
        key[:1], "item"
    )


class AskDesk:
    def __init__(
        self,
        hermes: Hermes,
        item_fn: Callable[[str], tuple[str, list[dict[str, Any]], Any]],
        full_fn: Callable[[str, list[dict[str, Any]], Any], str] | None = None,
        day_fn: Callable[[str, str], str] | None = None,
        entity_fn: Callable[[str, str], tuple[str, str]] | None = None,
        report_fn: Callable[[int], tuple[str, str]] | None = None,
    ):
        """item_fn(key) -> (title, messages, task); full_fn(key, messages, task) -> the
        item's ledger context as text; day_fn(view, day) -> text; entity_fn(kind, key) ->
        (title, text); report_fn(id) -> (title, text)."""
        self.hermes = hermes
        self.item_fn = item_fn
        self.full_fn = full_fn
        self.day_fn = day_fn
        self.entity_fn = entity_fn
        self.report_fn = report_fn

    def register(self, api: Api) -> None:
        api.add("GET", r"/api/hermes/status", self.r_status)
        api.add("POST", r"/api/hermes/preview", self.r_preview)
        api.add("POST", r"/api/hermes/ask", self.r_ask)
        api.add("GET", r"/api/hermes/job/([0-9a-f]{24})", self.r_job)

    # ---------------------------------------------------------------- context
    def build(self, target: Any, full: bool = False) -> Built:
        """(title, kind, text) for a target. `full` adds the item's ledger context."""
        t = target if isinstance(target, dict) else {}
        typ = str(t.get("type") or "none")
        if typ == "none":
            return "", "question", ""
        if typ == "item":
            key = str(t.get("key") or "")
            if not ITEM_RE.match(key):
                raise _bad("bad item key")
            title, msgs, task = self.item_fn(key)
            text = messages_text(msgs)
            if full and self.full_fn:
                try:
                    extra = self.full_fn(key, msgs, task)
                except Exception as e:  # noqa: BLE001 - ask without it rather than fail
                    extra = f"(ledger context unavailable: {str(e)[:160]})"
                if extra:
                    text += f"\n\n===== {LEDGER_HEAD} =====\n{extra}"
            return title, item_kind(key), text
        if typ == "day":
            view, day = str(t.get("view") or "plan"), str(t.get("day") or "")
            if view not in ("plan", "report") or not DAY_RE.match(day):
                raise _bad("day target needs view plan|report and day YYYY-MM-DD")
            if not self.day_fn:
                raise _bad("Day is not available")
            label = "Check-in plan" if view == "plan" else "End-of-day report"
            return f"{label} for {day}", "day page", self.day_fn(view, day)
        if typ == "entity":
            kind, key = str(t.get("kind") or ""), str(t.get("key") or "").strip()
            if kind not in KINDS or not key or len(key) > 200:
                raise _bad("bad ledger entity")
            if not self.entity_fn:
                raise _bad("ledger is not available")
            title, text = self.entity_fn(kind, key)
            return title, f"ledger {kind[:-1] if kind.endswith('s') else kind}", text
        if typ == "report":
            rid = str(t.get("id") or "")
            if not re.fullmatch(r"\d{1,9}", rid):
                raise _bad("bad report id")
            if not self.report_fn:
                raise _bad("research is not available")
            title, text = self.report_fn(int(rid))
            return title, "research report", text
        if typ == "text":
            sel = str(t.get("text") or "").strip()
            if not sel:
                raise _bad("select some text first")
            sel = sel[:MAX_SELECTION]
            title = str(t.get("title") or "Selection")[:200]
            key = str(t.get("key") or "")
            text = f"Highlighted passage:\n{sel}"
            if key and ITEM_RE.match(key):
                try:
                    ptitle, msgs, _ = self.item_fn(key)
                except Exception:  # noqa: BLE001 - the passage alone still works
                    ptitle, msgs = "", []
                if msgs:
                    text += f"\n\n===== THE ITEM IT CAME FROM: {ptitle} =====\n"
                    text += messages_text(msgs)
            return title, "selection", text
        raise _bad("unknown target type")

    # ---------------------------------------------------------------- routes
    def r_status(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        h = self.hermes
        return {
            "enabled": h.enabled,
            "toolsets": h.toolsets,
            "web_ok": h.web_ok,
            "source": h.source,
            "budget_seconds": h.budget,
        }

    def r_preview(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        title, kind, text = self.build(b.get("target"), full=False)
        return {
            "title": title,
            "kind": kind,
            "chars": len(text),
            "text": text,
            "can_full": (b.get("target") or {}).get("type") == "item" and bool(self.full_fn),
        }

    def r_ask(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        if not self.hermes.enabled:
            raise _bad("Hermes is not available (hermes CLI not found)", 503)
        question = str(b.get("question") or "").strip()
        if len(question) < 2:
            raise _bad("write a question first")
        session = str(b.get("session") or "") or None
        if session and not re.fullmatch(r"\d{8}_\d{6}_[0-9a-f]{6}", session):
            raise _bad("bad session id")
        target = b.get("target")
        full = b.get("full") is True
        web = b.get("web") is True
        # follow-ups carry only the question unless the operator re-sends the context
        send_ctx = not session or b.get("resend") is True
        if not isinstance(target, dict) or target.get("type") not in (
            None,
            "none",
            "item",
            "day",
            "entity",
            "report",
            "text",
        ):
            raise _bad("unknown target type")
        title, kind, text0 = self.build(target, full=False)  # validates; fails fast

        def run() -> dict[str, Any]:
            text = ""
            if send_ctx:
                text = self.build(target, full=True)[2] if full else text0
            try:
                res = self.hermes.ask(
                    question,
                    context=text,
                    title=title,
                    kind=kind,
                    session=session,
                    web=web,
                )
            except HermesError as e:
                raise RuntimeError(str(e)) from e
            res["sent_chars"] = len(text)
            res["sent_context"] = send_ctx
            return res

        return {"job": self.hermes.start(fn=run)}

    def r_job(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        j = self.hermes.job(m.group(1))
        if j is None:
            raise _bad("no such job", 404)
        return j
