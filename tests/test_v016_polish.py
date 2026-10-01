"""v0.16: one keyboard table; the help panel, the handlers and the README must agree."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/ultra/static"


def table() -> list[tuple[str, str]]:
    src = (STATIC / "keys.js").read_text()
    return re.findall(r'\["([^"]+)", "([^"]+)", "[^"]+", "(?:any|item)"\]', src)


def single_keys() -> set[str]:
    out: set[str] = set()
    for key, _ in table():
        for part in key.split(" / "):
            if len(part) == 1:
                out.add(part)
    return out


def test_every_handled_key_is_in_the_help_table():
    app = (STATIC / "app.js").read_text()
    body = app[app.index('document.addEventListener("keydown"') :]
    body = body[: body.index("const drawer")]
    handled = set(re.findall(r'e\.key === "(.)"', body))
    assert handled, "no handlers found"
    missing = handled - single_keys()
    assert not missing, f"keys handled but not documented in keys.js: {sorted(missing)}"


def test_every_documented_key_has_a_handler():
    app = (STATIC / "app.js").read_text() + (STATIC / "mailx.js").read_text()
    handled = set(re.findall(r'e\.key === "(.)"', app))
    extra = single_keys() - handled
    assert not extra, f"documented in keys.js but nothing handles: {sorted(extra)}"


def test_readme_lists_every_key():
    readme = (ROOT / "README.md").read_text()
    sec = readme[readme.index("## Keys") :]
    sec = sec[: sec.index("\n## ", 5)]
    for key, label in table():
        assert f"`{key}`" in sec, f"README Keys section is missing {key} ({label})"


def test_modifier_shortcuts_are_left_to_the_browser():
    app = (STATIC / "app.js").read_text()
    assert "if (e.ctrlKey || e.metaKey || e.altKey) return;" in app
    i_mod = app.index("if (e.ctrlKey || e.metaKey || e.altKey) return;")
    assert app.index('e.key.toLowerCase() === "k"') < i_mod  # Ctrl+K stays ours
    assert i_mod < app.index('if (e.key === "r")')  # Ctrl+R reloads, never replies


def test_help_panel_is_wired():
    html = (STATIC / "index.html").read_text()
    assert 'id="keyhelp"' in html and 'id="btn-help"' in html
    app = (STATIC / "app.js").read_text()
    assert 'if (e.key === "?") { e.preventDefault(); toggleHelp(); return; }' in app
    keys = (STATIC / "keys.js").read_text()
    assert "innerHTML" in keys and "esc(" in keys  # every label goes through esc()
