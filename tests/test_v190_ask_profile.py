"""v1.9: Ask Hermes in a read-only profile that can look things up (SPEC 7.12).

With `[hermes] profile = "ultra-ask"`, Ask runs `hermes -p ultra-ask chat ... -t
session_search,nexus,ursa`. Ultra re-checks the profile first: every tool in the two
servers' `tools.include` must be a read tool Ultra knows, and no other MCP server may be
enabled. Any failure means Ask runs exactly as before. The fake binary below answers
`config get` from a JSON file, so each test can break the profile a different way.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ultra.config import Config
from ultra.hermes import PROFILE_READ_TOOLS, Hermes
from ultra.store import Store

FAKE = r"""
import json, os, sys
a = sys.argv[1:]
with open(os.environ["HZ_LOG"], "a") as f:
    f.write(json.dumps(a) + "\n")
if "config" in a and "get" in a:
    key = a[a.index("get") + 1]
    cfg = json.load(open(os.environ["HZ_CFG"]))
    node = cfg
    for part in key.split("."):
        node = node.get(part) if isinstance(node, dict) else None
    print(json.dumps(node))
    sys.exit(0)
sid = "20261003_120000_abc123"
print(json.dumps({"type": "result", "session_id": sid, "exit_code": 0, "text": "ok"}))
"""

GOOD = {
    "mcp_servers": {
        "google_workspace": {"enabled": False, "command": "x"},
        "nexus": {"tools": {"include": sorted(PROFILE_READ_TOOLS["nexus"])}, "trust": "full"},
        "ursa": {"tools": {"include": sorted(PROFILE_READ_TOOLS["ursa"])}, "trust": "full"},
    }
}


@pytest.fixture
def setup(tmp_path: Path, monkeypatch):
    b = tmp_path / "hermes"
    b.write_text("#!/usr/bin/env python3\n" + FAKE)
    b.chmod(0o755)
    log, cfgf = tmp_path / "log.jsonl", tmp_path / "cfg.json"
    monkeypatch.setenv("HZ_LOG", str(log))
    monkeypatch.setenv("HZ_CFG", str(cfgf))

    def make(cfg: dict[str, Any], profile: str = "ultra-ask") -> tuple[Hermes, Path]:
        cfgf.write_text(json.dumps(cfg))
        h = Hermes(
            Config({"hermes": {"binary": str(b), "profile": profile}}), Store(tmp_path / "s.db")
        )
        return h, log

    return make


def chat_argv(log: Path) -> list[str]:
    return [json.loads(x) for x in log.read_text().splitlines() if '"chat"' in x][-1]


def test_a_good_profile_adds_the_servers(setup):
    h, log = setup(GOOD)
    h.ask("who is Ada?")
    a = chat_argv(log)
    assert a[:3] == ["-p", "ultra-ask", "chat"]
    assert a[a.index("-t") + 1] == "session_search,nexus,ursa"
    assert h.check_profile()["ok"] is True


def _broken(**changes: Any) -> dict[str, Any]:
    cfg = json.loads(json.dumps(GOOD))
    for path, value in changes.items():
        node = cfg["mcp_servers"]
        *head, last = path.split("__")
        for p in head:
            node = node.setdefault(p, {})
        node[last] = value
    return cfg


@pytest.mark.parametrize(
    ("cfg", "why"),
    [
        (
            _broken(nexus__tools={"include": [*sorted(PROFILE_READ_TOOLS["nexus"]), "nexus_log"]}),
            "nexus_log",
        ),
        (_broken(ursa__tools={"include": ["job_show_any", "job_cancel"]}), "job_cancel"),
        (_broken(ursa__tools={"include": ["job_show_any", "files_read"]}), "files_read"),
        (_broken(nexus__tools={}), "tools.include"),  # no include = every tool
        (_broken(nexus__tools={"include": []}), "tools.include"),
        (_broken(google_workspace={"enabled": True, "command": "x"}), "google_workspace"),
        (_broken(slack={"command": "y"}), "slack"),  # enabled by default
    ],
)
def test_a_broken_profile_falls_back_to_the_plain_ask(setup, cfg, why):
    h, log = setup(cfg)
    h.ask("who is Ada?")
    a = chat_argv(log)
    assert a[0] == "chat" and "-p" not in a  # the default profile, as before
    assert a[a.index("-t") + 1] == "session_search"
    pc = h.check_profile()
    assert pc["ok"] is False and why in pc["why"]


def test_no_profile_is_the_old_behaviour(setup):
    h, log = setup(GOOD, profile="")
    h.ask("who is Ada?")
    a = chat_argv(log)
    assert a[0] == "chat" and a[a.index("-t") + 1] == "session_search"
    assert not [x for x in log.read_text().splitlines() if '"config"' in x]  # never checked


def test_bad_profile_names_are_ignored(setup):
    h, _ = setup(GOOD, profile="../default; rm")
    assert h.profile == ""


def test_the_check_is_cached(setup):
    h, log = setup(GOOD)
    h.ask("one")
    h.ask("two")
    gets = [x for x in log.read_text().splitlines() if '"config"' in x]
    assert len(gets) == 3  # nexus include, ursa include, servers: once for both asks


def test_read_tool_lists_hold_no_writes():
    writes = (
        "log",
        "add",
        "update",
        "edit",
        "link",
        "tag",
        "assign",
        "submit",
        "cancel",
        "hold",
        "release",
        "upload",
    )
    for name, tools in PROFILE_READ_TOOLS.items():
        for t in tools:
            assert not any(t.endswith(f"_{w}") or f"_{w}_" in t for w in writes), (name, t)
    assert not PROFILE_READ_TOOLS["ursa"] & {
        "files_read",
        "files_list",
        "results_link",
        "job_results",
    }
