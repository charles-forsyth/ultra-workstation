"""SPEC 1.0.1: section 14 lists every route, section 6 every table, 5.2 every source file."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src/ultra"
SPEC = (ROOT / "SPEC.md").read_text()


def section(start: str, end: str) -> str:
    return SPEC[SPEC.index(start) : SPEC.index(end)]


def routes() -> set[str]:
    out: set[str] = set()
    for p in SRC.glob("*.py"):
        if p.name == "demo.py":
            continue
        for m in re.finditer(
            r'(?:api|self)\.add\(\s*"(?:GET|POST|PUT|DELETE)",\s*r?"([^"]+)"', p.read_text()
        ):
            out.add(m.group(1))
    return out


def literal_parts(route: str) -> list[str]:
    """Split a route regex into its literal pieces; every (...) group, nested or not,
    and every [...] class with its quantifier is one parameter."""
    parts, cur, i = [], "", 0
    while i < len(route):
        ch = route[i]
        if ch in "([":
            close = ")" if ch == "(" else "]"
            depth = 0
            while i < len(route):
                if route[i] == "\\":
                    i += 2
                    continue
                if route[i] in "([":
                    depth += 1
                elif route[i] in ")]":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            i += 1
            while i < len(route) and route[i] in "+*?{":  # quantifier after the group
                if route[i] == "{":
                    i = route.index("}", i)
                i += 1
            parts.append(cur)
            cur = ""
            del close
            continue
        if ch == "\\":
            i += 1
            ch = route[i]
        cur += ch
        i += 1
    parts.append(cur)
    return parts


def spec_form(route: str) -> re.Pattern[str]:
    """A route pattern as the spec writes it: each regex group becomes <anything>."""
    return re.compile("`" + "<[^>`]+>".join(re.escape(x) for x in literal_parts(route)) + "`")


def test_route_count_matches_the_spec():
    n = len(routes())
    assert f"{n} route paths as of" in section("## 14. HTTP API", "## 15. CLI"), n


def test_every_route_is_in_section_14_by_full_path():
    s14 = section("## 14. HTTP API", "## 15. CLI")
    missing = [r for r in sorted(routes()) if not spec_form(r).search(s14)]
    assert not missing, f"routes not listed in SPEC section 14: {missing}"


def test_section_14_lists_no_route_that_does_not_exist():
    s14 = section("## 14. HTTP API", "## 15. CLI")
    forms = [spec_form(r) for r in routes()] + [re.compile(r"`/api/(health|session)`")]
    listed = re.findall(r"`/api/[^`]+`", s14)
    ghosts = [x for x in listed if not any(f.fullmatch(x) for f in forms)]
    assert not ghosts, f"section 14 lists paths with no route: {ghosts}"


def test_no_unbuilt_endpoints_listed():
    s14 = section("## 14. HTTP API", "## 15. CLI")
    for ghost in ("/api/events", "/api/jobs", "/api/calendar", "/api/mail/tidy"):
        assert ghost not in s14, ghost


def test_every_table_is_in_section_6(tmp_path):
    from ultra.compose import Composer
    from ultra.config import Config
    from ultra.invites import Invites, Rsvp
    from ultra.learn import EditLearner
    from ultra.mailx import Annotations, DraftFiles, SavedSearches
    from ultra.sources import HouseFacts
    from ultra.store import Store

    store = Store(tmp_path / "s.db")
    Composer(Config({}), store, {"ada@example.org"})
    Annotations(store)
    SavedSearches(store)
    DraftFiles(store, tmp_path / "att")
    Invites(store, None)
    Rsvp(store, None)
    EditLearner(store)
    HouseFacts(store)
    with sqlite3.connect(tmp_path / "s.db") as c:
        tables = {r[0] for r in c.execute("select name from sqlite_master where type='table'")}
    tables.discard("sqlite_sequence")
    s6 = section("## 6. Data model", "## 7. Screens")
    missing = [t for t in sorted(tables) if f"| `{t}` |" not in s6]
    assert not missing, f"tables not in SPEC section 6: {missing}"
    listed = set(re.findall(r"^\| `([a-z_]+)` \|", s6, re.M))
    assert listed <= tables, f"section 6 lists tables that do not exist: {sorted(listed - tables)}"


def test_every_source_file_is_in_the_code_map():
    cmap = section("### 5.2 Code map", "## 6. Data model")
    files = [p.name for p in SRC.glob("*.py") if p.name != "__init__.py"]
    files += [p.name for p in (SRC / "static").glob("*.js")]
    missing = [
        f
        for f in sorted(files)
        if f"`{f}`" not in cmap and f"`static/{f}`" not in cmap and f not in cmap
    ]
    assert not missing, f"files not in SPEC 5.2 code map: {missing}"
