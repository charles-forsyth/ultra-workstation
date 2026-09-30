"""Learning from the operator's edits (SPEC 9.8).

After a send, the last AI version is compared with what was actually sent. Sentences
the operator deleted and phrases they replaced are recorded (locally, per send). When
the same kind of edit repeats, a suggestion appears: make it a style rule (a
[[forbid]] entry in style.toml) or a house fact. Nothing is applied without the
operator's click, and a dismissed suggestion does not come back.

No model is involved: the patterns are normalized phrases, counted.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import tomllib
from pathlib import Path
from typing import Any

from ultra.drafttools import word_diff

MIN_REPEATS = 3  # the same edit this many times (in different emails) -> a suggestion
MAX_EDITS = 400  # recorded sends kept

SCHEMA = """
CREATE TABLE IF NOT EXISTS edit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    draft_id INTEGER NOT NULL UNIQUE,
    ts REAL NOT NULL,
    removed TEXT NOT NULL,     -- JSON list of normalized removed phrases
    replaced TEXT NOT NULL,    -- JSON list of [old, new] normalized pairs
    added_words INTEGER NOT NULL,
    removed_words INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS edit_suggestions (
    key TEXT PRIMARY KEY,
    state TEXT NOT NULL,       -- open | accepted | dismissed
    updated REAL NOT NULL
);
"""

# Phrases shorter than this are too generic to learn from ("the", "Thanks,").
MIN_WORDS = 3


def _norm(s: str) -> str:
    s = re.sub(r"\s+", " ", s or "").strip().lower()
    return re.sub(r"[^a-z0-9' ]+", "", s).strip()


def _sentences(s: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[.!?])\s+|\n+", s or "") if x.strip()]


def edits_between(ai_body: str, sent_body: str) -> dict[str, Any]:
    """Removed sentences and replaced phrases from the AI text to the sent text."""
    ops = word_diff(ai_body, sent_body)
    removed: list[str] = []
    replaced: list[list[str]] = []
    sent_sents = {_norm(x) for x in _sentences(sent_body)}
    for s in _sentences(ai_body):
        n = _norm(s)
        if len(n.split()) >= MIN_WORDS and n not in sent_sents and n[:40] not in _norm(sent_body):
            removed.append(n[:200])
    removed_set = set(removed)
    for i, o in enumerate(ops):
        if o["op"] == "del" and i + 1 < len(ops) and ops[i + 1]["op"] == "add":
            # drop whole sentences already counted as removed, so a cut sentence
            # next to a small change still shows the small change
            left = [s for s in _sentences(o["text"]) if _norm(s) not in removed_set]
            a, b = _norm(" ".join(left)), _norm(ops[i + 1]["text"])
            if a and b and a != b and 1 <= len(a.split()) <= 6 and len(b.split()) <= 6:
                replaced.append([a, b])
    added = sum(len(o["text"].split()) for o in ops if o["op"] == "add")
    gone = sum(len(o["text"].split()) for o in ops if o["op"] == "del")
    return {
        "removed": removed[:30],
        "replaced": replaced[:30],
        "added_words": added,
        "removed_words": gone,
    }


class EditLearner:
    def __init__(self, store: Any, style_path: Path | None = None):
        self.store = store
        self.style_path = style_path
        with store._conn() as c:
            c.executescript(SCHEMA)

    def _db(self) -> Any:
        return self.store._conn()

    # ---------------------------------------------------------------- record
    def record(self, draft: dict[str, Any]) -> dict[str, Any] | None:
        """Record a sent draft: last AI version vs the sent (approved) version. Returns
        the edits, or None when there was no AI version (nothing to learn)."""
        vs = draft.get("versions") or []
        sent_v = draft.get("approved_version") or (vs[-1]["version"] if vs else None)
        sent = next((v for v in vs if v["version"] == sent_v), None)
        ai = [v for v in vs if v["author"] == "ai" and sent and v["version"] <= sent["version"]]
        if not sent or not ai:
            return None
        e = edits_between(ai[-1].get("body") or "", sent.get("body") or "")
        with self._db() as c:
            c.execute(
                "INSERT OR REPLACE INTO edit_log (draft_id, ts, removed, replaced, added_words, "
                "removed_words) VALUES (?,?,?,?,?,?)",
                (
                    int(draft["id"]),
                    time.time(),
                    json.dumps(e["removed"]),
                    json.dumps(e["replaced"]),
                    e["added_words"],
                    e["removed_words"],
                ),
            )
            c.execute(
                "DELETE FROM edit_log WHERE id NOT IN (SELECT id FROM edit_log ORDER BY id DESC "
                "LIMIT ?)",
                (MAX_EDITS,),
            )
        return e

    # ---------------------------------------------------------------- suggest
    def _states(self) -> dict[str, str]:
        return {k: s for k, s in self._db().execute("SELECT key, state FROM edit_suggestions")}

    def suggestions(self) -> list[dict[str, Any]]:
        rows = (
            self._db().execute("SELECT removed, replaced FROM edit_log ORDER BY id DESC").fetchall()
        )
        cut: dict[str, int] = {}
        swap: dict[tuple[str, str], int] = {}
        for removed, replaced in rows:
            for p in set(json.loads(removed)):
                cut[p] = cut.get(p, 0) + 1
            for a, b in {tuple(x) for x in json.loads(replaced)}:
                swap[(a, b)] = swap.get((a, b), 0) + 1
        # a removed sentence also counts toward shorter phrases it shares with others:
        # "happy to set up a call" cut in four different wordings is one habit
        grams: dict[str, set[str]] = {}
        for p in cut:
            w = p.split()
            for n in (4, 5, 6):
                for i in range(len(w) - n + 1):
                    grams.setdefault(" ".join(w[i : i + n]), set()).add(p)
        states = self._states()
        out: list[dict[str, Any]] = []
        seen_sents: set[str] = set()
        for g, sents in sorted(
            grams.items(), key=lambda x: (-sum(cut[s] for s in x[1]), -len(x[0]))
        ):
            n = sum(cut[s] for s in sents)
            if n < MIN_REPEATS or sents <= seen_sents:
                continue
            key = "cut:" + _key(g)
            if states.get(key) in ("accepted", "dismissed"):
                seen_sents |= sents
                continue
            seen_sents |= sents
            out.append(
                {
                    "key": key,
                    "kind": "cut",
                    "phrase": g,
                    "count": n,
                    "examples": sorted(sents)[:3],
                    "text": f'You cut sentences with "{g}" {n} times.',
                    "rule": {
                        "pattern": r"(?i)\b" + r"\s+".join(re.escape(x) for x in g.split()) + r"\b",
                        "message": f'You usually cut "{g}".',
                        "level": "warning",
                    },
                }
            )
        for (a, b), n in sorted(swap.items(), key=lambda x: -x[1]):
            if n < MIN_REPEATS:
                continue
            key = "swap:" + _key(a + "->" + b)
            if states.get(key) in ("accepted", "dismissed"):
                continue
            out.append(
                {
                    "key": key,
                    "kind": "swap",
                    "phrase": a,
                    "replacement": b,
                    "count": n,
                    "text": f'You changed "{a}" to "{b}" {n} times.',
                    "rule": {
                        "pattern": r"(?i)\b" + r"\s+".join(re.escape(x) for x in a.split()) + r"\b",
                        "message": f'You usually write "{b}" instead of "{a}".',
                        "level": "warning",
                    },
                }
            )
        return out[:12]

    def stats(self) -> dict[str, Any]:
        rows = self._db().execute("SELECT added_words, removed_words FROM edit_log").fetchall()
        return {
            "sends": len(rows),
            "untouched": sum(1 for a, r in rows if a == 0 and r == 0),
            "words_added": sum(a for a, _ in rows),
            "words_removed": sum(r for _, r in rows),
        }

    # ---------------------------------------------------------------- decide
    def dismiss(self, key: str) -> None:
        self._set(key, "dismissed")

    def accept_rule(self, key: str) -> dict[str, Any]:
        """Add the suggestion's rule to style.toml as a [[forbid]] warning (appended;
        the file is backed up first). The operator can edit or delete it there."""
        s = next((x for x in self.suggestions() if x["key"] == key), None)
        if not s:
            raise KeyError("no such suggestion")
        path = self.style_path
        if path is None:
            from ultra.config import config_dir

            path = config_dir() / "style.toml"
        old = path.read_text() if path.exists() else ""
        rule = s["rule"]
        block = (
            "\n[[forbid]]\n"
            f"pattern = {_toml_str(rule['pattern'])}\n"
            f"message = {_toml_str(rule['message'])}\n"
            f"level = {_toml_str(rule['level'])}\n"
            f"# learned from your edits, {time.strftime('%Y-%m-%d')}\n"
        )
        new = old.rstrip("\n") + "\n" + block if old else block.lstrip("\n")
        tomllib.loads(new)  # never write a file that would not parse
        if old:
            path.with_name(path.name + f".bak_{int(time.time())}").write_text(old)
        path.write_text(new)
        self._set(key, "accepted")
        return rule

    def _set(self, key: str, state: str) -> None:
        with self._db() as c:
            c.execute(
                "INSERT OR REPLACE INTO edit_suggestions (key, state, updated) VALUES (?,?,?)",
                (key, state, time.time()),
            )


def _key(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()[:16]


def _toml_str(s: str) -> str:
    """A TOML literal string when possible (regexes read as written), else basic."""
    if "'" not in s and "\n" not in s:
        return f"'{s}'"
    return json.dumps(s)
