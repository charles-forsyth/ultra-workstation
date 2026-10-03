"""v1.3.0: the MCP client for the hosted ledger and cluster servers (SPEC 8.8).

A fake server (real ThreadingHTTPServer on loopback) speaks the same OAuth metadata,
token endpoint (rotating refresh tokens: a reused one is refused) and stateless
JSON-RPC /mcp as the hosted servers, answering in JSON or SSE.
"""

from __future__ import annotations

import json
import os
import stat
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from ultra import mcpclient
from ultra.config import Config
from ultra.mcpclient import (
    Budget,
    McpAuthNeeded,
    McpClient,
    McpToolError,
    McpUnknown,
    McpUnreachable,
    _parse_body,
)


class FakeMcp:
    def __init__(self) -> None:
        self.refresh_calls = 0
        self.refresh_valid = {"r0"}
        self.access_valid = {"a0"}
        self.n = 0
        self.lock = threading.Lock()
        self.tool_calls: list[dict[str, Any]] = []
        self.sse = False
        self.mode = "ok"  # ok | 500 | hang | 401always | tool_error | error_json
        self.refresh_delay = 0.0
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def _send(self, status: int, body: Any, ctype: str = "application/json") -> None:
                b = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self) -> None:
                base = f"http://127.0.0.1:{outer.port}"
                if self.path == "/.well-known/oauth-authorization-server":
                    return self._send(
                        200,
                        {
                            "authorization_endpoint": base + "/authorize",
                            "token_endpoint": base + "/token",
                        },
                    )
                if self.path == "/health":
                    return self._send(200, {"status": "ok", "tools_version": "7"})
                if self.path == "/whoami":
                    tok = self.headers.get("Authorization", "")[7:]
                    if tok not in outer.access_valid:
                        return self._send(401, {"error": "invalid_token"})
                    return self._send(
                        200, {"email": "ada@example.org", "role": "admin", "calls_per_min": 600}
                    )
                self._send(404, {})

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n)
                if self.path == "/token":
                    f = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode()).items()}
                    if f.get("grant_type") == "authorization_code":
                        return self._send(
                            200,
                            {"access_token": "a0", "refresh_token": "r0", "expires_in": 3600},
                        )
                    time.sleep(outer.refresh_delay)
                    with outer.lock:
                        outer.refresh_calls += 1
                        rt = f.get("refresh_token")
                        if rt not in outer.refresh_valid:
                            return self._send(400, {"error": "invalid_grant"})
                        outer.refresh_valid.discard(rt)  # rotate
                        outer.n += 1
                        new_a, new_r = f"a{outer.n}", f"r{outer.n}"
                        outer.refresh_valid.add(new_r)
                        outer.access_valid = {new_a}
                    return self._send(
                        200, {"access_token": new_a, "refresh_token": new_r, "expires_in": 3600}
                    )
                if self.path == "/mcp":
                    tok = self.headers.get("Authorization", "")[7:]
                    if outer.mode == "401always" or tok not in outer.access_valid:
                        return self._send(401, {"error": "invalid_token"})
                    msg = json.loads(raw)
                    outer.tool_calls.append(msg)
                    if outer.mode == "500":
                        return self._send(500, {"error": "boom"})
                    if outer.mode == "hang":
                        time.sleep(2)
                        return self._send(200, {})
                    if outer.mode == "error_json":
                        result = {"content": [{"type": "text", "text": '{"error": "No task x"}'}]}
                    elif outer.mode == "error_structured":  # FastMCP: str wrapped in result
                        result = {
                            "content": [{"type": "text", "text": '{"error": "No unit zz"}'}],
                            "structuredContent": {"result": '{"error": "No unit zz"}'},
                        }
                    elif outer.mode == "structured":
                        result = {
                            "content": [{"type": "text", "text": "[1, 2]"}],
                            "structuredContent": {"result": "[1, 2]"},
                        }
                    elif outer.mode == "tool_error":
                        result = {"content": [{"type": "text", "text": "bad"}], "isError": True}
                    elif msg["method"] == "tools/list":
                        result = {"tools": [{"name": "nexus_search"}]}
                    else:
                        args = msg["params"]["arguments"]
                        result = {"content": [{"type": "text", "text": json.dumps({"echo": args})}]}
                    reply = {"jsonrpc": "2.0", "id": msg["id"], "result": result}
                    if outer.sse:
                        body = f"event: message\ndata: {json.dumps(reply)}\n\n".encode()
                        return self._send(200, body, "text/event-stream")
                    return self._send(200, reply)
                self._send(404, {})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def close(self) -> None:
        self.srv.shutdown()


@pytest.fixture
def fake():
    f = FakeMcp()
    yield f
    f.close()


def _client(fake: FakeMcp, tmp_path, exp_in: float = 3600, **kw: Any) -> McpClient:
    tf = tmp_path / "tokens" / "mcp-nexus.json"
    c = McpClient("nexus", fake.url, "ledger-ultra", tf, **kw)
    c.tokens.save(
        {
            "access_token": "a0",
            "refresh_token": "r0",
            "_exp": time.time() + exp_in,
            "token_endpoint": f"http://127.0.0.1:{fake.port}/token",
        }
    )
    return c


def test_call_returns_json_and_sends_jsonrpc(fake, tmp_path):
    c = _client(fake, tmp_path)
    assert c.call("nexus_search", {"term": "ada"}) == {"echo": {"term": "ada"}}
    m = fake.tool_calls[-1]
    assert m["jsonrpc"] == "2.0" and m["method"] == "tools/call"
    assert m["params"] == {"name": "nexus_search", "arguments": {"term": "ada"}}
    assert c.state["ok"] is True and c.state["ms"] is not None


def test_sse_answer_parsed(fake, tmp_path):
    fake.sse = True
    c = _client(fake, tmp_path)
    assert c.call("x", {"a": 1}) == {"echo": {"a": 1}}


def test_parse_body_takes_last_data_line():
    body = b'event: message\ndata: {"id": 1, "result": 1}\n\ndata: {"id": 2, "result": 2}\n'
    assert _parse_body(body, "text/event-stream")["result"] == 2
    with pytest.raises(ValueError):
        _parse_body(b"event: x\n\n", "text/event-stream")


def test_expired_token_refreshes_and_saves_rotated_pair(fake, tmp_path):
    c = _client(fake, tmp_path, exp_in=10)  # inside the refresh margin
    c.call("x")
    tok = json.loads(c.tokens.path.read_text())
    assert tok["refresh_token"] == "r1" and tok["access_token"] == "a1"
    assert fake.refresh_calls == 1
    assert "r0" not in fake.refresh_valid  # the old one is dead on the server


def test_token_file_is_mode_600_and_dir_700(fake, tmp_path):
    c = _client(fake, tmp_path)
    assert stat.S_IMODE(os.stat(c.tokens.path).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(c.tokens.path.parent).st_mode) == 0o700


def test_concurrent_refresh_is_single_flight(fake, tmp_path):
    """Six threads find the token expired at once: exactly one refresh POST, and all
    six calls succeed (a second refresh would spend a rotated token and sign out)."""
    fake.refresh_delay = 0.2
    c = _client(fake, tmp_path, exp_in=0, parallel=8)
    errs: list[Exception] = []

    def go() -> None:
        try:
            c.call("x")
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=go) for _ in range(6)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errs == []
    assert fake.refresh_calls == 1


def test_401_refreshes_once_then_retries(fake, tmp_path):
    c = _client(fake, tmp_path)
    fake.access_valid = {"server-rotated"}  # our access token was revoked server side
    fake.refresh_valid = {"r0"}
    assert c.call("x", {"k": 1}) == {"echo": {"k": 1}}
    assert fake.refresh_calls == 1


def test_401_after_refresh_means_signed_out(fake, tmp_path):
    c = _client(fake, tmp_path)
    fake.mode = "401always"
    with pytest.raises(McpAuthNeeded):
        c.call("x")
    assert fake.refresh_calls == 1  # one refresh, never a loop
    assert c.state["ok"] is False


def test_dead_refresh_token_asks_for_sign_in(fake, tmp_path):
    c = _client(fake, tmp_path, exp_in=0)
    fake.refresh_valid = set()
    with pytest.raises(McpAuthNeeded, match="ultra auth nexus"):
        c.call("x")


def test_not_signed_in(fake, tmp_path):
    c = McpClient("nexus", fake.url, "ledger-ultra", tmp_path / "none.json")
    assert not c.signed_in()
    with pytest.raises(McpAuthNeeded, match="ultra auth nexus"):
        c.call("x")


def test_server_error_is_unknown_not_unreachable(fake, tmp_path):
    """A 5xx may come after the tool ran: callers must not re-send a write."""
    c = _client(fake, tmp_path)
    fake.mode = "500"
    with pytest.raises(McpUnknown):
        c.call("nexus_log", {"text": "x"})
    assert len(fake.tool_calls) == 1  # sent once, never retried


def test_timeout_after_send_is_unknown(fake, tmp_path):
    c = _client(fake, tmp_path, timeout=0.3)
    fake.mode = "hang"
    with pytest.raises(McpUnknown):
        c.call("nexus_log", {"text": "x"})
    assert len(fake.tool_calls) == 1


def test_connection_refused_is_unreachable(tmp_path):
    c = McpClient("nexus", "http://127.0.0.1:9/mcp", "ledger-ultra", tmp_path / "t.json")
    c.tokens.save({"access_token": "a", "refresh_token": "r", "_exp": time.time() + 3600})
    with pytest.raises(McpUnreachable):
        c.call("x")


def test_tool_errors(fake, tmp_path):
    c = _client(fake, tmp_path)
    fake.mode = "tool_error"
    with pytest.raises(McpToolError):
        c.call("x")
    fake.mode = "error_json"  # the ledger server reports errors as {"error": ...}
    with pytest.raises(McpToolError, match="No task x"):
        c.call("nexus_tasks_show", {"task_id": "x"})
    fake.mode = "error_structured"  # ...and the same inside structuredContent
    with pytest.raises(McpToolError, match="No unit zz"):
        c.call("nexus_labs_show", {"name": "zz"})
    fake.mode = "structured"
    assert c.call("x") == [1, 2]


def test_https_required_except_loopback(tmp_path):
    with pytest.raises(ValueError, match="https"):
        McpClient("nexus", "http://evil.example.com/mcp", "c", tmp_path / "t.json")
    McpClient("nexus", "https://ok.example.org/mcp", "c", tmp_path / "t.json")


def test_whoami_and_health(fake, tmp_path):
    c = _client(fake, tmp_path)
    assert c.whoami()["role"] == "admin"
    assert c.health()["tools_version"] == "7"


def test_parallel_cap(fake, tmp_path):
    """Never more than `parallel` calls in flight."""
    c = _client(fake, tmp_path, parallel=2)
    inflight, peak = [0], [0]
    lk = threading.Lock()
    real = c._http

    def slow(*a: Any, **k: Any):
        with lk:
            inflight[0] += 1
            peak[0] = max(peak[0], inflight[0])
        time.sleep(0.05)
        try:
            return real(*a, **k)
        finally:
            with lk:
                inflight[0] -= 1

    c._http = slow  # type: ignore[method-assign]
    ts = [threading.Thread(target=c.call, args=("x",)) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert peak[0] <= 2


def test_budget_refuses_past_the_minute():
    now = [1000.0]
    b = Budget(3, clock=lambda: now[0])
    for _ in range(3):
        b.take(wait=0)
    with pytest.raises(McpToolError, match="budget"):
        b.take(wait=0)
    now[0] += 61
    b.take(wait=0)


def test_config_builds_clients_only_when_set(tmp_path):
    assert mcpclient.client_from_config(Config({}), "nexus") is None
    cfg = Config(
        {"mcp": {"nexus": {"url": "https://l.example.org/mcp", "client_id": "ledger-ultra"}}}
    )
    c = mcpclient.client_from_config(cfg, "nexus")
    assert c is not None and c.client_id == "ledger-ultra" and c.base == "https://l.example.org"
    assert c.tokens.path.name == "mcp-nexus.json"
    off = Config(
        {"mcp": {"ursa": {"url": "https://c.example.org", "client_id": "x", "enabled": False}}}
    )
    assert mcpclient.client_from_config(off, "ursa") is None
    with pytest.raises(ValueError):
        mcpclient.client_from_config(cfg, "bogus")


def test_sign_in_flow_end_to_end(fake, tmp_path):
    """PKCE sign-in against the fake: browser step simulated by hitting the callback."""
    c = McpClient("nexus", fake.url, "ledger-ultra", tmp_path / "t" / "mcp-nexus.json")
    opened: list[str] = []

    def fake_browser(url: str) -> None:
        opened.append(url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        assert q["code_challenge_method"] == ["S256"] and q["client_id"] == ["ledger-ultra"]
        cb = q["redirect_uri"][0]
        assert cb.startswith("http://127.0.0.1:") and cb.endswith("/callback")
        urllib.request.urlopen(f"{cb}?code=c1&state={q['state'][0]}").read()  # noqa: S310

    import webbrowser

    orig = webbrowser.open
    webbrowser.open = lambda u: threading.Thread(target=fake_browser, args=(u,)).start()  # type: ignore[assignment]
    try:
        tok = mcpclient.sign_in(c)
    finally:
        webbrowser.open = orig  # type: ignore[assignment]
    assert opened and tok["refresh_token"] == "r0"
    assert c.signed_in()


def test_sign_in_rejects_wrong_state(fake, tmp_path):
    import webbrowser

    c = McpClient("nexus", fake.url, "ledger-ultra", tmp_path / "t.json")

    def bad(url: str) -> None:
        cb = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["redirect_uri"][0]
        urllib.request.urlopen(f"{cb}?code=c1&state=forged").read()  # noqa: S310

    orig = webbrowser.open
    webbrowser.open = lambda u: threading.Thread(target=bad, args=(u,)).start()  # type: ignore[assignment]
    try:
        with pytest.raises(McpAuthNeeded):
            mcpclient.sign_in(c)
    finally:
        webbrowser.open = orig  # type: ignore[assignment]
    assert not c.signed_in()


def test_doctor_reports_mcp(fake, tmp_path, monkeypatch):
    from ultra import doctor

    cfg = Config({"mcp": {"nexus": {"url": fake.url, "client_id": "ledger-ultra"}}})
    checks = {c.name: c for c in doctor.mcp_checks(cfg)}
    assert "not signed in" in checks["mcp nexus"].detail and not checks["mcp nexus"].ok
    c = mcpclient.client_from_config(cfg, "nexus")
    assert c is not None
    c.tokens.save({"access_token": "a0", "refresh_token": "r0", "_exp": time.time() + 3600})
    checks = {c.name: c for c in doctor.mcp_checks(cfg)}
    assert checks["mcp nexus"].ok and "ada@example.org (admin)" in checks["mcp nexus"].detail


def test_cli_auth_status_and_sign_out(fake, tmp_path, monkeypatch, capsys):
    from ultra import cli, config

    d = config.config_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.toml").write_text(f'[mcp.nexus]\nurl = "{fake.url}"\nclient_id = "ledger-ultra"\n')
    c = mcpclient.client_from_config(config.load(), "nexus")
    assert c is not None
    c.tokens.save({"access_token": "a0", "refresh_token": "r0", "_exp": time.time() + 3600})
    with pytest.raises(SystemExit) as e:
        cli.main(["auth", "nexus", "--status"])
    assert e.value.code == 0 and "email=ada@example.org" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        cli.main(["auth", "nexus", "--sign-out"])
    assert e.value.code == 0 and not c.tokens.path.exists()
    with pytest.raises(SystemExit) as e:
        cli.main(["auth", "ursa", "--status"])
    assert e.value.code == 1  # not configured
