"""v0.12: the ledger goes through `nexus serve` when it runs, the CLI otherwise.

A fake serve (a real ThreadingHTTPServer on loopback) answers like nexus serve. The
rules under test: reads fall back on any serve failure; writes fall back only when
serve refused before running anything, and are never re-run on the CLI when serve
may have run them; the URL must be loopback; the token comes from the ledger's file.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from ultra.config import Config
from ultra.ledger import Ledger
from ultra.ledger_serve import ServeClient, ServeError, ServeUnavailable, _loopback_url
from ultra.ledger_write import LedgerWriter
from ultra.store import Store

TOKEN = "s" * 43
TID = "11111111-2222-3333-4444-555555555555"


class FakeServe:
    """Scripted nexus serve. `mode` picks the reply for /api/run."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.mode = "ok"
        self.reply: dict[str, Any] = {"exit_code": 0, "stdout": "{}", "stderr": ""}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a: Any) -> None:
                pass

            def _send(self, status: int, d: dict[str, Any]) -> None:
                b = json.dumps(d).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self) -> None:
                self._send(200, {"ok": True, "env": "dev"})

            def do_POST(self) -> None:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                outer.calls.append(
                    {
                        "body": body,
                        "auth": self.headers.get("Authorization"),
                        "ctype": self.headers.get("Content-Type"),
                    }
                )
                if self.headers.get("Authorization") != f"Bearer {TOKEN}":
                    return self._send(401, {"error": "bad token"})
                m = outer.mode
                if m == "ok":
                    return self._send(200, {"job": "j", "done": True, **outer.reply})
                if m == "refused":
                    return self._send(403, {"error": "not available through serve"})
                if m == "confirm":
                    return self._send(409, {"confirm_required": True, "token": "x"})
                if m == "pending":
                    return self._send(202, {"job": "j", "done": False})
                if m == "pending200":  # a done:false body even with a 200 status
                    return self._send(200, {"job": "j", "done": False, "exit_code": 0})
                if m == "garbage":
                    b = b"<html>oops"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(b)))
                    self.end_headers()
                    self.wfile.write(b)
                    return None
                if m == "error500":
                    return self._send(500, {"error": "boom"})
                return None

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()


@pytest.fixture
def serve(tmp_path):
    fs = FakeServe()
    tok = tmp_path / "serve.token"
    tok.write_text(TOKEN + "\n")
    yield fs, tok
    fs.close()


def _cfg(fs: FakeServe | None, tok, binary: str = "nexus", **extra: Any) -> Config:
    led: dict[str, Any] = {"binary": binary, **extra}
    if fs is not None:
        led.update(serve_url=f"http://127.0.0.1:{fs.port}", serve_token_file=str(tok))
    return Config({"ledger": led})


def _fake_cli(tmp_path, out: str = '{"netid": "cli"}', rc: int = 0) -> tuple[str, Any]:
    """A fake nexus binary that records each run in a file."""
    log = tmp_path / "cli_calls.txt"
    b = tmp_path / "nexus"
    b.write_text(
        "#!/usr/bin/env python3\nimport sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n"
        f"print({out!r})\nsys.exit({rc})\n"
    )
    b.chmod(0o755)
    return str(b), log


def _cli_calls(log) -> list[str]:
    return log.read_text().splitlines() if log.exists() else []


# ------------------------------------------------------------------ client
def test_url_must_be_loopback_http() -> None:
    assert _loopback_url("http://127.0.0.1:7450") == "http://127.0.0.1:7450"
    assert _loopback_url("http://localhost:7450") == "http://localhost:7450"
    for bad in (
        "http://192.168.1.5:7450",
        "https://127.0.0.1:7450",
        "http://evil.example:7450",
        "file:///etc/passwd",
        "http://100.64.1.2:7450",
    ):
        with pytest.raises(ValueError):
            _loopback_url(bad)


def test_client_sends_token_json_and_args(serve) -> None:
    fs, tok = serve
    fs.reply = {"exit_code": 0, "stdout": '{"a": 1}', "stderr": ""}
    c = ServeClient(_cfg(fs, tok))
    rc, out, _err = c.run(["people", "show", "adal", "--json"], stdin="x")
    assert (rc, out) == (0, '{"a": 1}')
    call = fs.calls[-1]
    assert call["auth"] == f"Bearer {TOKEN}" and call["ctype"] == "application/json"
    assert call["body"]["args"] == ["people", "show", "adal", "--json"]
    assert call["body"]["stdin"] == "x"
    assert c.last["via"] == "serve"


def test_client_classifies_failures(serve, tmp_path) -> None:
    fs, tok = serve
    c = ServeClient(_cfg(fs, tok))
    for mode in ("refused", "confirm"):
        fs.mode = mode
        with pytest.raises(ServeUnavailable):
            c.run(["stats"])
    for mode in ("pending", "pending200", "garbage", "error500"):
        fs.mode = mode
        with pytest.raises(ServeError):
            c.run(["stats"])
    # wrong token -> unavailable (nothing ran) and marked down for a while
    (tmp_path / "bad.token").write_text("w" * 43)
    c2 = ServeClient(_cfg(fs, tmp_path / "bad.token"))
    fs.mode = "ok"
    with pytest.raises(ServeUnavailable):
        c2.run(["stats"])
    assert not c2.available()
    # no token file -> unavailable
    c3 = ServeClient(_cfg(fs, tmp_path / "missing.token"))
    with pytest.raises(ServeUnavailable):
        c3.run(["stats"])


def test_client_marks_down_when_nothing_listens(tmp_path) -> None:
    tok = tmp_path / "t"
    tok.write_text(TOKEN)
    c = ServeClient(
        Config({"ledger": {"serve_url": "http://127.0.0.1:9", "serve_token_file": str(tok)}})
    )
    with pytest.raises(ServeUnavailable):
        c.run(["stats"])
    assert not c.available()


def test_use_serve_false_disables_it(serve) -> None:
    fs, tok = serve
    c = ServeClient(_cfg(fs, tok, use_serve=False))
    assert not c.available()


# ------------------------------------------------------------------ reads
def test_reads_use_serve_and_skip_the_cli(serve, tmp_path) -> None:
    fs, tok = serve
    b, log = _fake_cli(tmp_path)
    fs.reply = {"exit_code": 0, "stdout": '{"netid": "fromserve"}', "stderr": ""}
    led = Ledger(_cfg(fs, tok, binary=b), Store(tmp_path / "s.db"))
    assert led._run(["people", "show", "adal"]) == {"netid": "fromserve"}
    assert fs.calls[-1]["body"]["args"] == ["people", "show", "adal", "--json"]
    assert _cli_calls(log) == []


def test_reads_fall_back_to_the_cli(serve, tmp_path) -> None:
    fs, tok = serve
    b, log = _fake_cli(tmp_path)
    for mode in ("refused", "pending", "garbage"):
        fs.mode = mode
        led = Ledger(_cfg(fs, tok, binary=b), Store(tmp_path / f"{mode}.db"))
        assert led._run(["people", "show", "adal"]) == {"netid": "cli"}
    assert len(_cli_calls(log)) == 3


def test_read_allow_list_still_applies_before_serve(serve, tmp_path) -> None:
    from ultra.ledger import LedgerError

    fs, tok = serve
    led = Ledger(_cfg(fs, tok), Store(tmp_path / "s.db"))
    with pytest.raises(LedgerError):
        led._run(["people", "delete", "adal"])
    assert fs.calls == []


# ------------------------------------------------------------------ writes
def test_writes_use_serve_with_stdin(serve, tmp_path) -> None:
    fs, tok = serve
    b, log = _fake_cli(tmp_path)
    fs.reply = {"exit_code": 0, "stdout": f"Logged (ID: {TID})", "stderr": ""}
    w = LedgerWriter(_cfg(fs, tok, binary=b), Store(tmp_path / "s.db"))
    r = w.log("Met Ada about the plan.", "2026-09-30 10:00", [])
    assert r["id"] == TID
    body = fs.calls[-1]["body"]
    assert body["args"][0] == "log" and body["stdin"] == "Met Ada about the plan.\n"
    assert _cli_calls(log) == []


def test_a_write_serve_may_have_run_is_never_rerun_on_the_cli(serve, tmp_path) -> None:
    fs, tok = serve
    b, log = _fake_cli(tmp_path, out=f"Logged (ID: {TID})")
    for mode in ("pending", "pending200", "garbage", "error500"):
        fs.mode = mode
        w = LedgerWriter(_cfg(fs, tok, binary=b), Store(tmp_path / f"{mode}.db"))
        r = w.log("Once only.", "2026-09-30 10:00", [])
        assert r["id"] is None and r["rc"] != 0
        assert "ledger serve" in r["output_tail"]
    assert _cli_calls(log) == []  # not one duplicate


def test_a_write_serve_refused_falls_back_to_the_cli(serve, tmp_path) -> None:
    fs, tok = serve
    b, log = _fake_cli(tmp_path, out=f"Logged (ID: {TID})")
    fs.mode = "refused"
    w = LedgerWriter(_cfg(fs, tok, binary=b), Store(tmp_path / "s.db"))
    assert w.log("Via the CLI.", "2026-09-30 10:00", [])["id"] == TID
    assert len(_cli_calls(log)) == 1


def test_write_allow_list_still_applies_before_serve(serve, tmp_path) -> None:
    from ultra.ledger_write import WriteError

    fs, tok = serve
    w = LedgerWriter(_cfg(fs, tok), Store(tmp_path / "s.db"))
    w.enabled = True
    for bad in (["db-reset"], ["people", "delete", "x"], ["people", "add", "x", "Y Z"]):
        with pytest.raises(WriteError):
            w._run(bad)
    assert fs.calls == []
