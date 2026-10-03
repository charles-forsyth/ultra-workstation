"""v1.11: workspaces (SPEC 7.14). Work and Personal, each a whole Ultra.

The main workspace is today's folders; each other workspace is `workspaces/<slug>/`
under the config and data folders. These tests check that a workspace reads and writes
only its own folders, that requests are routed by header (or ?ws= on links), and that
unknown or bad workspace names are refused.
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ultra import config
from ultra.config import Config, list_workspaces, load_workspace

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "src/ultra/static"


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    home = tmp_path / "cfg" / "ultra-workstation"
    home.mkdir(parents=True)
    (home / "config.toml").write_text('[workspace]\nname = "Work"\n[ledger]\nenabled = false\n')
    p = home / "workspaces" / "personal"
    p.mkdir(parents=True)
    (p / "config.toml").write_text(
        '[workspace]\nname = "Personal"\ncolor = "green"\n'
        '[operator]\naddresses = ["me@example.com"]\n[ledger]\nenabled = false\n'
    )
    return tmp_path


def test_workspace_has_its_own_folders(xdg):
    main = config.load()
    ws = load_workspace("personal")
    assert main.config_home == xdg / "cfg/ultra-workstation"
    assert ws.config_home == xdg / "cfg/ultra-workstation/workspaces/personal"
    assert ws.data_home == xdg / "data/ultra-workstation/workspaces/personal"
    assert ws.style_path == ws.config_home / "style.toml"
    assert ws.workspace == {"slug": "personal", "name": "Personal", "color": "green"}
    assert main.workspace["slug"] == "main" and main.workspace["name"] == "Work"


@pytest.mark.parametrize("bad", ["", "main", "../x", "Personal", "a/b", "x" * 40, "missing"])
def test_bad_or_missing_workspace_is_refused(xdg, bad):
    with pytest.raises(KeyError):
        load_workspace(bad)


def test_list_puts_main_first_and_skips_broken(xdg):
    broken = xdg / "cfg/ultra-workstation/workspaces/broken"
    broken.mkdir()
    (broken / "config.toml").write_text("not = [toml")
    names = [w["name"] for w in list_workspaces(config.load())]
    assert names == ["Work", "Personal"]


def test_mcp_tokens_and_store_are_per_workspace(xdg):
    from ultra.live import Live
    from ultra.mcpclient import token_path

    ws = load_workspace("personal")
    assert token_path("nexus", ws).is_relative_to(ws.config_home)
    assert token_path("nexus") == xdg / "cfg/ultra-workstation/tokens/mcp-nexus.json"
    live = Live(ws)
    assert live.store.path == ws.data_home / "state.db"
    assert live.store.path.exists()
    assert not (xdg / "data/ultra-workstation/state.db").exists()  # main untouched


def test_google_client_cache_is_keyed_by_token_file():
    import inspect

    from ultra import google_auth

    src = inspect.getsource(google_auth.service)
    assert 'tp = str(cfg.get("google", f"token_{capability}")' in src and "tp, threading" in src


def _serve(api):
    from http.server import ThreadingHTTPServer

    from ultra.server import make_handler

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(api, port)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _get(port, path, ws=None):
    h = {"Host": f"127.0.0.1:{port}"}
    if ws:
        h["X-Ultra-Workspace"] = ws
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=h)
    try:
        with urllib.request.urlopen(req) as r:  # noqa: S310 - loopback test server
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_requests_are_routed_by_header_and_query(xdg, monkeypatch):
    from ultra.live import Live
    from ultra.server import Api

    monkeypatch.setattr(Live, "warm", lambda self: None)
    api = Api(config.load(), "tok")
    httpd, port = _serve(api)
    try:
        assert _get(port, "/api/session")[1]["workspace"]["name"] == "Work"
        s = _get(port, "/api/session", "personal")[1]
        assert s["workspace"]["name"] == "Personal" and s["addresses"] == ["me@example.com"]
        assert _get(port, "/api/session?ws=personal")[1]["workspace"]["slug"] == "personal"
        code, body = _get(port, "/api/session", "nope")
        assert code == 404 and "Unknown workspace" in body["error"]
        code, _ = _get(port, "/api/session", "../etc")
        assert code == 404
        ws = _get(port, "/api/workspaces", "personal")[1]["workspaces"]
        assert [w["slug"] for w in ws] == ["main", "personal"]  # listed from main config
        assert api.for_workspace("personal") is api.for_workspace("personal")  # built once
    finally:
        httpd.shutdown()


def test_demo_has_no_other_workspaces():
    from ultra.server import Api

    api = Api(Config({}), "tok", demo=True)
    with pytest.raises(KeyError):
        api.for_workspace("personal")
    assert len(api.workspaces({}, None, None)["workspaces"]) == 1  # type: ignore[arg-type]


def test_page_sends_the_workspace_everywhere():
    app = (STATIC / "app.js").read_text()
    assert '"X-Ultra-Workspace": WS.get()' in app
    assert 'fetch("/api/session", { headers: { "X-Ultra-Workspace": WS.get() } })' in app
    # every raw /api/ URL (links, media) goes through WS.url
    import re

    for f in STATIC.glob("*.js"):
        for m in re.finditer(r"""(?:href|src)=\\?["'`]?(\$\{)?["'`]?/api/""", f.read_text()):
            pytest.fail(f"{f.name}: raw /api/ link without WS.url: {m.group(0)}")
    for f, needle in (
        ("day.js", 'WS.url("/api/day/agenda.ics")'),
        ("mailx.js", "WS.url(`/api/mail/attachment/"),
        ("tools.js", "WS.url(`/api/audio/file/"),
    ):
        assert needle in (STATIC / f).read_text(), f
    assert 'e.key === "W") nextWorkspace()' in app
    assert 'id="ws-switch"' in (STATIC / "index.html").read_text()
