"""Home: the personal notes vault (SPEC 8.10, v1.10; writes v1.12).

v1.12: the default server is vault-mcp (the operator's own Go server; `kind =
"vault-mcp"`, detected from the command name). Reads map onto its tools
(`vault_search`, `vault_read`, `vault_tasks`, `vault_status`); the Log and Task card in a
workspace without Nexus writes through `write()`, which allows only `vault_log`,
`vault_log_note` and `vault_task_add` (append or create, one git commit each, done by
the server). `writes = false` in `[vault]` starts the server read-only. The older
headless-obsidian-mcp (`kind = "headless"`) still works for reads, as below.

Ultra runs a local MCP server over the operator's Obsidian vault (`[vault]` in config,
by default `headless-obsidian-mcp`, a stdio server that reads the markdown on disk) and
uses a handful of its read tools:

- `search_notes_ranked`  palette search ("Search the vault for ...")
- `read_notes`           open one note, read only
- `list_tasks`           open checkbox todos; the Home line on Today shows the dated ones
- `list_recent_notes`    recently changed notes
- `get_vault_stats`      the status check

Nothing here writes: the allow-list names read tools only, and the server is always
started with `OBSIDIAN_TOOLS=reads`, so its write tools are not even listed. Note text
is data, shown as text, never used as instructions. Folders the server cannot see (a
symlink out of the vault, dot folders) stay invisible; `exclude` in config hides more
(archive and template folders) from the Home line and search results.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from ultra.config import Config, expand

READ_TOOLS = frozenset(
    {"search_notes_ranked", "read_notes", "list_tasks", "list_recent_notes", "get_vault_stats"}
    | {"vault_search", "vault_read", "vault_tasks", "vault_recent", "vault_status", "vault_day"}
)
# v1.12: the only tools write() may call, and only on vault-mcp (append or create)
WRITE_TOOLS = frozenset({"vault_log", "vault_log_note", "vault_task_add"})
TASKS_NOTE = "01 - Hubs/Tasks and Todos.md"
TTL = 60  # seconds: notes change, but not within one look
DUE_RE = re.compile(r"(?:\U0001F4C5|\bdue:?)\s*(\d{4}-\d{2}-\d{2})")
PATH_RE = re.compile(r"^[^\x00]{1,400}$")
SOON_DAYS = 7


class VaultError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


class StdioMcp:
    """A minimal MCP client over a child process's stdin/stdout (newline-delimited
    JSON-RPC). One process for the app; restarted if it dies. Thread-safe."""

    def __init__(self, argv: list[str], env: dict[str, str], timeout: float = 30):
        self.argv = argv
        self.env = env
        self.timeout = timeout
        self.lock = threading.Lock()
        self.proc: subprocess.Popen[bytes] | None = None
        self.out: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._ids = 0

    def _reader(self, proc: subprocess.Popen[bytes]) -> None:
        if proc.stdout is None:
            return
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict):
                self.out.put(msg)
        self.out.put(None)  # the process ended

    def _start(self) -> None:
        self.out = queue.Queue()
        self.proc = subprocess.Popen(
            self.argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=self.env,
        )
        threading.Thread(target=self._reader, args=(self.proc,), daemon=True).start()
        self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "ultra-workstation", "version": "1"},
            },
        )
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _send(self, msg: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise VaultError(503, "the vault server is not running")
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        self.proc.stdin.flush()

    def _rpc(self, method: str, params: dict[str, Any]) -> Any:
        self._ids += 1
        rid = self._ids
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self.close()
                raise VaultError(504, f"the vault server did not answer {method} in time")
            try:
                msg = self.out.get(timeout=left)
            except queue.Empty:
                continue
            if msg is None:
                self.proc = None
                raise VaultError(503, "the vault server stopped")
            if msg.get("id") != rid:
                continue  # a notification or a late answer
            if "error" in msg:
                err = msg["error"]
                raise VaultError(
                    502, str(err.get("message", err) if isinstance(err, dict) else err)
                )
            return msg.get("result")

    def call(self, tool: str, args: dict[str, Any]) -> Any:
        with self.lock:
            try:
                if self.proc is None or self.proc.poll() is not None:
                    self._start()
                res = self._rpc("tools/call", {"name": tool, "arguments": args})
            except (OSError, BrokenPipeError) as e:
                self.close()
                raise VaultError(503, f"cannot run the vault server: {e}") from e
        if not isinstance(res, dict):
            raise VaultError(502, f"{tool} returned no result")
        text = "\n".join(
            str(p.get("text", ""))
            for p in res.get("content") or []
            if isinstance(p, dict) and p.get("type") == "text"
        )
        if res.get("isError"):
            raise VaultError(400, text[:300] or f"{tool} failed")
        if isinstance(res.get("structuredContent"), dict):
            return res["structuredContent"]
        try:
            return json.loads(text)
        except ValueError:
            return text

    def close(self) -> None:
        p, self.proc = self.proc, None
        if p is not None:
            try:
                p.kill()
                p.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass


def server_kind(sec: dict[str, Any]) -> str:
    """vault-mcp (the operator's server) or headless (headless-obsidian-mcp)."""
    k = str(sec.get("kind") or "").strip()
    if k in ("vault-mcp", "headless"):
        return k
    return "vault-mcp" if Path(str(sec.get("command") or "")).name == "vault-mcp" else "headless"


def writes_on(sec: dict[str, Any]) -> bool:
    return server_kind(sec) == "vault-mcp" and sec.get("writes", True) is not False


def client_from_config(cfg: Config) -> StdioMcp | None:
    sec = cfg.section("vault")
    if not sec.get("path") or sec.get("enabled", True) is False:
        return None
    vault = expand(str(sec["path"]))
    command = str(expand(str(sec.get("command") or "node")))
    args = [
        str(expand(str(a))) if str(a).startswith("~") else str(a) for a in sec.get("args") or []
    ]
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", ""),
    }
    if server_kind(sec) == "vault-mcp":
        env["VAULT_PATH"] = str(vault)
        if sec.get("author"):
            env["VAULT_AUTHOR"] = str(sec["author"])  # git author name; vault-mcp has a default
        if not writes_on(sec):
            env["VAULT_READ_ONLY"] = "1"  # the server then lists no write tools at all
    else:
        env["OBSIDIAN_VAULT_PATH"] = str(vault)
        env["OBSIDIAN_TOOLS"] = "reads"  # always: Ultra never asks for its write tools
    return StdioMcp([command, *args], env, timeout=float(sec.get("timeout", 30)))


def obsidian_url(vault_name: str, path: str) -> str:
    from urllib.parse import quote

    return f"obsidian://open?vault={quote(vault_name)}&file={quote(path)}"


class Vault:
    def __init__(self, cfg: Config, client: StdioMcp | None, today_fn: Any = None):
        self.client = client
        self.enabled = client is not None
        sec = cfg.section("vault")
        self.name = str(
            sec.get("name") or (Path(str(sec.get("path", ""))).name if sec.get("path") else "")
        )
        self.exclude = [
            str(x).strip("/") + "/" for x in sec.get("exclude") or [] if str(x).strip("/")
        ]
        self.today_fn = today_fn or dt.date.today
        self.kind = server_kind(sec)
        self.writes = self.enabled and writes_on(sec)
        self.via = str(sec.get("via") or "Ultra")
        self.lock = threading.Lock()
        self.cache: dict[tuple[str, str], tuple[float, Any]] = {}
        self.state: dict[str, Any] = {"ok": None, "error": "", "at": None}

    def _hidden(self, path: str) -> bool:
        return any(path.startswith(x) for x in self.exclude)

    def _call(self, tool: str, args: dict[str, Any], ttl: float = TTL) -> Any:
        if tool not in READ_TOOLS:  # defence in depth: the server runs with reads only too
            raise VaultError(400, f"{tool} is not a read tool Ultra uses")
        if self.client is None:
            raise VaultError(503, "the vault is not set up: [vault] in config")
        key = (tool, json.dumps(args, sort_keys=True))
        now = time.time()
        with self.lock:
            hit = self.cache.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        try:
            res = self.client.call(tool, args)
        except VaultError as e:
            self.state = {"ok": False, "error": str(e)[:200], "at": now}
            raise
        self.state = {"ok": True, "error": "", "at": now}
        with self.lock:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[key] = (now, res)
        return res

    # -- what Ultra shows
    def search(self, q: str, limit: int = 20) -> list[dict[str, Any]]:
        q = (q or "").strip()
        if not q:
            raise VaultError(400, "empty search")
        n = min(max(limit, 1), 50) + 10
        if self.kind == "vault-mcp":
            res = self._call("vault_search", {"query": q[:300], "limit": n})
        else:
            res = self._call("search_notes_ranked", {"query": q[:300], "limit": n})
        rows = res.get("results", []) if isinstance(res, dict) else []
        out = [
            {
                "path": str(r.get("path", "")),
                "title": str(r.get("headline") or r.get("title") or r.get("path", "")),
                "snippet": str(r.get("snippet", ""))[:300],
                "modified": r.get("modified"),
            }
            for r in rows
            if isinstance(r, dict) and not self._hidden(str(r.get("path", "")))
        ]
        return out[:limit]

    def note(self, path: str) -> dict[str, Any]:
        if not PATH_RE.match(path or "") or ".." in path.split("/") or path.startswith("/"):
            raise VaultError(400, "bad note path")
        if self._hidden(path):
            raise VaultError(404, "that note is hidden from Ultra ([vault] exclude)")
        if self.kind == "vault-mcp":
            n = self._call("vault_read", {"path": path})
            if not isinstance(n, dict) or not n.get("path"):
                raise VaultError(404, "note not found")
            return {
                "path": str(n["path"]),
                "contents": str(n.get("contents", ""))[:200_000],
                "tags": [],
                "open_url": obsidian_url(self.name, str(n["path"])) if self.name else "",
            }
        res = self._call("read_notes", {"paths": [path]})
        notes = res.get("notes", []) if isinstance(res, dict) else []
        if not notes:
            errs = res.get("errors", []) if isinstance(res, dict) else []
            raise VaultError(404, str(errs[0] if errs else "note not found")[:200])
        n = notes[0]
        return {
            "path": str(n.get("path", path)),
            "contents": str(n.get("contents", ""))[:200_000],
            "tags": n.get("tags") or [],
            "open_url": obsidian_url(self.name, str(n.get("path", path))) if self.name else "",
        }

    def todos(self) -> dict[str, Any]:
        """Open dated todos: overdue and due within a week, plus the undated count."""
        if self.kind == "vault-mcp":
            res = self._call("vault_tasks", {})
        else:
            res = self._call("list_tasks", {"status": ["open"], "limit": 0})
        rows = res.get("results", []) if isinstance(res, dict) else []
        today = self.today_fn()
        dated: list[dict[str, Any]] = []
        undated = 0
        for r in rows:
            if not isinstance(r, dict) or self._hidden(str(r.get("path", ""))):
                continue
            text = str(r.get("text", ""))
            m = DUE_RE.search(text)
            if not m:
                undated += 1
                continue
            try:
                due = dt.date.fromisoformat(m.group(1))
            except ValueError:
                undated += 1
                continue
            dated.append(
                {
                    "text": DUE_RE.sub("", text).strip()[:200],
                    "due": due.isoformat(),
                    "days": (due - today).days,
                    "path": str(r.get("path", "")),
                    "line": r.get("line"),
                }
            )
        dated.sort(key=lambda x: x["due"])
        overdue = [x for x in dated if x["days"] < 0]
        soon = [x for x in dated if 0 <= x["days"] <= SOON_DAYS]
        later = [x for x in dated if x["days"] > SOON_DAYS]
        return {"overdue": overdue, "soon": soon, "later": later, "undated": undated}

    def health(self) -> dict[str, Any]:
        try:
            s = self._call(
                "vault_status" if self.kind == "vault-mcp" else "get_vault_stats", {}, ttl=300
            )
            return {"ok": True, "notes": s.get("notes") if isinstance(s, dict) else None}
        except VaultError as e:
            return {"ok": False, "error": str(e)[:200]}

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "name": self.name,
            "kind": self.kind,
            "writes": self.writes,
            **self.state,
        }

    # -- writes (v1.12): the Log and Task card in a workspace without Nexus
    def write(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """One write through vault-mcp. Never retried: an unknown outcome is reported,
        and the server's own git log is the record of what happened."""
        if tool not in WRITE_TOOLS:
            raise VaultError(400, f"{tool} is not a write Ultra makes")
        if not self.writes or self.client is None:
            raise VaultError(403, "vault writes are off ([vault] writes, or not vault-mcp)")
        res = self.client.call(tool, {**args, "via": self.via})
        with self.lock:
            self.cache.clear()  # todos and search may have changed
        if not isinstance(res, dict) or not res.get("path"):
            raise VaultError(502, f"{tool} returned no result")
        return res

    def write_options(self) -> dict[str, Any]:
        """What the card's Where picker offers: Journal topics and task sections."""
        topics: list[str] = []
        sections: list[str] = []
        try:
            st = self._call("vault_status", {}, ttl=300)
            topics = [str(t) for t in (st.get("log_topics") or [])] if isinstance(st, dict) else []
            n = self._call("vault_read", {"path": TASKS_NOTE}, ttl=60)
            for line in str(n.get("contents", "") if isinstance(n, dict) else "").splitlines():
                if line.startswith("## ") and "COMPLETED" not in line.upper():
                    sections.append(line[3:].strip())
        except VaultError as e:
            return {"topics": topics, "sections": sections, "error": str(e)[:200]}
        return {"topics": topics, "sections": sections}


class VaultApi:
    """GET /api/vault/status, /api/vault/home, /api/vault/search, /api/vault/note."""

    def __init__(self, vault: Vault, store: Any = None):
        self.vault = vault
        self.store = store

    def register(self, api: Any) -> None:
        api.add("GET", r"/api/vault/status", self.r_status)
        api.add("GET", r"/api/vault/home", self.r_home)
        api.add("GET", r"/api/vault/search", self.r_search)
        api.add("GET", r"/api/vault/note", self.r_note)

    def _err(self, e: VaultError) -> Exception:
        from ultra.server import ApiError

        return ApiError(e.status, str(e))

    def r_status(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self.vault.status()

    def r_home(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        if not self.vault.enabled:
            return {"enabled": False}
        try:
            return {"enabled": True, **self.vault.todos()}
        except VaultError as e:
            return {"enabled": True, "error": str(e)[:200]}

    def r_search(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            return {"results": self.vault.search((q.get("q") or [""])[0])}
        except VaultError as e:
            raise self._err(e) from e

    def r_note(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            return self.vault.note((q.get("path") or [""])[0])
        except VaultError as e:
            raise self._err(e) from e
