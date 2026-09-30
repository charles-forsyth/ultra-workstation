"""Research adapter: a client of the `deep-research` CLI (SPEC 7.9).

Only `--json` commands, argv lists, no shell. Ultra never reads deep-research's
database or files. Starting a run costs money, so `start` is only reachable from the
launcher's Start button, and the thread text is included only when the operator ticks
"Include this thread" for that run (written to a mode-600 temp file, removed when the
run finishes or after a day).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from ultra.config import Config, data_dir, private_dir
from ultra.store import Store

ALLOWED = {"search", "list", "show", "estimate", "start"}
MAX_PROMPT = 8000


class ResearchError(Exception):
    pass


class Research:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        self.binary = str(cfg.get("research", "binary", "deep-research"))
        self.enabled = bool(cfg.get("research", "enabled", True)) and bool(
            shutil.which(self.binary)
        )
        self.dashboard_url = str(cfg.get("research", "dashboard_url", "") or "")
        self.depth = int(cfg.get("research", "default_depth", 1))
        self.breadth = int(cfg.get("research", "default_breadth", 3))
        self.sem = threading.Semaphore(2)
        self.state: dict[str, Any] = {"ok": None, "error": ""}

    def _run(self, args: list[str], timeout: int = 180) -> Any:
        if args[0] not in ALLOWED:
            raise ResearchError(f"not an allowed research command: {args[0]}")
        if not self.enabled:
            raise ResearchError("deep-research CLI not found")
        env = dict(os.environ)
        # The CLI loads its own key from ~/.config/deepresearch/.env; never hand it ours.
        env.pop("GEMINI_API_KEY", None)
        # --json must come before any "--" (after it, everything is the prompt)
        if "--" in args:
            i = args.index("--")
            argv = [self.binary, *args[:i], "--json", *args[i:]]
        else:
            argv = [self.binary, *args, "--json"]
        with self.sem:
            r = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                stdin=subprocess.DEVNULL,
                env=env,
            )
        out = (r.stdout or "").strip()
        try:
            data = json.loads(out) if out else None
        except ValueError as e:
            self.state = {"ok": False, "error": "bad JSON from deep-research"}
            raise ResearchError(f"deep-research {args[0]}: output was not JSON") from e
        if r.returncode != 0 or (isinstance(data, dict) and data.get("error")):
            msg = (data or {}).get("error") if isinstance(data, dict) else None
            msg = msg or (r.stderr or "").strip().splitlines()[-1:] or ["failed"]
            self.state = {"ok": False, "error": str(msg)[:200]}
            raise ResearchError(str(msg if isinstance(msg, str) else msg[0])[:400])
        self.state = {"ok": True, "error": ""}
        return data

    # ---------------------------------------------------------------- reads
    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        q = " ".join(query.split())[:500]
        if len(q) < 2:
            raise ResearchError("query is too short")
        return self._run(["search", "--limit", str(max(1, min(limit, 10))), "--", q], timeout=180)

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        d = self._run(["list", "--limit", str(max(1, min(limit, 100)))], timeout=60)
        return d if isinstance(d, list) else []

    def show(self, sid: int) -> dict[str, Any]:
        d = self._run(["show", str(int(sid))], timeout=60)
        return d if isinstance(d, dict) else {}

    def estimate(self, prompt: str, depth: int, breadth: int, upload: Path | None) -> Any:
        args = ["estimate", "--depth", str(depth), "--breadth", str(breadth)]
        if upload:
            args += ["--upload", str(upload)]
        return self._run([*args, "--", prompt[:MAX_PROMPT]], timeout=60)

    # ---------------------------------------------------------------- start (costs money)
    def start(self, prompt: str, depth: int, breadth: int, thread_text: str | None) -> Any:
        prompt = prompt.strip()
        if len(prompt) < 10:
            raise ResearchError("write a fuller question (at least 10 characters)")
        if len(prompt) > MAX_PROMPT:
            raise ResearchError(f"question is over {MAX_PROMPT} characters")
        if not (1 <= depth <= 3 and 1 <= breadth <= 5):
            raise ResearchError("depth must be 1-3 and breadth 1-5")
        upload = self._thread_file(thread_text) if thread_text else None
        args = ["start", "--depth", str(depth), "--breadth", str(breadth)]
        if upload:
            args += ["--upload", str(upload)]
        d = self._run([*args, "--", prompt], timeout=120)
        sid = (d or {}).get("session_id")
        self.store.journal(
            "research_start",
            str(sid),
            bool(sid),
            {
                "depth": depth,
                "breadth": breadth,
                "thread_included": bool(upload),
                "chars": len(prompt),
            },
        )
        if upload and sid:
            self.store.cache_put(f"research:upload:{sid}", str(upload))
        return d

    def _thread_file(self, text: str) -> Path:
        folder = private_dir(data_dir() / "research-uploads")
        fd, name = tempfile.mkstemp(prefix="thread-", suffix=".txt", dir=folder)
        with os.fdopen(fd, "w") as f:
            f.write(text[:1_000_000])
        os.chmod(name, 0o600)
        return Path(name)

    def cleanup_uploads(self, max_age: float = 86400) -> int:
        """Remove thread files for finished runs, and anything older than a day."""
        folder = data_dir() / "research-uploads"
        if not folder.exists():
            return 0
        n = 0
        finished = set()
        try:
            for r in self.runs(50):
                if r.get("status") in ("completed", "failed", "cancelled"):
                    hit = self.store.cache_get(f"research:upload:{r['id']}")
                    if hit:
                        finished.add(hit[0])
        except ResearchError:
            pass
        for p in folder.glob("thread-*.txt"):
            if str(p) in finished or time.time() - p.stat().st_mtime > max_age:
                p.unlink(missing_ok=True)
                n += 1
        return n


def report_text(show: dict[str, Any]) -> str:
    """The report Markdown from `show --json` (field name differs by version)."""
    for k in ("report", "result", "markdown"):
        v = show.get(k)
        if isinstance(v, str) and v.strip():
            return v
    return ""


def safe_run_id(v: Any) -> int:
    s = str(v)
    if not re.fullmatch(r"\d{1,9}", s):
        raise ResearchError("bad run id")
    return int(s)
