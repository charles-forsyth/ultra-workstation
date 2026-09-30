"""Gmail adapter, read side (v0.2): inbox + recent Sent threads, full thread view.

Uses the `read` capability only. Thread metadata is cached by Gmail historyId, so a
refresh only fetches threads that changed. Calls go through one lock because the
googleapiclient HTTP object is not thread-safe.
"""

from __future__ import annotations

import base64
import html
import re
import threading
import time
from html.parser import HTMLParser
from typing import Any, ClassVar

from ultra import google_auth
from ultra.config import Config
from ultra.rules import Rules, classify_thread, is_rsvp, parse_addr, ticket_events
from ultra.store import Store

META_HEADERS = ["From", "To", "Cc", "Subject", "Date", "Message-ID"]
MAX_INBOX = 200
BATCH = 40


class _Text(HTMLParser):
    """HTML -> plain text for the default (safe) view."""

    BLOCK: ClassVar[set[str]] = {
        "p",
        "div",
        "br",
        "tr",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "table",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.out.append(data)


def html_to_text(s: str) -> str:
    p = _Text()
    p.feed(s)
    text = html.unescape("".join(p.out))
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _b64(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode(
        "utf-8", "replace"
    )


def extract_body(payload: dict[str, Any]) -> tuple[str, bool, list[dict[str, Any]]]:
    """(text, came_from_html, attachments) from a Gmail message payload."""
    plain: list[str] = []
    htmls: list[str] = []
    atts: list[dict[str, Any]] = []

    def walk(p: dict[str, Any]) -> None:
        mime = p.get("mimeType", "")
        body = p.get("body") or {}
        if p.get("filename"):
            atts.append(
                {
                    "name": p["filename"],
                    "mime": mime,
                    "size": body.get("size", 0),
                    "id": body.get("attachmentId", ""),
                }
            )
            return
        if mime == "text/plain" and body.get("data"):
            plain.append(_b64(body["data"]))
        elif mime == "text/html" and body.get("data"):
            htmls.append(_b64(body["data"]))
        for sub in p.get("parts") or []:
            walk(sub)

    walk(payload)
    if plain:
        return "\n".join(plain).strip(), False, atts
    if htmls:
        return html_to_text("\n".join(htmls)), True, atts
    return "", False, atts


def split_quoted(text: str) -> tuple[str, str]:
    """(new text, quoted history). Folds '> ' lines and 'On ... wrote:' tails."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"^On .{5,200} wrote:\s*$", line) or re.match(
            r"^-{2,}\s*Original Message\s*-{2,}", line, re.I
        ):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:])
        if line.startswith(">") and all(
            (x.startswith(">") or not x.strip()) for x in lines[i:]
        ):
            return "\n".join(lines[:i]).rstrip(), "\n".join(lines[i:])
    return text, ""


def _headers(msg: dict[str, Any]) -> dict[str, str]:
    return {
        h["name"].lower(): h["value"]
        for h in (msg.get("payload") or {}).get("headers", [])
    }


class Mail:
    def __init__(self, cfg: Config, store: Store, rules: Rules):
        self.cfg = cfg
        self.store = store
        self.rules = rules
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"ok": None, "error": "", "at": 0.0}

    def _svc(self) -> Any:
        return google_auth.service(self.cfg, "gmail", "v1", "read")

    # ---------------------------------------------------------------- listing
    def _list_ids(self, q: str, limit: int) -> list[tuple[str, str]]:
        g = self._svc()
        out: list[tuple[str, str]] = []
        tok = None
        while len(out) < limit:
            r = (
                g.users()
                .threads()
                .list(userId="me", q=q, maxResults=min(100, limit), pageToken=tok)
                .execute()
            )
            out += [(t["id"], t.get("historyId", "")) for t in r.get("threads", [])]
            tok = r.get("nextPageToken")
            if not tok:
                break
        return out[:limit]

    def _fetch_meta(self, ids: list[str]) -> dict[str, dict[str, Any]]:
        g = self._svc()
        got: dict[str, dict[str, Any]] = {}
        errors: list[str] = []

        def cb(req_id: str, resp: Any, exc: Exception | None) -> None:
            if exc is not None:
                errors.append(f"{req_id}: {exc}")
            else:
                got[req_id] = resp

        for i in range(0, len(ids), BATCH):
            batch = g.new_batch_http_request(callback=cb)
            for tid in ids[i : i + BATCH]:
                batch.add(
                    g.users()
                    .threads()
                    .get(
                        userId="me",
                        id=tid,
                        format="metadata",
                        metadataHeaders=META_HEADERS,
                    ),
                    request_id=tid,
                )
            batch.execute()
        if errors and not got:
            raise RuntimeError(errors[0])
        return got

    def _summarize(self, t: dict[str, Any]) -> dict[str, Any]:
        msgs = []
        labels: set[str] = set()
        for m in t.get("messages", []):
            h = _headers(m)
            name, addr = parse_addr(h.get("from", ""))
            ml = m.get("labelIds") or []
            labels |= set(ml)
            msgs.append(
                {
                    "id": m["id"],
                    "from_addr": addr,
                    "from_name": name or addr,
                    "to": h.get("to", ""),
                    "cc": h.get("cc", ""),
                    "subject": h.get("subject", ""),
                    "ts_ms": int(m.get("internalDate", 0)),
                    "text": html.unescape(m.get("snippet") or ""),
                    "labels": ml,
                }
            )
        return {
            "id": t["id"],
            "historyId": t.get("historyId"),
            "messages": msgs,
            "labels": sorted(labels),
        }

    def refresh(self) -> dict[str, Any]:
        """Fetch changed threads; rebuild the stream. Returns the stream payload."""
        with self.lock:
            try:
                look = int(self.cfg.get("mail", "sent_lookback_days", 14))
                inbox = self._list_ids("in:inbox", MAX_INBOX)
                sent = self._list_ids(f"in:sent newer_than:{look}d", MAX_INBOX)
                wanted = dict(sent)
                wanted.update(dict(inbox))
                cached: dict[str, Any] = {}
                hit = self.store.cache_get("mail:threads")
                if hit:
                    cached = hit[0]
                stale = [
                    tid
                    for tid, hid in wanted.items()
                    if tid not in cached or cached[tid].get("historyId") != hid
                ]
                fresh = self._fetch_meta(stale) if stale else {}
                threads = {
                    tid: (self._summarize(fresh[tid]) if tid in fresh else cached[tid])
                    for tid in wanted
                    if tid in fresh or tid in cached
                }
                self.store.cache_put("mail:threads", threads)
                inbox_ids = {tid for tid, _ in inbox}
                payload = self._build_stream(threads, inbox_ids)
                self.store.cache_put("mail:stream", payload)
                self.state = {
                    "ok": True,
                    "error": "",
                    "at": time.time(),
                    "fetched": len(stale),
                }
                return payload
            except google_auth.AuthNeeded as e:
                self.state = {
                    "ok": False,
                    "error": str(e),
                    "at": time.time(),
                    "auth": e.capability,
                }
                raise
            except Exception as e:
                self.state = {
                    "ok": False,
                    "error": f"{type(e).__name__}: {e}",
                    "at": time.time(),
                }
                raise

    # ---------------------------------------------------------------- stream
    def _build_stream(
        self, threads: dict[str, Any], inbox_ids: set[str]
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        tickets: dict[str, dict[str, Any]] = {}
        for tid, t in threads.items():
            msgs = t["messages"]
            if not msgs:
                continue
            last = msgs[-1]
            subject = next((m["subject"] for m in msgs if m["subject"]), "(no subject)")
            if is_rsvp(subject):
                continue
            other = next(
                (m for m in reversed(msgs) if not self.rules.is_me(m["from_addr"])),
                None,
            )
            if other is None:  # only I wrote: show who I'm waiting on
                name, addr = _first_recipient(self.rules, msgs)
                label = name or addr or "(no recipient)"
                other = {"from_name": label, "from_addr": addr}
            if any(self.rules.is_ticket_sender(m["from_addr"]) for m in msgs):
                nums = self.rules.ticket_numbers(subject + " " + last["text"])
                if nums:
                    self._add_ticket(
                        tickets, nums[0], tid, t, subject, tid in inbox_ids
                    )
                    continue
            c = classify_thread(self.rules, msgs, tid in inbox_ids)
            if c["court"] == "NONE":
                continue
            items.append(
                {
                    "key": f"g-{tid}",
                    "source": "email",
                    "from": other["from_name"],
                    "addr": other["from_addr"],
                    "subject": subject,
                    "snippet": last["text"][:200],
                    "ts": _iso(last["ts_ms"]),
                    "ts_ms": last["ts_ms"],
                    "count": len(msgs),
                    "court": c["court"],
                    "reason": c["reason"],
                    "badges": c["badges"],
                    "waiting_days": c["waiting_days"],
                    "unread": "UNREAD" in last["labels"],
                }
            )
        for tk in tickets.values():
            items.append(tk)
        items.sort(key=lambda x: x["ts_ms"], reverse=True)
        counts: dict[str, int] = {}
        for it in items:
            counts[it["court"]] = counts.get(it["court"], 0) + 1
        return {"items": items, "counts": counts, "built_at": time.time()}

    def _add_ticket(
        self,
        tickets: dict[str, dict[str, Any]],
        num: str,
        tid: str,
        t: dict[str, Any],
        subject: str,
        in_inbox: bool,
    ) -> None:
        msgs = t["messages"]
        last = msgs[-1]
        events = ticket_events(subject + " " + " ".join(m["text"] for m in msgs))
        tk = tickets.get(num)
        if tk is None:
            tk = tickets[num] = {
                "key": f"k-{num}",
                "source": "ticket",
                "from": num,
                "addr": last["from_addr"],
                "subject": subject,
                "snippet": last["text"][:200],
                "ts": _iso(last["ts_ms"]),
                "ts_ms": last["ts_ms"],
                "count": 0,
                "threads": [],
                "events": [],
                "court": "MINE" if in_inbox else "LOW",
                "reason": "ticket notice in your inbox"
                if in_inbox
                else "archived notices",
                "badges": [],
                "waiting_days": 0,
                "unread": False,
            }
        tk["threads"].append(tid)
        tk["count"] += len(msgs)
        tk["events"] = sorted(set(tk["events"]) | set(events))
        if in_inbox:
            tk["court"] = "MINE"
        if "assigned to you" in events and "ASSIGNED" not in tk["badges"]:
            tk["badges"].append("ASSIGNED")
        if last["ts_ms"] > tk["ts_ms"]:
            tk.update(
                ts=_iso(last["ts_ms"]),
                ts_ms=last["ts_ms"],
                snippet=last["text"][:200],
                subject=subject,
            )

    def stream(self, max_age: float) -> tuple[dict[str, Any] | None, float | None]:
        hit = self.store.cache_get("mail:stream")
        if not hit:
            return None, None
        return hit[0], hit[1]

    # ---------------------------------------------------------------- thread
    def thread(self, tid: str) -> dict[str, Any]:
        with self.lock:
            t = (
                self._svc()
                .users()
                .threads()
                .get(userId="me", id=tid, format="full")
                .execute()
            )
        out = []
        for m in t.get("messages", []):
            h = _headers(m)
            name, addr = parse_addr(h.get("from", ""))
            text, from_html, atts = extract_body(m.get("payload") or {})
            new, quoted = split_quoted(text)
            out.append(
                {
                    "id": m["id"],
                    "from": f"{name} <{addr}>" if name else addr,
                    "from_addr": addr,
                    "mine": self.rules.is_me(addr),
                    "to": h.get("to", ""),
                    "cc": h.get("cc", ""),
                    "subject": h.get("subject", ""),
                    "ts": _iso(int(m.get("internalDate", 0))),
                    "body": new,
                    "quoted": quoted,
                    "from_html": from_html,
                    "attachments": atts,
                    "labels": m.get("labelIds") or [],
                }
            )
        return {"key": f"g-{tid}", "messages": out}


def _first_recipient(rules: Rules, msgs: list[dict[str, Any]]) -> tuple[str, str]:
    """(name, addr) of the first To/Cc recipient who isn't me."""
    import email.utils

    for m in reversed(msgs):
        for field_ in ("to", "cc"):
            for name, addr in email.utils.getaddresses([m.get(field_) or ""]):
                if addr and not rules.is_me(addr):
                    return name.strip().strip('"'), addr.lower()
    return "", ""


def _iso(ms: int) -> str:
    from datetime import UTC, datetime

    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()
