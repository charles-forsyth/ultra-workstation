"""Deterministic rules (SPEC 11): court state, tickets, noise, done signals.

No model calls here. Everything is regex and set logic so it is fast, testable and
explainable (every result carries a reason).
"""

from __future__ import annotations

import email.utils
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ultra.config import Config

DEFAULT_TICKET_RX = [r"RITM\d{7}", r"INC\d{7}", r"SCTASK\d{7}", r"REQ\d{7}"]
DEFAULT_NOISE = [
    r"no-?reply",
    r"do-?not-?reply",
    r"newsletter",
    r"notification",
    r"mailer-daemon",
    r"calendar-notification",
]
DEFAULT_DONE = [
    r"\bis ready\b",
    r"\bare ready\b",
    r"\bcompleted?\b",
    r"\ball set\b",
    r"\bis done\b",
    r"\bfinished\b",
    r"\bdeployed\b",
    r"\bhas been resolved\b",
    r"\byou can now\b",
]
TICKET_EVENTS = [
    ("assigned to you", r"assigned to you\b"),
    ("assigned to your group", r"assigned to your group"),
    ("comments added", r"comments? (?:have been |has been )?added"),
    ("resolved", r"\bresolved\b"),
    ("closed", r"\bclosed\b"),
    ("approval requested", r"approval (?:is )?(?:requested|required|needed)"),
]


def _compile(patterns: list[str]) -> re.Pattern[str]:
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.I)


@dataclass
class Rules:
    me: set[str]
    vip: set[str] = field(default_factory=set)
    noise: re.Pattern[str] = field(default_factory=lambda: _compile(DEFAULT_NOISE))
    ticket_sender: re.Pattern[str] | None = None
    ticket_number: re.Pattern[str] = field(default_factory=lambda: _compile(DEFAULT_TICKET_RX))
    done: re.Pattern[str] = field(default_factory=lambda: _compile(DEFAULT_DONE))
    amber_days: int = 3
    red_days: int = 5

    @classmethod
    def from_config(cls, cfg: Config, vip: set[str] | None = None) -> Rules:
        tickets = cfg.section("tickets")
        court = cfg.section("court")
        mail = cfg.section("mail")
        sender = tickets.get("sender_patterns") or []
        return cls(
            me=cfg.my_addresses,
            vip={v.lower() for v in (vip or set())},
            noise=_compile(list(mail.get("noise_patterns") or []) + DEFAULT_NOISE),
            ticket_sender=_compile(sender) if sender else None,
            ticket_number=_compile(tickets.get("number_patterns") or DEFAULT_TICKET_RX),
            done=_compile(court.get("done_signal_patterns") or DEFAULT_DONE),
            amber_days=int(court.get("waiting_amber_days", 3)),
            red_days=int(court.get("waiting_red_days", 5)),
        )

    # ---------------------------------------------------------------- helpers
    def is_me(self, addr: str) -> bool:
        return addr.lower() in self.me

    def is_noise(self, addr: str, labels: list[str] | None = None) -> bool:
        if addr.lower() in self.vip:
            return False
        if labels and set(labels) & {
            "CATEGORY_PROMOTIONS",
            "CATEGORY_SOCIAL",
            "CATEGORY_FORUMS",
        }:
            return True
        return bool(self.noise.search(addr))

    def is_ticket_sender(self, addr: str) -> bool:
        return bool(self.ticket_sender and self.ticket_sender.search(addr))

    def ticket_numbers(self, text: str) -> list[str]:
        return sorted({m.group(0).upper() for m in self.ticket_number.finditer(text)})


def parse_addr(header: str) -> tuple[str, str]:
    name, addr = email.utils.parseaddr(header or "")
    return name.strip().strip('"'), addr.lower()


def _to_someone_else(rules: Rules, messages: list[dict[str, Any]]) -> bool:
    """True if any message in the thread was addressed to someone but me."""
    for m in messages:
        for field_ in ("to", "cc"):
            for _, a in email.utils.getaddresses([m.get(field_, "") or ""]):
                if a and not rules.is_me(a):
                    return True
    return False


def ticket_events(text: str) -> list[str]:
    return [name for name, rx in TICKET_EVENTS if re.search(rx, text, re.I)]


def days_since(ms: int, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    then = datetime.fromtimestamp(ms / 1000, UTC)
    return max(0, (now - then).days)


RSVP_RX = re.compile(
    r"^(Accepted|Declined|Tentatively accepted|Tentative|Updated invitation|"
    r"Invitation|Canceled event|Cancelled event)( with note)?:",
    re.I,
)


def is_rsvp(subject: str) -> bool:
    """Calendar invitation traffic: handled on the calendar, not in the stream."""
    return bool(RSVP_RX.match(subject.strip()))


def classify_thread(rules: Rules, messages: list[dict[str, Any]], in_inbox: bool) -> dict[str, Any]:
    """Court state for one email thread.

    messages: oldest first, each {from_addr, from_name, ts_ms, text, labels}.
    Returns {court, reason, badges, waiting_days, last_other, ready}.
    """
    if not messages:
        return {"court": "NONE", "reason": "empty", "badges": [], "waiting_days": 0}
    last = messages[-1]
    mine = [m for m in messages if rules.is_me(m["from_addr"])]
    others = [m for m in messages if not rules.is_me(m["from_addr"])]
    last_mine_ts = mine[-1]["ts_ms"] if mine else 0
    badges: list[str] = []
    other_addrs = {m["from_addr"] for m in others}
    if other_addrs & rules.vip:
        badges.append("VIP")

    # A peer saying "done/ready" after my last message moves the ball to me.
    ready = any(m["ts_ms"] > last_mine_ts and rules.done.search(m.get("text", "")) for m in others)
    if ready:
        badges.append("READY")

    if rules.is_me(last["from_addr"]):
        if not others and not _to_someone_else(rules, messages):
            return {
                "court": "NONE",
                "reason": "note to yourself",
                "badges": badges,
                "waiting_days": 0,
                "ready": False,
            }
        wd = days_since(last["ts_ms"])
        if wd >= rules.red_days:
            badges.append("OVERDUE")
        elif wd >= rules.amber_days:
            badges.append("SLOW")
        return {
            "court": "WAITING",
            "reason": "you sent the last message",
            "badges": badges,
            "waiting_days": wd,
            "ready": False,
        }
    if not in_inbox:
        return {
            "court": "NONE",
            "reason": "archived",
            "badges": badges,
            "waiting_days": 0,
            "ready": ready,
        }
    noise = rules.is_noise(last["from_addr"], last.get("labels"))
    if noise and not ready:
        return {
            "court": "LOW",
            "reason": "automated or bulk sender",
            "badges": badges,
            "waiting_days": 0,
            "ready": False,
        }
    return {
        "court": "MINE",
        "reason": "ready signal from a peer" if ready else "last message is to you",
        "badges": badges,
        "waiting_days": days_since(last["ts_ms"]),
        "ready": ready,
    }
