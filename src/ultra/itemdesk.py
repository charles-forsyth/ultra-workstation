"""v0.8 routes: item-level People and Full context, the AI briefing, possible matches,
and Add to ledger (the only path that may run `people add`).

Add to ledger, like sending mail, takes two separate confirmations and a single-use
token bound to the exact fields:
  1. POST /api/person/add/check    fields -> strict checks, duplicate lookups
                                   -> {token1, fields, warnings}
  2. POST /api/person/add/confirm  token1 + the SAME fields -> {token2} (step 2 of 2)
  3. POST /api/person/add/commit   token2 + the SAME fields -> runs `people add`,
                                   reads the record back
Tokens expire (5 min), work once, and are tied to a hash of the fields: changing any
field after step 1 means starting again.
"""

from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ultra.itemctx import ItemContext, context_text, item_text
from ultra.ledger_write import WriteError, check_person
from ultra.lint import ascii_fix

if TYPE_CHECKING:
    from ultra.server import Api

KEY_RE = re.compile(r"^(g|k|s)-[A-Za-z0-9_-]{1,80}$|^t-[0-9a-f-]{36}$")
TOKEN_TTL = 300


def _bad(msg: str, status: int = 400) -> Exception:
    from ultra.server import ApiError

    return ApiError(status, msg)


def _fields_hash(f: dict[str, str]) -> str:
    raw = "\x1f".join(f.get(k, "") for k in ("netid", "name", "title", "dept"))
    return hashlib.sha256(raw.encode()).hexdigest()


class ItemDesk:
    def __init__(
        self,
        ictx: ItemContext,
        thread_fn: Callable[[str], dict[str, Any]],
        task_fn: Callable[[str], dict[str, Any] | None],
        writer: Any,
        ai: Any,
        store: Any,
        operator: str = "",
    ):
        self.ictx = ictx
        self.thread_fn = thread_fn
        self.task_fn = task_fn
        self.writer = writer
        self.ai = ai
        self.store = store
        self.operator = operator
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ultra-item")
        self.jobs: dict[str, dict[str, Any]] = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def register(self, api: Api) -> None:
        k = r"((?:g|k|s)-[A-Za-z0-9_-]{1,80}|t-[0-9a-f-]{36})"
        api.add("GET", rf"/api/item/people/{k}", self.r_people)
        api.add("POST", r"/api/item/full", self.r_full_start)
        api.add("GET", r"/api/item/full/job/([0-9a-f]{20})", self.r_full_job)
        api.add("POST", r"/api/item/briefing", self.r_briefing)
        api.add("POST", r"/api/item/suggest", self.r_suggest)
        api.add("POST", r"/api/person/add/check", self.r_add_check)
        api.add("POST", r"/api/person/add/confirm", self.r_add_confirm)
        api.add("POST", r"/api/person/add/commit", self.r_add_commit)

    # ---------------------------------------------------------------- inputs
    def _item(self, key: str) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
        if not KEY_RE.match(key or ""):
            raise _bad("bad item key")
        if key.startswith("t-"):
            t = self.task_fn(key[2:]) or {}
            task = t.get("task") or {}
            msgs = [
                {
                    "from": "ledger task",
                    "subject": task.get("summary", ""),
                    "ts": "",
                    "body": "\n".join(
                        [str(task.get("summary", ""))]
                        + [f"{k}: {v}" for k, v in (task.get("details") or {}).items()]
                        + [
                            f"linked: {x.get('name')} ({x.get('type')})"
                            for x in t.get("links") or []
                        ]
                    ),
                }
            ]
            return msgs, {
                "id": key[2:],
                "summary": task.get("summary", ""),
                "links": t.get("links") or [],
            }
        return (self.thread_fn(key).get("messages") or []), None

    # ---------------------------------------------------------------- people
    def r_people(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        key = m.group(1)
        msgs, task = self._item(key)
        try:
            ents = self.ictx.entities(key, msgs, task)
        except Exception as e:  # noqa: BLE001
            return {"key": key, "people": [], "unavailable": True, "reason": str(e)[:200]}
        return ents

    # ---------------------------------------------------------------- full
    def r_full_start(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        key = str(b.get("key", ""))
        msgs, task = self._item(key)
        confirmed = [str(x) for x in (b.get("confirmed") or [])][:20]
        fresh = bool(b.get("fresh"))
        jid = secrets.token_hex(10)
        job: dict[str, Any] = {"state": "running", "started": time.time(), "key": key}
        with self.lock:
            self.jobs[jid] = job
            for old in [j for j, v in self.jobs.items() if time.time() - v["started"] > 3600]:
                self.jobs.pop(old, None)

        def run() -> None:
            try:
                job["result"] = self.ictx.full(key, msgs, task, confirmed, fresh)
                job["state"] = "done"
            except Exception as e:  # noqa: BLE001
                job.update(state="error", error=str(e)[:300])

        self.pool.submit(run)
        return {"job": jid}

    def r_full_job(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        job = self.jobs.get(m.group(1))
        if not job:
            raise _bad("no such job", 404)
        out = {k: v for k, v in job.items() if k != "result"}
        out["seconds"] = round(time.time() - job["started"], 1)
        if job.get("state") == "done":
            out["result"] = job["result"]
        return out

    def _ctx_for(self, b: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        key = str(b.get("key", ""))
        msgs, task = self._item(key)
        confirmed = [str(x) for x in (b.get("confirmed") or [])][:20]
        return key, self.ictx.full(key, msgs, task, confirmed)  # cached from the Full tab

    # ---------------------------------------------------------------- briefing
    def r_briefing(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        if self.ai is None or not getattr(self.ai, "enabled", False):
            raise _bad("AI is off")
        b = body or {}
        key, ctx = self._ctx_for(b)
        text = context_text(ctx)
        ck = "brief:" + hashlib.sha256(text.encode()).hexdigest()[:24]
        if not b.get("fresh"):
            hit = self.store.cache_get(ck, 7 * 86400)
            if hit:
                return {**hit[0], "cached": True}
        try:
            r = self.ai.briefing(text, self.operator)
        except Exception as e:
            raise _bad(str(e)[:300], 502) from e
        md = ascii_fix(r.text.strip())
        ids = {h["id"][:8]: h["id"] for h in ctx.get("history") or [] if h.get("kind") == "ledger"}
        out = {
            "key": key,
            "markdown": md,
            "cites": ids,  # 8-char prefix -> full ledger id, for citation links
            "model": r.model,
            "tokens": r.tokens,
            "seconds": round(r.seconds, 1),
            "context_chars": len(text),
        }
        self.store.cache_put(ck, out)
        return out

    # ---------------------------------------------------------------- suggestions
    def r_suggest(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """AI possible matches: picks only from ledger search results, never invents."""
        if self.ai is None or not getattr(self.ai, "enabled", False):
            return {"suggest": [], "ai": False}
        b = body or {}
        key = str(b.get("key", ""))
        msgs, task = self._item(key)
        ck = f"suggest:{key}:{len(item_text(msgs))}"
        hit = self.store.cache_get(ck, 3600)
        if hit and not b.get("fresh"):
            return hit[0]
        ents = self.ictx.entities(key, msgs, task)
        subject = next((x.get("subject") for x in msgs if x.get("subject")), "") or ""
        cands: dict[str, dict[str, Any]] = {}
        terms = [subject] + [p["name"] for p in ents["people"][:4] if p.get("in_ledger")]
        for term in [t for t in terms if t][:5]:
            try:
                for r in self.ictx.ledger.search_raw(term, 15):
                    if r.get("type") in (
                        "Researcher",
                        "Lab",
                        "GCPProject",
                        "ResearchProject",
                        "Grant",
                        "Asset",
                    ):
                        cands.setdefault(
                            r["id"], {"id": r["id"], "name": r.get("name", ""), "type": r["type"]}
                        )
            except Exception:  # noqa: BLE001, S112 - one failed search just adds nothing
                continue
        known = [p["name"] for p in ents["people"]] + [
            x["name"] for v in ents["things"].values() for x in v
        ]
        known_low = {str(k).lower() for k in known}
        cand_list = [c for c in cands.values() if str(c["name"]).lower() not in known_low]
        try:
            picks = self.ai.possible_matches(item_text(msgs), cand_list, known)
        except Exception as e:  # noqa: BLE001
            return {"suggest": [], "error": str(e)[:200]}
        out = {"suggest": [{**p, "kind": "ai"} for p in picks], "ai": True}
        self.store.cache_put(ck, out)
        return out

    # ---------------------------------------------------------------- add to ledger
    def _token(self, stage: str, fields: dict[str, str]) -> str:
        tok = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            for t in [t for t, v in self.tokens.items() if v["exp"] < now]:
                self.tokens.pop(t, None)
            self.tokens[tok] = {
                "stage": stage,
                "hash": _fields_hash(fields),
                "exp": now + TOKEN_TTL,
            }
        return tok

    def _spend(self, tok: str, stage: str, fields: dict[str, str]) -> None:
        with self.lock:
            t = self.tokens.pop(str(tok or ""), None)  # single use, even on failure
        if not t or t["stage"] != stage:
            raise _bad("confirmation expired or already used; start again", 409)
        if t["exp"] < time.time():
            raise _bad("confirmation expired; start again", 409)
        if t["hash"] != _fields_hash(fields):
            raise _bad("the fields changed after you confirmed; start again", 409)

    def _fields(self, b: dict[str, Any]) -> dict[str, str]:
        try:
            return check_person(
                str(b.get("netid", "")),
                str(b.get("name", "")),
                str(b.get("title", "")),
                str(b.get("dept", "")),
            )
        except WriteError as e:
            raise _bad(str(e)) from e

    def _exists(self, netid: str) -> dict[str, Any] | None:
        led = self.ictx.ledger
        p = led._run(["people", "show", netid]) if hasattr(led, "_run") else led.person_show(netid)
        return p if isinstance(p, dict) and p.get("netid") else None

    def r_add_check(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """Step 1: strict checks + duplicate lookups. Nothing is written."""
        b = body or {}
        f = self._fields(b)
        if getattr(self.writer, "enabled", True) is False:
            raise _bad("ledger CLI not found")
        warnings = []
        existing = self._exists(f["netid"])
        if existing:
            raise _bad(f"{f['netid']} is already in the ledger as {existing.get('name', '')}", 409)
        try:
            same = [
                p
                for p in self.ictx.catalog().people
                if " ".join(str(p.get("name", "")).lower().split()) == f["name"].lower()
            ]
        except Exception:  # noqa: BLE001
            same = []
        if same:
            warnings.append(
                "Someone with this exact name is already in the ledger: "
                + ", ".join(f"{p.get('name')} ({p.get('netid')})" for p in same[:3])
            )
        addr = str(b.get("addr", "")).lower()
        if addr and "@" in addr and addr.split("@")[0] != f["netid"]:
            warnings.append(f"The netid does not match the address {addr}; check it.")
        return {"token": self._token("check", f), "fields": f, "warnings": warnings}

    def r_add_confirm(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """Step 2: the operator confirmed once; issue the final token."""
        b = body or {}
        f = self._fields(b)
        self._spend(str(b.get("token", "")), "check", f)
        return {"token": self._token("confirm", f), "fields": f}

    def r_add_commit(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        """Step 3: the second confirmation. Runs `people add` once, reads back."""
        b = body or {}
        # token first: a replayed or missing token is refused before anything else runs
        with self.lock:
            known = str(b.get("token", "")) in self.tokens
        if not known:
            raise _bad("confirmation expired or already used; start again", 409)
        f = self._fields(b)
        self._spend(str(b.get("token", "")), "confirm", f)
        try:
            res = self.writer.person_add(f, self._exists)
        except WriteError as e:
            raise _bad(str(e)) from e
        return res
