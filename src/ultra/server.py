"""HTTP server: stdlib ThreadingHTTPServer, JSON API, static files.

Every request passes the guards in guard.py. The page gets the CSRF token from
/api/session (a same-origin GET); every non-GET must send it back in X-Ultra-Token.
"""

from __future__ import annotations

import json
import mimetypes
import re
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ultra import __version__, guard
from ultra.config import Config
from ultra.ledger_write import new_tasks_on, tasks_off_note
from ultra.remote import PeerPolicy, policy_from_config

MAX_BODY = 25 * 1024 * 1024  # attachments ride in JSON as base64 later

Handler = Callable[[dict[str, list[str]], Any, re.Match[str]], Any]


class BytesFile:
    """In-memory file (a mail attachment). Served as a download unless ``inline`` is set
    AND the type is one the page may preview; always with nosniff and the strict CSP,
    plus a sandbox for inline documents."""

    def __init__(self, data: bytes, ctype: str, name: str, inline: bool = False):
        self.data = data
        self.ctype = ctype
        self.name = name
        self.inline = inline


class FileResponse:
    """Returned by a route to stream a local file (audio) with HTTP Range support."""

    def __init__(self, path: Any, ctype: str, download_name: str = ""):
        self.path = path
        self.ctype = ctype
        self.download_name = download_name


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class Api:
    """Route table. Adapters register their routes in later versions."""

    def __init__(self, cfg: Config, token: str, demo: bool = False):
        self.cfg = cfg
        self.token = token
        self.demo = demo
        self.routes: list[tuple[str, re.Pattern[str], Handler]] = []
        self.add("GET", r"/api/health", self.health)
        self.add("GET", r"/api/session", self.session)
        self.add("GET", r"/api/workspaces", self.workspaces)
        self.live: Any = None
        # v1.11 workspaces (SPEC 7.14): the main workspace is this Api; each other one is
        # its own Api (own Live, own config and folders), built on first use. A request
        # names its workspace in the X-Ultra-Workspace header (links: ?ws=).
        self.others: dict[str, Api] = {}
        self._others_lock = threading.Lock()
        self.main_cfg = cfg  # a workspace Api lists workspaces from the main config
        if demo:
            from ultra import demo as demo_data

            demo_data.register(self)
        else:
            from ultra.live import Live

            self.live = Live(cfg)
            self.live.register(self)

    def for_workspace(self, slug: str | None) -> Api:
        """The Api serving a workspace; the main one for "" / "main". KeyError if unknown."""
        if not slug or slug == "main" or slug == self.cfg.slug:
            return self
        if self.demo:
            raise KeyError("workspaces are off in demo mode")
        with self._others_lock:
            hit = self.others.get(slug)
            if hit is None:
                from ultra.config import load_workspace

                hit = Api(load_workspace(slug), self.token)
                hit.main_cfg = self.cfg
                self.others[slug] = hit
                if hit.live is not None:
                    hit.live.warm()
            return hit

    def workspaces(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        from ultra.config import list_workspaces

        if self.demo:
            return {"workspaces": [{**self.cfg.workspace, "main": True}]}
        return {"workspaces": list_workspaces(self.main_cfg)}

    def add(self, method: str, pattern: str, fn: Handler) -> None:
        self.routes.append((method, re.compile(f"^{pattern}$"), fn))

    def dispatch(
        self, method: str, path: str, query: dict[str, list[str]], body: Any
    ) -> tuple[int, Any]:
        allowed = False
        path = unquote(path)  # route params arrive percent-encoded (ben%40x.org)
        for m, rx, fn in self.routes:
            match = rx.match(path)
            if not match:
                continue
            allowed = True
            if m == method:
                return 200, fn(query, body, match)
        if allowed:
            raise ApiError(405, "Method not allowed")
        raise ApiError(404, "Not found")

    def health(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return {"ok": True, "version": __version__, "demo": self.demo}

    def session(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return {
            "token": self.token,
            "version": __version__,
            "demo": self.demo,
            "operator": self.cfg.get("operator", "name", ""),
            "timezone": self.cfg.timezone,
            "slack": bool(self.live and self.live.slack.enabled) or self.demo,
            "ai": bool(self.live and self.live.ai.enabled) or self.demo,
            # v1.6: "calm" (default) or "classic"; a browser can override it (dot menu)
            "layout": "classic" if self.cfg.get("ui", "layout", "calm") == "classic" else "calm",
            # v1.11: which workspace answered (the page shows its name and colour)
            "workspace": self.cfg.workspace,
            # v1.13: the Ledger place shows Nexus when there is a ledger, else Life (the vault)
            "ledger": bool(self.live and self.live.ledger.enabled) or self.demo,
            "vault": bool(self.live and self.live.vault.enabled),
            # v1.15: false where this workspace's ledger tasks are archive-only (the
            # page hides Task); a workspace whose Task goes to the vault keeps it
            "new_tasks": not self.live.desk.tasks_off() if self.live else new_tasks_on(self.cfg),
            "tasks_note": tasks_off_note(self.cfg),
            "addresses": sorted(self.cfg.my_addresses)[:4],
        }


def _static_bytes(path: str) -> tuple[bytes, str] | None:
    path = unquote(path)
    name = "index.html" if path in ("", "/") else path.lstrip("/")
    if ".." in name.split("/") or name.startswith("."):
        return None
    root = resources.files("ultra").joinpath("static")
    target = root.joinpath(*name.split("/"))
    if not target.is_file():
        return None
    ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if ctype.startswith("text/") or ctype.endswith("javascript"):
        ctype += "; charset=utf-8"
    return target.read_bytes(), ctype


def make_handler(
    api: Api, port: int, peer_ok: Callable[[str], bool] | None = None
) -> type[BaseHTTPRequestHandler]:
    remote = peer_ok is not None

    class H(BaseHTTPRequestHandler):
        server_version = f"ultra/{__version__}"
        peer_policy = peer_ok

        def log_message(self, format: str, *args: Any) -> None:
            return  # no access log: URLs can carry message ids

        def handle_one_request(self) -> None:
            try:
                super().handle_one_request()
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True

        def _send(self, status: int, payload: bytes, ctype: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Content-Security-Policy", guard.CSP)
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, status: int, obj: Any) -> None:
            data = json.dumps(obj, default=str).encode()
            self._send(status, data, "application/json; charset=utf-8")

        def _handle(self, method: str) -> None:
            url = urlparse(self.path)
            addr = self.client_address[0]
            if not (guard.loopback_peer(addr) or (peer_ok is not None and peer_ok(addr))):
                return self._json(403, {"error": "This address may not connect."})
            host = self.headers.get("Host", "")
            if not guard.host_allowed(host, port, remote):
                return self._json(403, {"error": "Unrecognised host name."})
            if not url.path.startswith("/api/"):
                if method != "GET":
                    return self._json(405, {"error": "Method not allowed"})
                found = _static_bytes(url.path)
                if not found:
                    return self._json(404, {"error": "Not found"})
                return self._send(200, *found)
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._json(413, {"error": "Request too large"})
            if method != "GET":
                if not guard.origin_allowed(self.headers.get("Origin"), host):
                    return self._json(403, {"error": "Cross-origin request refused"})
                if not self.headers.get("Content-Type", "").startswith("application/json"):
                    return self._json(415, {"error": "Use application/json"})
                if not guard.token_ok(self.headers.get(guard.CSRF_HEADER), api.token):
                    return self._json(403, {"error": "Missing or wrong token"})
            body = None
            if length:
                try:
                    body = json.loads(self.rfile.read(length) or b"null")
                except ValueError:
                    return self._json(400, {"error": "Invalid JSON"})
            query = parse_qs(url.query)
            ws = self.headers.get("X-Ultra-Workspace") or (query.get("ws") or [""])[0]
            try:
                target = api.for_workspace(ws)
            except KeyError as e:
                return self._json(404, {"error": f"Unknown workspace: {e}"})
            except Exception as e:  # noqa: BLE001 - a broken workspace config
                return self._json(500, {"error": f"Workspace {ws!r} failed to start: {e}"})
            try:
                status, obj = target.dispatch(method, url.path, query, body)
            except ApiError as e:
                return self._json(e.status, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 - never leak a traceback to the page
                return self._json(500, {"error": f"{type(e).__name__}: {e}"})
            if isinstance(obj, FileResponse):
                return self._file(obj)
            if isinstance(obj, BytesFile):
                return self._bytes(obj)
            if hasattr(obj, "text") and hasattr(obj, "ctype") and hasattr(obj, "name"):
                return self._text_file(obj)
            return self._json(status, obj)

        def _bytes(self, f: BytesFile) -> None:
            name = re.sub(r"[^A-Za-z0-9._ ()+-]", "_", f.name)[:120] or "attachment"
            self.send_response(200)
            self.send_header("Content-Type", f.ctype)
            self.send_header("Content-Length", str(len(f.data)))
            disp = "inline" if f.inline else "attachment"
            self.send_header("Content-Disposition", f'{disp}; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            # inline previews: no scripts, no forms, no navigation, nothing
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; sandbox"
                if f.inline
                else guard.CSP,
            )
            self.end_headers()
            self.wfile.write(f.data)

        def _text_file(self, f: Any) -> None:
            data = f.text.encode("utf-8")
            name = re.sub(r"[^A-Za-z0-9._-]", "_", f.name)[:80]
            self.send_response(200)
            self.send_header("Content-Type", f.ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", guard.CSP)
            self.end_headers()
            self.wfile.write(data)

        def _file(self, f: FileResponse) -> None:
            try:
                data = f.path.read_bytes()
            except OSError:
                return self._json(404, {"error": "File not found"})
            size = len(data)
            start, end, status = 0, size - 1, 200
            rng = self.headers.get("Range", "")
            m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
            if m and size:
                a, b = m.group(1), m.group(2)
                if a:
                    start = int(a)
                    end = min(int(b), size - 1) if b else size - 1
                elif b:  # suffix range: last N bytes
                    start = max(0, size - int(b))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                status = 206
            chunk = data[start : end + 1]
            self.send_response(status)
            self.send_header("Content-Type", f.ctype)
            self.send_header("Content-Length", str(len(chunk)))
            self.send_header("Accept-Ranges", "bytes")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            if f.download_name:
                self.send_header("Content-Disposition", f'attachment; filename="{f.download_name}"')
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", guard.CSP)
            self.end_headers()
            self.wfile.write(chunk)

        def do_GET(self) -> None:
            self._handle("GET")

        def do_POST(self) -> None:
            self._handle("POST")

        def do_PUT(self) -> None:
            self._handle("PUT")

        def do_DELETE(self) -> None:
            self._handle("DELETE")

    return H


def build(
    cfg: Config,
    port: int,
    demo: bool = False,
    host: str = "127.0.0.1",
    policy: PeerPolicy | None = None,
) -> tuple[ThreadingHTTPServer, Api]:
    """Bind host:port (0 = any free port) and return the server and its Api.

    On a non-loopback host, only clients the policy admits may connect (default: 192.168
    LANs and the operator's own tailnet devices, or [server] remote_networks).
    """
    api = Api(cfg, guard.new_token(), demo=demo)
    pol: PeerPolicy | None = None
    if not guard.loopback_peer(host):
        pol = (
            policy
            if policy is not None
            else policy_from_config(cfg.get("server", "remote_networks", None))
        )
        if not pol:
            raise ValueError("a non-loopback --host needs at least one allowed network")
    httpd = ThreadingHTTPServer((host, port), make_handler(api, port, pol))
    real = httpd.server_address[1]
    if real != port:  # port 0: the Host check must use the port actually bound
        httpd.RequestHandlerClass = make_handler(api, int(real), pol)
    httpd.daemon_threads = True
    return httpd, api


def serve(cfg: Config, port: int, demo: bool = False, host: str = "127.0.0.1") -> None:
    httpd, api = build(cfg, port, demo, host)
    print(f"[INFO] Ultra listening on http://{host}:{port}", flush=True)
    if not guard.loopback_peer(host):
        pol = httpd.RequestHandlerClass.peer_policy  # type: ignore[attr-defined]
        print(f"[WARN] Remote access on for: {', '.join(pol.specs)}", flush=True)
        if pol.tailnet:
            own = ", ".join(f"{n} {ip}" for ip, n in pol.own().items() if "." in ip)
            print(f"[INFO] Own tailnet devices: {own or 'none found'}", flush=True)
    if api.live is not None:
        api.live.warm()
    if not demo:
        from ultra.config import list_workspaces

        names = [w["name"] for w in list_workspaces(cfg)]
        if len(names) > 1:
            print(f"[INFO] Workspaces: {', '.join(names)}", flush=True)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()
