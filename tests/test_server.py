"""Server and guard tests: loopback, Host, Origin, JSON-only writes, CSRF, CSP."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from ultra import guard
from ultra.config import load
from ultra.server import build


@pytest.fixture
def srv():
    httpd, api = build(load(Path("/nonexistent/config.toml")), 0, demo=True)
    port = int(httpd.server_address[1])
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield port, api.token
    httpd.shutdown()
    httpd.server_close()


def req(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": f"127.0.0.1:{port}"}
    h.update(headers or {})
    data = json.dumps(body).encode() if body is not None else None
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    raw = r.read()
    return r.status, dict(r.getheaders()), raw


def test_health_and_session_token(srv):
    port, token = srv
    s, _, raw = req(port, "GET", "/api/health")
    assert s == 200 and json.loads(raw)["demo"] is True
    s, _, raw = req(port, "GET", "/api/session")
    assert json.loads(raw)["token"] == token


def test_security_headers_on_every_response(srv):
    port, _ = srv
    for path in ("/", "/api/health", "/nope"):
        _, h, _ = req(port, "GET", path)
        assert h["Content-Security-Policy"] == guard.CSP
        assert h["X-Content-Type-Options"] == "nosniff"
        assert h["X-Frame-Options"] == "DENY"
        assert h["Cache-Control"] == "no-store"


def test_foreign_host_refused(srv):
    port, _ = srv
    s, _, _ = req(port, "GET", "/api/health", headers={"Host": "evil.example.com"})
    assert s == 403
    s, _, _ = req(port, "GET", "/api/health", headers={"Host": "127.0.0.1:1"})
    assert s == 403
    s, _, _ = req(port, "GET", "/api/health", headers={"Host": f"localhost:{port}"})
    assert s == 200


def test_write_needs_json_token_and_same_origin(srv):
    port, token = srv
    ok = {"Content-Type": "application/json", guard.CSRF_HEADER: token}
    # no token
    s, _, _ = req(port, "POST", "/api/health", {}, {"Content-Type": "application/json"})
    assert s == 403
    # wrong token
    s, _, _ = req(port, "POST", "/api/health", {}, {**ok, guard.CSRF_HEADER: "x"})
    assert s == 403
    # form post (no JSON content type) is refused before anything else
    s, _, _ = req(port, "POST", "/api/health", {}, {guard.CSRF_HEADER: token})
    assert s == 415
    # cross origin
    s, _, _ = req(port, "POST", "/api/health", {}, {**ok, "Origin": "https://evil.example"})
    assert s == 403
    # correct token reaches routing (GET-only route -> 405)
    s, _, _ = req(port, "POST", "/api/health", {}, ok)
    assert s == 405


def test_static_index_has_no_inline_script_or_style(srv):
    port, _ = srv
    s, h, raw = req(port, "GET", "/")
    html = raw.decode()
    assert s == 200 and "text/html" in h["Content-Type"]
    assert "<script>" not in html and "<style>" not in html
    assert " style=" not in html and " onclick=" not in html


def test_static_traversal_refused(srv):
    port, _ = srv
    for p in ("/../pyproject.toml", "/.hidden", "/%2e%2e/config.py"):
        s, _, _ = req(port, "GET", p)
        assert s == 404


def test_route_params_are_url_decoded(srv):
    port, _ = srv
    s, _, raw = req(port, "GET", "/api/context/ben%40example.org")
    assert s == 200 and json.loads(raw)["name"] == "Ben Carter"


def test_demo_stream_filters(srv):
    port, _ = srv
    _, _, raw = req(port, "GET", "/api/stream?filter=waiting")
    items = json.loads(raw)["items"]
    assert items and all(
        i["court"] == "WAITING" or (i["source"] == "task" and i["status"] == "BLOCKED")
        for i in items
    )
    assert any(i["court"] == "WAITING" for i in items)
    _, _, raw = req(port, "GET", "/api/stream?filter=slack")
    assert all(i["source"] == "slack" for i in json.loads(raw)["items"])


@pytest.mark.parametrize(
    "addr,expected",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("::ffff:127.0.0.1", True),
        ("10.0.0.5", False),
        ("bogus", False),
    ],
)
def test_loopback_peer(addr, expected):
    assert guard.loopback_peer(addr) is expected


def test_token_compare():
    assert guard.token_ok("abc", "abc")
    assert not guard.token_ok("", "abc")
    assert not guard.token_ok(None, "abc")
