"""Client for `nexus serve` (the ledger's warm local service), with a CLI fallback.

The ledger CLI costs 3-8 s per call to start. `nexus serve` runs the same commands
warm (SPEC 8.6). This client:

- talks only to a loopback address (it refuses anything else);
- reads the bearer token from the ledger's own token file (never stored here);
- returns the same (exit code, stdout, stderr) shape as running the CLI, so the
  callers' allow-lists, parsing and read-backs stay exactly as they are;
- falls back to the CLI when serve is not running or not answering. A request that
  reached serve is never re-run on the CLI (a write could happen twice);
- never handles serve's delete confirmations: Ultra does not delete through it.
"""

from __future__ import annotations

import ipaddress
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from ultra.config import Config, expand

DEFAULT_URL = "http://127.0.0.1:7450"
DEFAULT_TOKEN_FILE = "~/.config/nexus/serve.token"  # noqa: S105 - a path, not a secret
RETRY_AFTER = 30.0  # seconds before trying serve again after it failed to answer


class ServeUnavailable(Exception):
    """Serve did not take the request (not running, refused, wrong token). Safe to
    fall back to the CLI: nothing ran."""


class ServeError(Exception):
    """Serve took the request but the outcome is unknown (timeout mid-run, bad reply).
    Do NOT fall back: the command may have run."""


def _loopback_url(url: str) -> str:
    p = urllib.parse.urlparse(url)
    host = p.hostname or ""
    try:
        ok = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        ok = False
    if p.scheme != "http" or not ok:
        raise ValueError(f"ledger serve URL must be http on loopback, not {url!r}")
    return f"http://{host}:{p.port or 7450}"


class ServeClient:
    def __init__(self, cfg: Config):
        self.enabled = bool(cfg.get("ledger", "use_serve", True))
        self.url = _loopback_url(str(cfg.get("ledger", "serve_url", DEFAULT_URL)))
        self.token_file = expand(str(cfg.get("ledger", "serve_token_file", DEFAULT_TOKEN_FILE)))
        self._down_until = 0.0
        self._lock = threading.Lock()
        self.last: dict[str, Any] = {"via": None, "ms": None, "error": ""}

    def _token(self) -> str:
        try:
            tok = Path(self.token_file).read_text().strip()
        except OSError as e:
            raise ServeUnavailable(f"no serve token ({e.__class__.__name__})") from e
        if len(tok) < 32:
            raise ServeUnavailable("serve token file is empty or short")
        return tok

    def available(self) -> bool:
        return self.enabled and time.monotonic() >= self._down_until

    def mark_down(self, why: str) -> None:
        with self._lock:
            self._down_until = time.monotonic() + RETRY_AFTER
            self.last = {"via": "cli", "ms": None, "error": why[:200]}

    def health(self) -> dict[str, Any] | None:
        try:
            # self.url is checked to be http://<loopback> in __init__
            req = urllib.request.Request(self.url + "/api/health")  # noqa: S310
            req.add_header("Authorization", f"Bearer {self._token()}")
            with urllib.request.urlopen(req, timeout=2) as r:  # noqa: S310
                d = json.loads(r.read() or b"{}")
                return d if isinstance(d, dict) else None
        except Exception:  # noqa: BLE001 - health is advisory
            return None

    def run(self, args: list[str], stdin: str = "", timeout: float = 240) -> tuple[int, str, str]:
        """Run `nexus <args>` on serve. Returns (exit_code, stdout, stderr)."""
        if not self.available():
            raise ServeUnavailable("serve marked down")
        body = json.dumps({"args": list(args), "stdin": stdin, "wait": timeout}).encode()
        req = urllib.request.Request(self.url + "/api/run", data=body, method="POST")  # noqa: S310
        req.add_header("Authorization", f"Bearer {self._token()}")
        req.add_header("Content-Type", "application/json")
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout + 15) as r:  # noqa: S310
                status, raw = r.status, r.read()
        except urllib.error.HTTPError as e:
            status, raw = e.code, e.read()
        except (ConnectionRefusedError, urllib.error.URLError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(
                reason, (ConnectionRefusedError, FileNotFoundError, OSError)
            ) and not isinstance(reason, TimeoutError):
                self.mark_down(f"not answering: {reason}")
                raise ServeUnavailable(str(reason)) from e
            raise ServeError(f"no reply from ledger serve: {reason}") from e
        except TimeoutError as e:
            raise ServeError("ledger serve timed out; the command may still be running") from e
        try:
            d = json.loads(raw or b"{}")
        except ValueError as e:
            raise ServeError(f"bad reply from ledger serve (HTTP {status})") from e
        if status in (401, 403, 404, 405, 409, 413, 415) or (status == 400 and "job" not in d):
            # refused before anything ran (auth, policy, shape): the CLI may still do it
            if status == 401:
                self.mark_down("serve refused the token")
            raise ServeUnavailable(f"serve refused (HTTP {status}): {d.get('error', '')}")
        if status == 202 or not d.get("done", True):
            raise ServeError(
                f"ledger serve is still running this command (job {d.get('job', '?')}); not retried"
            )
        if status != 200 or "exit_code" not in d or not isinstance(d["exit_code"], int):
            raise ServeError(f"unexpected reply from ledger serve (HTTP {status})")
        self.last = {"via": "serve", "ms": int((time.monotonic() - t0) * 1000), "error": ""}
        return int(d["exit_code"]), str(d.get("stdout") or ""), str(d.get("stderr") or "")
