"""A small MCP client for the hosted ledger and cluster servers (SPEC 8.8).

Standard library only. Adapted from the MIT-licensed stdlib client that ships with the
cluster server (`examples/mcp_client.py`), with what a threaded server app needs:

- One POST per call: JSON-RPC over Streamable HTTP to a stateless server, answers as
  plain JSON or as an SSE stream (both are parsed).
- OAuth 2.1 with PKCE as a *pre-registered program client* (a fixed client id from the
  server's users file, a loopback redirect, no dynamic registration).
- Refresh tokens rotate on every use and a reused one is refused, so the refresh runs
  under one lock per server, re-checks expiry inside the lock, and saves the new token
  pair atomically (temp file + rename, mode 600) BEFORE the new access token is used.
- At most N calls in flight per server (a semaphore below the server's own cap) and a
  client-side calls-per-minute budget below the server's limit.
- Two kinds of failure, never confused:
    McpUnreachable  the request never reached the server (DNS, connect refused, TLS,
                    401 before the tool ran). Nothing ran; a read may fall back.
    McpUnknown      the request may have reached the server and the outcome is not
                    known (timeout or connection lost after sending, 5xx, bad reply).
                    Writes are never re-sent after this.
  plus McpToolError for a tool that ran and said no (bad arguments, not found, rate
  limited) and McpAuthNeeded when nobody has signed in or the session ended.
- Server URLs and client ids come from private config only (the repo is public).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ultra.config import Config, config_dir, expand, private_dir

SERVERS = ("nexus", "ursa")  # config sections [mcp.nexus] / [mcp.ursa]
DEFAULTS = {
    # parallel: below each server's per-caller in-flight cap (16 on both);
    # per_min: below the program client's budget in the server's users file.
    "nexus": {"parallel": 8, "per_min": 500, "timeout": 60},
    "ursa": {"parallel": 2, "per_min": 100, "timeout": 120},
}
REFRESH_MARGIN = 60  # seconds: refresh an access token this long before it expires


class McpError(Exception):
    """Base class."""


class McpAuthNeeded(McpError):
    """Not signed in, or the session ended. Fix: `ultra auth <server>`."""


class McpUnreachable(McpError):
    """The request never reached the server; nothing ran (safe to fall back)."""


class McpUnknown(McpError):
    """The request may have run; the outcome is unknown. Never re-send a write."""


class McpToolError(McpError):
    """The tool ran and returned an error (bad args, not found, too many calls)."""


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def pkce_pair() -> tuple[str, str]:
    verifier = _b64(os.urandom(32))
    return verifier, _b64(hashlib.sha256(verifier.encode()).digest())


def _parse_body(body: bytes, ctype: str) -> dict[str, Any]:
    """A JSON-RPC message from a plain JSON or an SSE (text/event-stream) answer."""
    text = body.decode("utf-8", "replace")
    if "text/event-stream" in ctype.lower():
        msgs = [
            line[5:].strip()
            for line in text.splitlines()
            if line.startswith("data:") and line[5:].strip()
        ]
        if not msgs:
            raise ValueError("empty event stream")
        text = msgs[-1]
    msg = json.loads(text)
    if not isinstance(msg, dict):
        raise ValueError("not a JSON-RPC object")
    return msg


class TokenStore:
    """One server's tokens in a mode-600 JSON file under the config dir."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> dict[str, Any] | None:
        try:
            d = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return None
        return d if isinstance(d, dict) and d.get("refresh_token") else None

    def save(self, tok: dict[str, Any]) -> None:
        private_dir(self.path.parent)
        tmp = self.path.with_name(self.path.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(tok, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)  # atomic: a crash leaves the old or the new pair

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class Budget:
    """Sliding one-minute call budget (client side, below the server's limit)."""

    def __init__(self, per_min: int, clock: Callable[[], float] = time.monotonic):
        self.per_min = max(1, int(per_min))
        self.clock = clock
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def take(self, wait: float = 5.0) -> None:
        deadline = self.clock() + wait
        while True:
            with self.lock:
                now = self.clock()
                while self.calls and now - self.calls[0] >= 60:
                    self.calls.popleft()
                if len(self.calls) < self.per_min:
                    self.calls.append(now)
                    return
                retry = 60 - (now - self.calls[0])
            if self.clock() + min(retry, 0.25) > deadline:
                raise McpToolError("Ultra's own call budget for this server is used up")
            time.sleep(min(retry, 0.25))


class McpClient:
    """Client for one hosted MCP server. Thread-safe."""

    def __init__(
        self,
        name: str,
        url: str,
        client_id: str,
        token_file: Path,
        parallel: int = 4,
        per_min: int = 100,
        timeout: float = 60,
        opener: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        p = urllib.parse.urlparse(url)
        if p.scheme != "https" and p.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError(f"{name}: MCP server URL must be https (or loopback for tests)")
        self.name = name
        self.base = url.rstrip("/").removesuffix("/mcp")
        self.client_id = client_id
        self.tokens = TokenStore(token_file)
        self.sem = threading.BoundedSemaphore(max(1, parallel))
        self.budget = Budget(per_min)
        self.timeout = timeout
        self._open = opener or urllib.request.urlopen
        self._clock = clock
        self._refresh_lock = threading.Lock()
        self._ids = 0
        self._id_lock = threading.Lock()
        self.state: dict[str, Any] = {"ok": None, "error": "", "at": None, "ms": None}
        self._meta: dict[str, Any] | None = None

    # ------------------------------------------------------------------ HTTP
    def _http(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        """(status, headers, body). Raises McpUnreachable if nothing was sent, and
        McpUnknown if the connection broke after the request went out."""
        # url is always self.base (https or loopback, checked in __init__) or the
        # server's own OAuth metadata endpoints
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})  # noqa: S310
        try:
            with self._open(req, timeout=timeout or self.timeout) as r:
                return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
        except urllib.error.HTTPError as e:
            hdrs = {k.lower(): v for k, v in (e.headers or {}).items()}
            return e.code, hdrs, e.read() or b""
        except urllib.error.URLError as e:
            # connect refused, DNS, TLS handshake: the request was never delivered
            reason = e.reason
            if isinstance(reason, TimeoutError | OSError) and "timed out" in str(reason):
                raise McpUnknown(f"{self.name}: timed out") from e
            raise McpUnreachable(f"{self.name}: cannot reach the server ({reason})") from e
        except TimeoutError as e:
            raise McpUnknown(f"{self.name}: timed out waiting for the answer") from e
        except (ConnectionError, OSError) as e:
            raise McpUnknown(f"{self.name}: connection lost ({e})") from e

    # ------------------------------------------------------------------ OAuth
    def metadata(self) -> dict[str, Any]:
        if self._meta is not None:
            return self._meta
        st, _, body = self._http("GET", self.base + "/.well-known/oauth-authorization-server")
        if st != 200:
            raise McpUnreachable(f"{self.name}: no OAuth metadata (HTTP {st})")
        meta = json.loads(body)
        if not isinstance(meta, dict):
            raise McpUnreachable(f"{self.name}: bad OAuth metadata")
        self._meta = meta
        return meta

    def authorize_url(self, redirect_uri: str, state: str, challenge: str) -> str:
        meta = self.metadata()
        q = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return str(meta["authorization_endpoint"]) + "?" + urllib.parse.urlencode(q)

    def exchange(self, code: str, redirect_uri: str, verifier: str) -> dict[str, Any]:
        meta = self.metadata()
        st, _, body = self._http(
            "POST",
            str(meta["token_endpoint"]),
            urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": self.client_id,
                    "code_verifier": verifier,
                }
            ).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        )
        if st != 200:
            raise McpAuthNeeded(f"{self.name}: sign-in failed ({st}: {body[:200]!r})")
        tok = json.loads(body)
        tok["token_endpoint"] = meta["token_endpoint"]
        tok["client_id"] = self.client_id
        tok["_exp"] = self._clock() + int(tok.get("expires_in", 3600))
        self.tokens.save(tok)
        return tok

    def signed_in(self) -> bool:
        return self.tokens.load() is not None

    def access_token(self, force_refresh: bool = False) -> str:
        """A valid access token; refreshes under the lock (single flight)."""
        tok = self.tokens.load()
        if tok is None:
            raise McpAuthNeeded(f"not signed in to {self.name}: run `ultra auth {self.name}`")
        if not force_refresh and tok.get("_exp", 0) - self._clock() > REFRESH_MARGIN:
            return str(tok["access_token"])
        seen = tok.get("access_token")
        with self._refresh_lock:
            tok = self.tokens.load()  # another thread may have refreshed meanwhile
            if tok is None:
                raise McpAuthNeeded(f"not signed in to {self.name}: run `ultra auth {self.name}`")
            fresh = tok.get("_exp", 0) - self._clock() > REFRESH_MARGIN
            if fresh and (not force_refresh or tok.get("access_token") != seen):
                return str(tok["access_token"])
            st, _, body = self._http(
                "POST",
                str(tok.get("token_endpoint") or self.metadata()["token_endpoint"]),
                urllib.parse.urlencode(
                    {
                        "grant_type": "refresh_token",
                        "refresh_token": tok["refresh_token"],
                        "client_id": self.client_id,
                    }
                ).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"},
            )
            if st in (400, 401):
                raise McpAuthNeeded(
                    f"{self.name} session ended: run `ultra auth {self.name}` again"
                )
            if st != 200:
                raise McpUnreachable(f"{self.name}: token refresh failed (HTTP {st})")
            new = json.loads(body)
            tok.update(new)  # always keep the newest refresh token (they rotate)
            tok["_exp"] = self._clock() + int(new.get("expires_in", 3600))
            self.tokens.save(tok)  # saved before the new access token is used
            return str(tok["access_token"])

    # ------------------------------------------------------------------ JSON-RPC
    def _next_id(self) -> int:
        with self._id_lock:
            self._ids += 1
            return self._ids

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        """One JSON-RPC call. Retries once after a 401 by refreshing, which is safe
        because a 401 means the server refused before running anything."""
        self.budget.take()
        with self.sem:
            t0 = time.monotonic()
            st, hdrs, body = 0, dict[str, str](), b""
            for attempt in (0, 1):
                token = self.access_token(force_refresh=attempt == 1)
                payload = {
                    "jsonrpc": "2.0",
                    "id": self._next_id(),
                    "method": method,
                    "params": params or {},
                }
                st, hdrs, body = self._http(
                    "POST",
                    self.base + "/mcp",
                    json.dumps(payload).encode(),
                    {
                        "Authorization": "Bearer " + token,
                        "Content-Type": "application/json",
                        "Accept": "application/json, text/event-stream",
                    },
                )
                if st == 401 and attempt == 0:
                    continue
                break
            ms = int((time.monotonic() - t0) * 1000)
            if st == 401:
                self._fail("signed out")
                raise McpAuthNeeded(f"{self.name} refused the token: run `ultra auth {self.name}`")
            if st in (403, 404, 405, 429):
                self._fail(f"HTTP {st}")
                raise McpUnreachable(f"{self.name}: HTTP {st} {body[:200]!r}")
            if st != 200:
                self._fail(f"HTTP {st}")
                raise McpUnknown(f"{self.name}: HTTP {st}")
            try:
                msg = _parse_body(body, hdrs.get("content-type", ""))
            except (ValueError, UnicodeDecodeError) as e:
                self._fail("bad reply")
                raise McpUnknown(f"{self.name}: unreadable reply") from e
            if "error" in msg:
                err = msg["error"]
                text = err.get("message", str(err)) if isinstance(err, dict) else str(err)
                self._ok(ms)
                raise McpToolError(f"{self.name}: {text}")
            self._ok(ms)
            return msg.get("result")

    def _ok(self, ms: int) -> None:
        self.state = {"ok": True, "error": "", "at": self._clock(), "ms": ms}

    def _fail(self, why: str) -> None:
        self.state = {**self.state, "ok": False, "error": why, "at": self._clock()}

    def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        """Call a tool. Returns its JSON result (decoded from the text content);
        raises McpToolError when the tool reports an error."""
        res = self.rpc("tools/call", {"name": tool, "arguments": args or {}})
        if not isinstance(res, dict):
            raise McpUnknown(f"{self.name}: {tool} returned no result")
        if res.get("structuredContent") is not None and not res.get("isError"):
            sc = res["structuredContent"]
            # FastMCP wraps a str return as {"result": "<json>"}
            if isinstance(sc, dict) and set(sc) == {"result"} and isinstance(sc["result"], str):
                return _maybe_json(sc["result"])
            return sc
        text = "\n".join(
            str(p.get("text", ""))
            for p in res.get("content") or []
            if isinstance(p, dict) and p.get("type") == "text"
        )
        if res.get("isError"):
            raise McpToolError(f"{tool}: {text[:500] or 'error'}")
        data = _maybe_json(text)
        if isinstance(data, dict) and set(data) == {"error"}:
            raise McpToolError(f"{tool}: {data['error']}")
        return data

    def tools(self) -> list[dict[str, Any]]:
        res = self.rpc("tools/list")
        return list((res or {}).get("tools") or [])

    def read_resource(self, uri: str) -> str:
        res = self.rpc("resources/read", {"uri": uri})
        parts = (res or {}).get("contents") or []
        return "\n".join(str(p.get("text", "")) for p in parts if isinstance(p, dict))

    def whoami(self) -> dict[str, Any]:
        st, _, body = self._http(
            "GET", self.base + "/whoami", headers={"Authorization": "Bearer " + self.access_token()}
        )
        if st == 401:
            raise McpAuthNeeded(f"{self.name} refused the token: run `ultra auth {self.name}`")
        if st != 200:
            raise McpUnreachable(f"{self.name}: /whoami HTTP {st}")
        d = json.loads(body)
        return d if isinstance(d, dict) else {}

    def health(self) -> dict[str, Any]:
        st, _, body = self._http("GET", self.base + "/health", timeout=15)
        if st != 200:
            raise McpUnreachable(f"{self.name}: /health HTTP {st}")
        d = json.loads(body)
        return d if isinstance(d, dict) else {}


def _maybe_json(text: str) -> Any:
    t = text.strip()
    if t.startswith(("{", "[")):
        try:
            return json.loads(t)
        except ValueError:
            return text
    return text


# ---------------------------------------------------------------------- config
def token_path(name: str) -> Path:
    return config_dir() / "tokens" / f"mcp-{name}.json"


def configured(cfg: Config, name: str) -> bool:
    sec = cfg.section("mcp").get(name)
    return isinstance(sec, dict) and bool(sec.get("url")) and bool(sec.get("client_id"))


def client_from_config(cfg: Config, name: str) -> McpClient | None:
    """The client for [mcp.<name>], or None when it is not configured or disabled."""
    if name not in SERVERS:
        raise ValueError(f"unknown MCP server {name!r}")
    sec = cfg.section("mcp").get(name)
    if not isinstance(sec, dict) or not sec.get("url") or not sec.get("client_id"):
        return None
    if sec.get("enabled", True) is False:
        return None
    d = DEFAULTS[name]
    tf = expand(str(sec["token_file"])) if sec.get("token_file") else token_path(name)
    return McpClient(
        name,
        str(sec["url"]),
        str(sec["client_id"]),
        tf,
        parallel=min(int(sec.get("parallel", d["parallel"])), 12),
        per_min=int(sec.get("per_min", d["per_min"])),
        timeout=float(sec.get("timeout", d["timeout"])),
    )


# ---------------------------------------------------------------------- sign-in
def sign_in(client: McpClient, port: int = 0, open_browser: bool = True) -> dict[str, Any]:
    """Browser sign-in on a loopback redirect (any port is accepted by both servers)."""
    import http.server
    import webbrowser

    got: dict[str, str] = {}
    done = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            u = urllib.parse.urlparse(self.path)
            if u.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            got.update({k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()})
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Signed in to Ultra. You can close this tab.")
            done.set()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), Handler)
    redirect = f"http://127.0.0.1:{srv.server_address[1]}/callback"
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(16)
    url = client.authorize_url(redirect, state, challenge)
    t = threading.Thread(target=lambda: srv.serve_forever(poll_interval=0.2), daemon=True)
    t.start()
    print(f"Sign in to {client.name} in your browser. If it does not open, visit:\n{url}")
    if open_browser:
        webbrowser.open(url)
    try:
        if not done.wait(600):
            raise McpAuthNeeded("sign-in timed out")
    finally:
        srv.shutdown()
    if got.get("state") != state or "code" not in got:
        raise McpAuthNeeded(f"sign-in failed: {got.get('error') or 'no code returned'}")
    return client.exchange(got["code"], redirect, verifier)
