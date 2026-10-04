"""Life: the Personal workspace's Ledger place, built from the notes vault (SPEC 8.11, v1.13).

Work's Ledger is Nexus (people, labs, projects, tasks). Personal's is the operator's own
life, read from the vault through vault-mcp and arranged by area (home, animals, garden,
vehicles, family, spirit, money, fun by default; the operator's own list in a private
`life.toml`).

Nothing here writes. Each area has key notes (shown as cards; a missing note is
skipped, never an error), folders whose recent notes and open tasks belong to it, and
words that file a task under it. The page reads:

- `vault_tasks` (open tasks, filed into areas by their note's folder or by words),
- `vault_recent` per area folder (what changed lately),
- `vault_day` for today (daily note, check-ins, journal),
- `vault_read` for a note the operator opens (the existing reader).

Nothing is cached beyond the vault client's own 60 s cache, so the page is never more
than a minute behind Obsidian. Vault text is data: the page escapes all of it.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from ultra.vault import Vault, VaultError


@dataclass(frozen=True)
class Area:
    key: str
    name: str
    blurb: str
    notes: tuple[tuple[str, str], ...] = ()  # (path without .md, label)
    folders: tuple[str, ...] = ()  # recent notes and tasks from these folders
    words: tuple[str, ...] = ()  # task text starting with or containing these words
    journal: tuple[str, ...] = ()  # Journal/<Topic> folders that belong here
    log_topic: str = ""  # default Journal topic for "Log" from this area
    icon: str = ""  # an emoji for the tile (optional; life.toml)


# Generic defaults (a public repo: nothing personal here). The operator's own areas,
# key notes and folders live in a private file, `life.toml` next to the workspace's
# config (see `load_areas`); this list is used only when that file is missing.
DEFAULT_AREAS: tuple[Area, ...] = (
    Area(
        "home",
        "Home",
        "The house, the land and everything that keeps them running.",
        folders=("Journal/Homestead", "Journal/Maintenance"),
        words=("house", "roof", "furnace", "well", "mower", "yard", "repair"),
        journal=("Homestead", "Maintenance"),
        log_topic="Homestead",
    ),
    Area(
        "animals",
        "Animals",
        "Pets and livestock.",
        words=("dog", "cat", "hen", "hens", "flock", "chicken", "coop", "feed", "vet"),
    ),
    Area(
        "garden",
        "Garden",
        "Beds, pots, harvests and what to plant next.",
        folders=("Journal/Garden",),
        words=("garden", "bed", "seed", "plant", "harvest", "soil", "garlic"),
        journal=("Garden",),
        log_topic="Garden",
    ),
    Area(
        "vehicles",
        "Vehicles",
        "Cars, trucks, campers and their upkeep.",
        folders=("Journal/Travel",),
        words=("car", "truck", "camper", "tire", "oil", "inspection", "registration"),
        journal=("Travel",),
        log_topic="Maintenance",
    ),
    Area(
        "family",
        "Family",
        "The household, friends and the people in your life.",
        folders=("Journal/Health",),
        words=("family", "birthday", "doctor", "health", "school"),
        journal=("Health",),
        log_topic="Health",
    ),
    Area(
        "spirit",
        "Spirit",
        "Practice, reflection and gatherings.",
        words=("ritual", "meditation", "retreat", "gathering"),
    ),
    Area(
        "money",
        "Money",
        "Budget, bills, receipts and shopping.",
        words=("buy", "pay", "bill", "budget", "tax", "receipt", "refund", "order"),
    ),
    Area(
        "fun",
        "Fun",
        "Hobbies, trips, books and games.",
        words=("kayak", "hike", "camping", "book", "reading", "game", "festival", "trip"),
    ),
)


def load_areas(path: Any) -> tuple[Area, ...]:
    """Areas from a private TOML file:

        [[area]]
        key = "home"            # a-z, 2-20 letters
        name = "Home"
        blurb = "..."
        notes = [["01 - Hubs/Home_MOC", "Home hub"], ...]
        folders = ["Assets/House", "Journal/Homestead"]
        words = ["coop", "furnace"]
        log_topic = "Homestead"

    Order matters: the first area whose folder or word matches a task gets it. A bad
    file falls back to the defaults (and says so in the log) rather than breaking."""
    import tomllib
    from pathlib import Path

    p = Path(path) if path else None
    if p is None or not p.is_file():
        return DEFAULT_AREAS
    try:
        data = tomllib.loads(p.read_text())
        out = []
        for a in data.get("area") or []:
            key = str(a["key"])
            if not re.fullmatch(r"[a-z]{2,20}", key):
                raise ValueError(f"bad area key {key!r}")
            out.append(
                Area(
                    key,
                    str(a.get("name") or key.title())[:40],
                    str(a.get("blurb") or "")[:200],
                    notes=tuple((str(n[0]), str(n[1])[:60]) for n in a.get("notes") or []),
                    folders=tuple(str(f) for f in a.get("folders") or []),
                    words=tuple(str(w).lower() for w in a.get("words") or []),
                    log_topic=str(a.get("log_topic") or ""),
                    icon=str(a.get("icon") or "")[:4],
                )
            )
        if not out or len({a.key for a in out}) != len(out):
            raise ValueError("no areas, or two with the same key")
        return tuple(out)
    except (OSError, ValueError, KeyError, TypeError, IndexError, tomllib.TOMLDecodeError) as e:
        import logging

        logging.getLogger("ultra").warning("life.toml ignored (%s); using default areas", e)
        return DEFAULT_AREAS


AREAS = DEFAULT_AREAS  # the defaults; a Life instance carries its own (life.toml)
DUE_RE = re.compile(r"(?:\U0001F4C5|\bdue:?)\s*(\d{4}-\d{2}-\d{2})")
# checkbox lines in daily notes are schedules, not open todos; life.toml can add more
# notes to skip (`skip_tasks = [...]`, e.g. a daily chore checklist)
SKIP_TASK_NOTES: tuple[str, ...] = ("Daily Notes/",)
SOON = 14


def _norm(path: str) -> str:
    return path[:-3] if path.endswith(".md") else path


def area_of_task(t: dict[str, Any], areas: tuple[Area, ...] = DEFAULT_AREAS) -> str:
    """The first area whose folder holds the task's note, else whose word leads or
    appears in its text. Order matters: the first matching area wins."""
    path = str(t.get("path", ""))
    for a in areas:
        if any(path == f or path.startswith(f + "/") for f in a.folders):
            return a.key
    text = str(t.get("text", "")).lower()
    head = text.split(":", 1)[0] if ":" in text[:30] else ""
    for a in areas:  # a "Coop:" or "Garlic:" prefix is the clearest signal
        if head and any(head.strip() == w or head.strip().startswith(w) for w in a.words):
            return a.key
    for a in areas:
        if any(re.search(rf"\b{re.escape(w)}\b", text) for w in a.words):
            return a.key
    return "other"


@dataclass
class Life:
    vault: Vault
    today_fn: Any = None
    areas: tuple[Area, ...] = DEFAULT_AREAS
    skip: tuple[str, ...] = SKIP_TASK_NOTES
    _notes: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_config(cls, vault: Vault, cfg: Any) -> Life:
        """life.toml next to the workspace config (private), else the defaults."""
        import tomllib

        path = cfg.config_home / "life.toml"
        skip: tuple[str, ...] = SKIP_TASK_NOTES
        if path.is_file():
            try:
                extra = tomllib.loads(path.read_text()).get("skip_tasks") or []
                skip = SKIP_TASK_NOTES + tuple(str(x) for x in extra)
            except (OSError, tomllib.TOMLDecodeError):
                pass
        return cls(vault, areas=load_areas(path), skip=skip)

    @property
    def keys(self) -> set[str]:
        return {a.key for a in self.areas}

    def _today(self) -> dt.date:
        return self.today_fn() if self.today_fn else self.vault.today_fn()

    def _tasks(self) -> list[dict[str, Any]]:
        res = self.vault._call("vault_tasks", {})
        rows = res.get("results", []) if isinstance(res, dict) else []
        today = self._today()
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            path = str(r.get("path", ""))
            if path.startswith(self.skip) or self.vault._hidden(path):
                continue
            text = str(r.get("text", ""))
            m = DUE_RE.search(text)
            due = m.group(1) if m else ""
            days = None
            if due:
                try:
                    days = (dt.date.fromisoformat(due) - today).days
                except ValueError:
                    due = ""
            out.append(
                {
                    "text": plain(DUE_RE.sub("", text))[:240],
                    "due": due,
                    "days": days,
                    "path": path,
                    "line": r.get("line"),
                    "section": str(r.get("section") or "")[:80],
                    "area": area_of_task(r, self.areas),
                }
            )
        out.sort(key=lambda x: (x["due"] or "9999", x["path"], x["line"] or 0))
        return out

    def _recent(self, folder: str, limit: int) -> list[dict[str, Any]]:
        try:
            res = self.vault._call("vault_recent", {"folder": folder, "limit": limit})
        except VaultError:
            return []  # a folder that is not there yet
        rows = res.get("results", []) if isinstance(res, dict) else []
        out = []
        for r in rows:
            if not isinstance(r, dict) or not r.get("path") or self.vault._hidden(str(r["path"])):
                continue
            path = str(r["path"])
            m = re.search(r"(\d{4}-\d{2}-\d{2})", path.rsplit("/", 1)[-1])
            out.append(
                {
                    "path": path,
                    "title": _pretty(path),
                    "modified": r.get("modified"),
                    # the note's own date (from its name) when it has one; file times
                    # move with every git checkout or reorganization
                    "date": m.group(1) if m else "",
                }
            )
        return out

    def _exists(self, path: str) -> bool:
        if path not in self._notes:
            try:
                n = self.vault._call("vault_read", {"path": path}, ttl=600)
                self._notes[path] = isinstance(n, dict) and bool(n.get("path"))
            except VaultError:
                self._notes[path] = False
        return self._notes[path]

    def overview(self) -> dict[str, Any]:
        """The Life home: every area with its counts, today's line, and what is due."""
        tasks = self._tasks()
        areas = []
        for a in self.areas:
            mine = [t for t in tasks if t["area"] == a.key]
            areas.append(
                {
                    "key": a.key,
                    "name": a.name,
                    "blurb": a.blurb,
                    "icon": a.icon,
                    "open": len(mine),
                    "overdue": sum(1 for t in mine if t["days"] is not None and t["days"] < 0),
                    "soon": sum(
                        1 for t in mine if t["days"] is not None and 0 <= t["days"] <= SOON
                    ),
                    "next": next((t for t in mine if t["due"]), None),
                }
            )
        other = [t for t in tasks if t["area"] == "other"]
        due = [t for t in tasks if t["days"] is not None and t["days"] <= SOON]
        today = self._today().isoformat()
        try:
            day = self.vault._call("vault_day", {"date": today})
        except VaultError:
            day = {}
        log_lines: list[str] = []
        daily = day.get("daily") if isinstance(day, dict) else None
        if isinstance(daily, dict):
            log_lines = _log_section(str(daily.get("contents", "")))
        recent = self._recent("Journal", 30) + self._recent("Daily Notes/Check-ins", 30)
        recent.sort(
            key=lambda r: (str(r.get("date") or ""), str(r.get("modified") or "")), reverse=True
        )
        return {
            "today": today,
            "areas": areas,
            "due": due[:20],
            "undated_other": len([t for t in other if not t["due"]]),
            "day": {
                "daily": daily.get("path") if isinstance(daily, dict) else "",
                "log": log_lines[-8:],
                "checkins": (day.get("checkins") or []) if isinstance(day, dict) else [],
                "captains_log": (day.get("captains_log") or "") if isinstance(day, dict) else "",
                "journal": (day.get("logs") or []) if isinstance(day, dict) else [],
            },
            "recent": recent[:8],
            "vault": self.vault.name,
            "writes": self.vault.writes,
        }

    def area(self, key: str) -> dict[str, Any]:
        a = next((x for x in self.areas if x.key == key), None)
        if a is None:
            raise VaultError(404, "no such area")
        tasks = [t for t in self._tasks() if t["area"] == key]
        notes = [{"path": p, "label": label} for p, label in a.notes if self._exists(p)]
        seen = {n["path"] for n in notes}
        recent: list[dict[str, Any]] = []
        for f in a.folders:
            recent += self._recent(f, 8)
        recent = [r for r in recent if _norm(r["path"]) not in seen]
        recent.sort(
            key=lambda r: (str(r.get("date") or ""), str(r.get("modified") or "")), reverse=True
        )
        uniq: dict[str, dict[str, Any]] = {}
        for r in recent:
            uniq.setdefault(r["path"], r)
        return {
            "key": a.key,
            "name": a.name,
            "blurb": a.blurb,
            "icon": a.icon,
            "notes": notes,
            "tasks": tasks,
            "recent": list(uniq.values())[:12],
            "log_topic": a.log_topic,
            "search_words": list(a.words[:6]),
            "writes": self.vault.writes,
        }


WIKI_RE = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")


def plain(text: str) -> str:
    """Task text for display: [[Note|label]] -> label, [[path/Note_Name]] -> Note Name,
    markdown emphasis and empty brackets dropped."""

    def wl(m: re.Match[str]) -> str:
        return m.group(2) or m.group(1).rsplit("/", 1)[-1].replace("_", " ")

    t = re.sub(r"\(\s*\[\[[^\]]+\]\]\s*\)", "", text)  # "(see [[Note]])" asides go
    t = WIKI_RE.sub(wl, t)
    t = re.sub(r"(\*\*|__|\*|`)", "", t)
    t = re.sub(r"\(\s*\)", "", t)
    return " ".join(t.split()).rstrip(" ,;")


def _pretty(title: str) -> str:
    """'2026-10-02_Checkin_Evening' -> 'Evening check-in'; dates and underscores go."""
    t = title.rsplit("/", 1)[-1]
    t = t[:-3] if t.endswith(".md") else t
    t = re.sub(r"^\d{4}-\d{2}-\d{2}_", "", t)
    m = re.match(r"^Checkin(?:_(.+?))?(?:_(\d+))?$", t)
    if m:
        part = (m.group(1) or "").replace("_", " ").replace("Checkin", "").strip()
        return f"{part} check-in".strip().capitalize() if part else "Check-in"
    if t.endswith("_Captains_Log") or t == "Captains_Log":
        return "Captain's Log"
    return t.replace("_", " ")[:120]


def _log_section(text: str) -> list[str]:
    """The lines under '## Log' in a daily note (the quick entries)."""
    out: list[str] = []
    on = False
    for line in text.splitlines():
        if line.startswith("## "):
            on = line.strip().lower() == "## log"
            continue
        if on and line.strip():
            out.append(line.strip()[:300])
    return out


class LifeApi:
    """GET /api/life (overview) and /api/life/area/<key>."""

    def __init__(self, life: Life):
        self.life = life

    def register(self, api: Any) -> None:
        api.add("GET", r"/api/life", self.r_overview)
        api.add("GET", r"/api/life/area/([a-z]{2,20})", self.r_area)

    def _err(self, e: VaultError) -> Exception:
        from ultra.server import ApiError

        return ApiError(e.status, str(e))

    def r_overview(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        if not self.life.vault.enabled:
            return {"enabled": False}
        try:
            return {"enabled": True, **self.life.overview()}
        except VaultError as e:
            return {"enabled": True, "error": str(e)[:200]}

    def r_area(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        if m.group(1) not in self.life.keys:
            raise self._err(VaultError(404, "no such area"))
        try:
            return self.life.area(m.group(1))
        except VaultError as e:
            raise self._err(e) from e
