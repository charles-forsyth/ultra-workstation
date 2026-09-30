"""Draft text tools (SPEC 9.7): cut flagged sentences, compare versions, Tidy.

Everything here is deterministic: no model call can change a draft through this
module. Tidy only fixes form (characters, spacing, greeting, signature, reference
line); it never adds or removes a fact.
"""

from __future__ import annotations

import difflib
import re
from typing import Any

from ultra.lint import ascii_fix


# ---------------------------------------------------------------- cut
def cut_sentences(body: str, texts: list[str]) -> str:
    """Remove each sentence in ``texts`` from ``body`` (exact, or with flexible
    whitespace), then tidy the blank space left behind."""
    out = body
    for raw in texts:
        t = " ".join((raw or "").split())
        if not t:
            continue
        i = out.find(t)
        if i >= 0:
            out = out[:i] + out[i + len(t) :]
            continue
        rx = r"\s+".join(re.escape(w) for w in t.split())
        out = re.sub(rx, "", out, count=1)
    return _squeeze(out)


def _squeeze(s: str) -> str:
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"(?m)^[ \t]+(?=\S)", lambda m: m.group(0) if len(m.group(0)) > 1 else "", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    s = re.sub(r"(?<=\S) {2,}(?=\S)", " ", s)
    return s.strip() + ("\n" if s.endswith("\n") else "")


# ---------------------------------------------------------------- compare
def _tokens(s: str) -> list[str]:
    """Words with their following whitespace, so joining the tokens rebuilds the text."""
    return re.findall(r"\S+\s*|\s+", s or "")


def word_diff(a: str, b: str) -> list[dict[str, str]]:
    """Word-level change list from a to b: [{op: same|add|del, text}]. Words are
    matched on their text alone (whitespace and line breaks do not count as a change);
    the output keeps each side's own whitespace so the text can be rebuilt."""
    ta, tb = _tokens(a), _tokens(b)
    ka = [t.strip() for t in ta]
    kb = [t.strip() for t in tb]
    sm = difflib.SequenceMatcher(a=ka, b=kb, autojunk=False)
    out: list[dict[str, str]] = []

    def push(op: str, text: str) -> None:
        if not text:
            return
        if out and out[-1]["op"] == op:
            out[-1]["text"] += text
        else:
            out.append({"op": op, "text": text})

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            push("same", "".join(tb[j1:j2]))
        else:
            push("del", "".join(ta[i1:i2]))
            push("add", "".join(tb[j1:j2]))
    return out


FIELD_LABELS = {
    "to_addrs": "To",
    "cc": "Cc",
    "bcc": "Bcc",
    "subject": "Subject",
    "from_addr": "From",
}


def compare(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """What changed between two versions: header fields, body words, and counts."""
    fields = []
    for k, label in FIELD_LABELS.items():
        a, b = (old.get(k) or "").strip(), (new.get(k) or "").strip()
        if a != b:
            fields.append({"field": label, "old": a, "new": b})
    ops = word_diff(old.get("body") or "", new.get("body") or "")
    added = sum(len(o["text"].split()) for o in ops if o["op"] == "add")
    removed = sum(len(o["text"].split()) for o in ops if o["op"] == "del")
    return {
        "from": old.get("version"),
        "to": new.get("version"),
        "fields": fields,
        "body": ops,
        "added_words": added,
        "removed_words": removed,
        "same": not fields and added == 0 and removed == 0,
    }


# ---------------------------------------------------------------- tidy
def tidy(
    body: str, signature: str = "", ref: str = "", ref_pattern: str = ""
) -> tuple[str, list[str]]:
    """Fix form only. Returns (text, list of what changed, in plain words)."""
    changes: list[str] = []
    t = body or ""

    fixed = ascii_fix(t)
    if fixed != t:
        changes.append("typographic characters to plain ASCII")
        t = fixed

    fixed = "\n".join(ln.rstrip() for ln in t.replace("\r\n", "\n").split("\n"))
    if fixed != t:
        changes.append("trailing spaces removed")
        t = fixed

    fixed = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", t)
    if fixed != t:
        changes.append("double spaces")
        t = fixed

    fixed = re.sub(r"\n{3,}", "\n\n", t)
    if fixed != t:
        changes.append("extra blank lines")
        t = fixed

    # a greeting written twice ("Hi Ben,\n\nHi Ben,")
    fixed = re.sub(
        r"\A(\s*)((?:hi|hello|dear|hey)\b[^\n]{0,60}\n)(\s*\n)?(?:\s*)\2",
        r"\1\2\3",
        t,
        flags=re.I,
    )
    if fixed != t:
        changes.append("repeated greeting")
        t = fixed

    # the signature written twice at the end
    if signature.strip():
        sig = re.escape(signature.strip())
        fixed = re.sub(
            rf"(?:\n\s*{sig}\s*)(\n\s*{sig}\s*)+(?=(\n|$))", f"\n\n{signature.strip()}", t
        )
        if fixed != t:
            changes.append("repeated signature")
            t = fixed

    # the ticket reference: exactly one, last
    if ref and ref_pattern:
        rx = re.compile(r"(?m)^[ \t>]*" + ref_pattern + r"[ \t]*$\n?")
        kept = rx.sub("", t).rstrip()
        fixed = f"{kept}\n\n{ref}\n" if kept else f"{ref}\n"
        if fixed.strip() != t.strip():
            changes.append(f"{ref} placed once, last")
        t = fixed

    t = re.sub(r"\n{3,}", "\n\n", t)
    t = t.strip("\n") + "\n" if t.strip() else ""
    if (body or "").strip() and t.strip() == (body or "").strip() and not changes:
        return body, []
    return t, changes
