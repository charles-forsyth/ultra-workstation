"""Outgoing text rules (SPEC 10). Errors block approval; warnings don't.

Generic rules ship here. Operator rules come from style.toml ([ascii] level,
[[forbid]] pattern/message/level, [signature] text). Nothing operator-specific lives
in this file.
"""

from __future__ import annotations

import email.utils
import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ultra.config import config_dir

FIXES = {
    "\u2014": "-",  # em dash
    "\u2013": "-",  # en dash
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u2026": "...",
    "\u00a0": " ",
    "\u2022": "-",
    "\u2192": "->",
}


@dataclass
class Issue:
    level: str  # error | warning
    code: str
    message: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def load_style(path: Path | None = None) -> dict[str, Any]:
    path = path or config_dir() / "style.toml"
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def ascii_fix(text: str) -> str:
    """Replace common typographic characters with ASCII equivalents."""
    for k, v in FIXES.items():
        text = text.replace(k, v)
    return text


def _addrs(field: str) -> list[str]:
    return [a.lower() for _, a in email.utils.getaddresses([field or ""]) if a]


def lint(
    version: dict[str, Any],
    style: dict[str, Any],
    *,
    thread_participants: set[str] | None = None,
    me: set[str] | None = None,
    org_domain: str = "",
    kind: str = "reply",
    attachments: int = 0,
) -> list[Issue]:
    issues: list[Issue] = []
    body = version.get("body") or ""
    subject = version.get("subject") or ""
    to = _addrs(version.get("to_addrs") or "")
    cc = _addrs(version.get("cc") or "")
    bcc = _addrs(version.get("bcc") or "")

    if not to:
        issues.append(Issue("error", "no_to", "Add at least one To recipient."))
    if not body.strip():
        issues.append(Issue("error", "empty", "The message is empty."))
    if not subject.strip():
        issues.append(Issue("warning", "no_subject", "No subject."))

    level = str((style.get("ascii") or {}).get("level", "error"))
    bad = sorted({c for c in body + subject if ord(c) > 126})
    if bad:
        shown = " ".join(repr(c) for c in bad[:8])
        issues.append(Issue(level, "non_ascii", f"Non-ASCII characters: {shown}. Use Fix ASCII."))

    for rule in style.get("forbid") or []:
        pat = rule.get("pattern")
        if pat and re.search(pat, body + "\n" + subject):
            issues.append(
                Issue(
                    str(rule.get("level", "warning")),
                    "style",
                    str(rule.get("message") or f"Matches forbidden pattern {pat}"),
                )
            )

    if kind == "reply_all" and thread_participants:
        present = set(to) | set(cc) | set(bcc) | (me or set())
        dropped = sorted(thread_participants - present)
        if dropped:
            issues.append(
                Issue(
                    "warning",
                    "dropped",
                    "Reply-all no longer includes: " + ", ".join(dropped),
                )
            )
    if org_domain:
        ext = sorted(a for a in set(to) | set(cc) | set(bcc) if not a.endswith("@" + org_domain))
        if ext:
            issues.append(Issue("warning", "external", "Outside your domain: " + ", ".join(ext)))
    if re.search(r"\b(attached|attachment|see the file)\b", body, re.I) and not attachments:
        issues.append(
            Issue("warning", "no_attachment", "Mentions an attachment but none is attached.")
        )
    return issues


def blocking(issues: list[Issue]) -> bool:
    return any(i.level == "error" for i in issues)
