"""Bucket snapshots, thread participants, and staged ledger cards (SPEC 7.2, 11.2).

Deterministic: the default log text is a template, chips come from address
resolution and graph edges. The model is only used when the operator clicks
"Draft text with AI". Nothing here writes to the ledger; ledger_write does, after a
click, and only with the chips the operator left checked.
"""

from __future__ import annotations

import email.utils
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from ultra.lint import ascii_fix
from ultra.rules import Rules

MAX_PEOPLE = 15
GCP_RE = re.compile(
    r"(?:console\.cloud\.google\.com/[^\s]*[?&]project=|\bproject(?:\s+id)?[:\s]+)"
    r"([a-z][a-z0-9-]{4,28}[a-z0-9])\b",
    re.I,
)


class LedgerLike(Protocol):
    enabled: bool

    def resolve(self, addr: str, name: str = "") -> dict[str, Any] | None: ...
    def context(self, addr: str, name: str = "") -> dict[str, Any]: ...
    def search(self, term: str) -> list[dict[str, Any]]: ...


# ---------------------------------------------------------------- participants
def participants(messages: list[dict[str, Any]], rules: Rules) -> list[dict[str, Any]]:
    """Everyone on a thread except the operator and noise senders, senders first."""
    people: dict[str, dict[str, Any]] = {}

    def add(name: str, addr: str, role: str) -> None:
        addr = (addr or "").strip().lower()
        if not addr or "@" not in addr or rules.is_me(addr) or rules.is_noise(addr):
            return
        p = people.setdefault(addr, {"addr": addr, "name": "", "roles": [], "sent": 0})
        if name and not p["name"]:
            p["name"] = name.strip().strip('"')
        if role not in p["roles"]:
            p["roles"].append(role)
        if role == "from":
            p["sent"] += 1

    for m in messages:
        for n, a in email.utils.getaddresses([m.get("from") or ""]):
            add(n, a, "from")
        for field in ("to", "cc"):
            for n, a in email.utils.getaddresses([m.get(field) or ""]):
                add(n, a, field)
        # Slack rows carry a display name and a profile email
        if m.get("from_email"):
            add(m.get("from") or "", m["from_email"], "from")
    order = {"from": 0, "to": 1, "cc": 2}
    return sorted(
        people.values(),
        key=lambda p: (-p["sent"], min(order.get(r, 3) for r in p["roles"]), p["addr"]),
    )


def gcp_candidates(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(1).lower() for m in GCP_RE.finditer(text or "")))[:5]


def local_minute(iso: str, tz: str) -> str:
    """ISO timestamp -> 'YYYY-MM-DD HH:MM' in the operator's zone (SPEC: convert to
    local time before logging; senders use many different UTC offsets)."""
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        dt = datetime.now().astimezone()
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------- snapshots
def snapshot_thread(key: str, thread: dict[str, Any], rules: Rules, kind: str) -> dict[str, Any]:
    msgs = thread.get("messages") or []
    subject = next((m.get("subject") for m in msgs if m.get("subject")), "") or ""
    text_all = "\n".join((m.get("body") or "") for m in msgs)
    latest_other = next((m for m in reversed(msgs) if not m.get("mine")), None)
    latest_mine = next((m for m in reversed(msgs) if m.get("mine")), None)
    return {
        "key": key,
        "kind": kind,
        "subject": subject,
        "count": len(msgs),
        "first_ts": msgs[0]["ts"] if msgs else "",
        "last_ts": msgs[-1]["ts"] if msgs else "",
        "people": participants(msgs, rules),
        "tickets": rules.ticket_numbers(subject + "\n" + text_all)[:10],
        "gcp": gcp_candidates(text_all),
        "latest_other": _msg_brief(latest_other),
        "latest_mine": _msg_brief(latest_mine),
        "permalink": thread.get("permalink", ""),
    }


def _msg_brief(m: dict[str, Any] | None, limit: int = 1200) -> dict[str, Any] | None:
    if not m:
        return None
    return {"from": m.get("from", ""), "ts": m.get("ts", ""), "text": (m.get("body") or "")[:limit]}


def snapshot_entity(ent: dict[str, Any]) -> dict[str, Any]:
    return {
        "key": ent["id"],
        "kind": "entity",
        "subject": ent.get("name", ""),
        "entity_type": ent.get("type") or ent.get("entity_type") or "",
        "people": [],
        "tickets": [],
        "gcp": [],
    }


def snapshot_snippet(text: str, source_key: str, source_subject: str, ts: str) -> dict[str, Any]:
    return {
        "key": f"snip-{abs(hash((source_key, text))) % 10**10}",
        "kind": "snippet",
        "subject": source_subject,
        "source": source_key,
        "text": text[:2000],
        "last_ts": ts,
        "people": [],
        "tickets": [],
        "gcp": [],
    }


# ---------------------------------------------------------------- template text
def template_log(items: list[dict[str, Any]], tz: str) -> str:
    """Plain, factual default text. The operator edits it or asks AI to rewrite."""
    parts: list[str] = []
    for it in items:
        k = it.get("kind")
        if k in ("email", "ticket", "sent"):
            who = ", ".join(_label(p) for p in it.get("people", [])[:8]) or "no one else"
            span = local_minute(it.get("last_ts", ""), tz)
            head = "Sent reply" if k == "sent" else ("Ticket" if k == "ticket" else "Email")
            lines = [f"{head}: {it.get('subject', '')} ({span} local). With: {who}."]
            if it.get("tickets"):
                lines.append("Tickets: " + ", ".join(it["tickets"]) + ".")
            lo, lm = it.get("latest_other"), it.get("latest_mine")
            if k != "sent" and lo:
                lines.append(f"Latest from {_short(lo['from'])}: {_squash(lo['text'], 600)}")
            if lm:
                lines.append(f"My reply: {_squash(lm['text'], 500)}")
            parts.append("\n".join(lines))
        elif k == "slack":
            lo = it.get("latest_other") or {}
            parts.append(
                f"Slack ({it.get('subject', '')}): {_short(lo.get('from', ''))}: "
                f"{_squash(lo.get('text', ''), 600)}"
            )
        elif k == "snippet":
            parts.append(f'From "{it.get("subject", "")}": {_squash(it.get("text", ""), 800)}')
        elif k == "entity":
            parts.append(f"Re: {it.get('subject', '')}.")
    return ascii_fix("\n\n".join(p for p in parts if p).strip())


def template_task(items: list[dict[str, Any]]) -> str:
    main = next((i for i in items if i.get("kind") != "entity"), items[0] if items else {})
    who = ", ".join(_short_name(p) for p in (main.get("people") or [])[:3])
    tickets = " ".join(main.get("tickets") or [])
    base = f"Follow up: {main.get('subject', '').strip()}"
    if who:
        base += f" ({who})"
    if tickets and tickets not in base:
        base += f" {tickets}"
    return ascii_fix(base)[:300]


def _label(p: dict[str, Any]) -> str:
    return f"{p['name']} <{p['addr']}>" if p.get("name") else p["addr"]


def _short(frm: str) -> str:
    n, a = email.utils.parseaddr(frm or "")
    return n or a or frm


def _short_name(p: dict[str, Any]) -> str:
    return p.get("name") or p["addr"].split("@")[0]


def _squash(text: str, n: int) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[: n - 3].rstrip() + "..."


# ---------------------------------------------------------------- staging
class Stager:
    """Builds staged cards and guards each one against a double commit."""

    def __init__(self, ledger: LedgerLike, tz: str, me_netid: str = ""):
        self.ledger = ledger
        self.tz = tz
        self.me_netid = me_netid
        self._cards: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def stage(self, action: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        if action not in ("log", "task"):
            raise ValueError("action must be log or task")
        if not items:
            raise ValueError("nothing to stage: the bucket is empty")
        chips, unresolved = self._chips(items, action)
        last = max((i.get("last_ts") or "" for i in items), default="")
        card: dict[str, Any] = {
            "id": secrets.token_urlsafe(9),
            "action": action,
            "items": [
                {"kind": i["kind"], "key": i.get("key"), "subject": i.get("subject", "")}
                for i in items
            ],
            "date": local_minute(last, self.tz) if last else local_minute("", self.tz),
            "text": template_log(items, self.tz) if action == "log" else template_task(items),
            "priority": "MEDIUM",
            "chips": chips,
            "unresolved": unresolved,
            "state": "staged",
            "created": time.time(),
        }
        with self._lock:
            self._cards[card["id"]] = card
            # keep the table small: drop cards older than a day
            cutoff = time.time() - 86400
            for k in [k for k, v in self._cards.items() if v["created"] < cutoff]:
                self._cards.pop(k, None)
        return card

    def annotate(self, card_id: str, **kw: Any) -> None:
        with self._lock:
            if card_id in self._cards:
                self._cards[card_id].update(kw)

    def claim(self, card_id: str) -> dict[str, Any]:
        """Mark a card as committing. Raises if unknown or already used."""
        with self._lock:
            c = self._cards.get(card_id)
            if not c:
                raise LookupError("This card expired; stage it again")
            if c["state"] != "staged":
                raise PermissionError(f"This card was already {c['state']}")
            c["state"] = "committing"
            return c

    def finish(self, card_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            c = self._cards.get(card_id)
            if c:
                c["state"] = "committed" if result.get("id") else "failed"
                c["result"] = result

    def release(self, card_id: str) -> None:
        """A commit that wrote nothing (validation error) may be tried again."""
        with self._lock:
            c = self._cards.get(card_id)
            if c and c["state"] == "committing":
                c["state"] = "staged"

    # chips ----------------------------------------------------------------
    def _chips(self, items: list[dict[str, Any]], action: str) -> tuple[list[dict], list[dict]]:
        people: dict[str, dict[str, Any]] = {}
        for it in items:
            for p in it.get("people") or []:
                people.setdefault(p["addr"], p)
        plist = list(people.values())[:MAX_PEOPLE]
        # chips are keyed by ledger id, so two addresses of one person give one chip
        chips: list[dict[str, Any]] = []
        unresolved: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add(chip: dict[str, Any]) -> None:
            if chip["id"] and chip["id"] not in seen:
                seen.add(chip["id"])
                chips.append(chip)

        ctxs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        if self.ledger.enabled and plist:
            with ThreadPoolExecutor(max_workers=4) as ex:
                futs = [(p, ex.submit(self._safe_context, p)) for p in plist]
                ctxs = [(p, f.result()) for p, f in futs]
        for p, c in ctxs:
            if not c.get("id"):
                unresolved.append({"addr": p["addr"], "name": p.get("name", "")})
                continue
            if c.get("netid") == self.me_netid:
                continue
            role = "sender" if "from" in p.get("roles", []) else "recipient"
            add(
                {
                    "id": c["id"],
                    "name": c["name"],
                    "kind": "person",
                    "source": role,
                    "checked": True,
                    "via": "log",
                }
            )
        # labs and projects from each person's graph (one hop)
        for _p, c in ctxs:
            if not c.get("id") or c.get("netid") == self.me_netid:
                continue
            labs = c.get("lab_refs") or []
            for lab in labs:
                pi = lab.get("edge") == "PI_OF"
                add(
                    {
                        "id": lab["id"],
                        "name": lab["name"],
                        "kind": "lab",
                        "source": f"{'PI of' if pi else 'member of'} ({c['name']})",
                        "checked": pi or len(labs) == 1,
                        "via": "log",
                        "derived": True,
                    }
                )
            for pr in (c.get("project_refs") or [])[:4]:
                add(
                    {
                        "id": pr["id"],
                        "name": pr["name"],
                        "kind": "project",
                        "source": f"graph ({c['name']})",
                        "checked": False,
                        "via": "ref",
                        "derived": True,
                    }
                )
        # entities dropped in the bucket
        for it in items:
            if it.get("kind") == "entity":
                add(
                    {
                        "id": it["key"],
                        "name": it.get("subject", ""),
                        "kind": (it.get("entity_type") or "entity").lower(),
                        "source": "bucket",
                        "checked": True,
                        "via": "log" if it.get("entity_type") in ("Researcher", "Lab") else "ref",
                    }
                )
        # GCP ids and ticket numbers found in the text, confirmed in the ledger
        if self.ledger.enabled:
            for gid in dict.fromkeys(g for it in items for g in it.get("gcp") or []):
                for r in self._safe_search(gid):
                    if r.get("type") == "GCPProject" and gid in (r.get("name") or "").lower():
                        add(
                            {
                                "id": r["id"],
                                "name": r["name"],
                                "kind": "project",
                                "source": "in text",
                                "checked": True,
                                "via": "ref",
                            }
                        )
                        break
            for num in dict.fromkeys(t for it in items for t in it.get("tickets") or []):
                for r in self._safe_search(num):
                    if r.get("type") not in ("Interaction", "Researcher") and num in (
                        r.get("name") or ""
                    ):
                        add(
                            {
                                "id": r["id"],
                                "name": r["name"],
                                "kind": (r.get("type") or "entity").lower(),
                                "source": f"{num} in text",
                                "checked": True,
                                "via": "ref",
                            }
                        )
        if action == "task":
            for ch in chips:  # tasks reference everything; no PARTICIPATED_IN
                ch["via"] = "ref"
        return chips, unresolved

    def _safe_context(self, p: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.ledger.context(p["addr"], p.get("name", "")) or {}
        except Exception:  # noqa: BLE001 - an unreachable ledger leaves the chip off
            return {}

    def _safe_search(self, term: str) -> list[dict[str, Any]]:
        try:
            return self.ledger.search(term)
        except Exception:  # noqa: BLE001
            return []
