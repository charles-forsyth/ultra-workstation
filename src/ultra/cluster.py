"""Cluster facts in the mail loop (SPEC 8.9, v1.8): the cluster MCP server, read only.

When an email or ticket mentions a Slurm job ("job 315", "JobId=315", "slurm-315.out"),
the thread shows a Cluster chip. Opening it calls the cluster MCP server (bifrost,
`[mcp.ursa]`) as Ultra's program client, which holds the R1 + R2 read tiers only:

- `job_show_any`   state, times, efficiency, partition, the log paths
- `job_explain_any` the server's deterministic diagnosis (rules, evidence, fix)
- `ticket_draft`   a reply built from those findings, with what happened and evidence
- `job_log_tail`, through `job_results`... are R1-own-jobs only, so the full log comes
  from `job_explain_any(lines=N)` (Q7: full logs are fine for the operator, an admin).

Plus `script_check` for a batch script pasted in mail (selection or a code block).

Nothing here submits, cancels or holds anything: the client has no A1 tools, and the
allow-list below names read tools only. A ticket_draft only ever becomes a new AI
version of the reply-all draft, which still needs both approvals.
Text from the cluster (logs, job names, submit lines, ticket text) is data, shown as
text and never used as instructions.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from ultra.mcpclient import McpAuthNeeded, McpClient, McpError, McpUnreachable

# Job ids as they appear in support mail. A bare number is never a job id: it needs a
# word that says so ("job", "jobid", "job id", "job #") or the Slurm log name.
JOB_RE = re.compile(
    r"(?ix)"
    r"(?:\bjob\s*(?:id|\#|number|no\.?)?\s*[:=#]?\s*|\bjobid\s*=\s*|\bslurm-)"
    r"(\d{1,9})(?!\d|\.\d)"
)
SCRIPT_RE = re.compile(r"(?m)^#SBATCH\s+\S")
MAX_JOBS = 5

READ_TOOLS = frozenset(
    {"job_show_any", "job_explain_any", "ticket_draft", "script_check", "cluster_status", "health"}
)
TTL = 60  # seconds: a job's state changes, but not within one look at a thread


class ClusterError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def job_ids(text: str) -> list[str]:
    """Distinct job ids mentioned in the text, in order, at most MAX_JOBS."""
    out: list[str] = []
    for m in JOB_RE.finditer(text or ""):
        j = m.group(1).lstrip("0") or "0"
        if j != "0" and j not in out:
            out.append(j)
        if len(out) >= MAX_JOBS:
            break
    return out


def _issue_text(i: Any) -> str:
    return str(i.get("message", i) if isinstance(i, dict) else i)[:200]


def has_script(text: str) -> bool:
    return len(SCRIPT_RE.findall(text or "")) >= 1


class Cluster:
    def __init__(self, client: McpClient | None):
        self.client = client
        self.enabled = client is not None
        self.lock = threading.Lock()
        self.cache: dict[tuple[str, str], tuple[float, Any]] = {}

    def _call(self, tool: str, args: dict[str, Any]) -> Any:
        if tool not in READ_TOOLS:  # defence in depth: the client has no write tier either
            raise ClusterError(400, f"{tool} is not a read tool Ultra uses")
        if self.client is None:
            raise ClusterError(503, "the cluster server is not set up: [mcp.ursa] in config")
        key = (tool, repr(sorted(args.items())))
        now = time.time()
        with self.lock:
            hit = self.cache.get(key)
            if hit and now - hit[0] < TTL:
                return hit[1]
        try:
            res = self.client.call(tool, args)
        except McpAuthNeeded as e:
            raise ClusterError(401, str(e)) from e
        except McpUnreachable as e:
            raise ClusterError(503, f"cannot reach the cluster server: {e}") from e
        except McpError as e:
            raise ClusterError(502, str(e)) from e
        data = res.get("data", res) if isinstance(res, dict) else res
        with self.lock:
            if len(self.cache) > 200:
                self.cache.clear()
            self.cache[key] = (now, data)
        return data

    # -- what Ultra shows
    def job(self, job_id: str, ticket_text: str = "", lines: int = 40) -> dict[str, Any]:
        """One job, explained, with a reply draft. Read only."""
        if not re.fullmatch(r"\d{1,9}", job_id or ""):
            raise ClusterError(400, "job id must be digits")
        lines = max(10, min(int(lines or 40), 400))
        show = self._call("job_show_any", {"job_id": job_id})
        explain = self._call("job_explain_any", {"job_id": job_id, "lines": lines})
        draft = None
        if ticket_text.strip():
            try:
                draft = self._call(
                    "ticket_draft", {"job_id": job_id, "ticket_text": ticket_text[:4000]}
                )
            except ClusterError:
                draft = None  # the facts above still help
        return {"job_id": job_id, "show": show, "explain": explain, "draft": draft}

    def check_script(self, script: str) -> Any:
        s = (script or "").strip()
        if not s:
            raise ClusterError(400, "no script")
        if len(s) > 20_000:
            raise ClusterError(400, "script is over 20000 characters")
        return self._call("script_check", {"script": s})

    def health(self) -> dict[str, Any]:
        """One line for Today: problems on the cluster right now (staff health tool).
        Cached for 5 minutes; a failure reads as "unknown", never as healthy."""
        now = time.time()
        with self.lock:
            hit = self.cache.get(("health", "line"))
            if hit and now - hit[0] < 300:
                return hit[1]
        try:
            d = self._call("health", {})
            issues = d.get("issues") or []
            line = {
                "ok": bool(d.get("ok")) and not issues,
                "issues": [_issue_text(i) for i in issues][:5],
                "failure_rate_24h_percent": d.get("failure_rate_24h_percent"),
                "jobs_ended_24h": d.get("jobs_ended_24h"),
            }
        except ClusterError as e:
            line = {"ok": None, "error": str(e)[:200], "issues": []}
        with self.lock:
            self.cache[("health", "line")] = (now, line)
        return line

    def status(self) -> dict[str, Any]:
        st = getattr(self.client, "state", {}) if self.client else {}
        return {"enabled": self.enabled, "ok": st.get("ok"), "error": st.get("error", "")}


def thread_text(messages: list[dict[str, Any]]) -> str:
    """The text a job id is looked for in: bodies and subjects, newest last."""
    return "\n".join(f"{m.get('subject', '')}\n{m.get('body', '')}" for m in messages or [])


def with_cluster(thread: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Add what the thread view needs to show the Cluster chip (job ids, a script)."""
    if not enabled or not isinstance(thread, dict):
        return thread
    text = thread_text(thread.get("messages") or [])
    jobs = job_ids(text)
    script = has_script(text)
    if jobs or script:
        thread = {**thread, "cluster": {"jobs": jobs, "script": script}}
    return thread


class ClusterApi:
    """HTTP routes: GET /api/cluster/status, POST /api/cluster/job, POST /api/cluster/script."""

    def __init__(self, cluster: Cluster, store: Any = None):
        self.cluster = cluster
        self.store = store

    def register(self, api: Any) -> None:
        api.add("GET", r"/api/cluster/status", self.r_status)
        api.add("GET", r"/api/cluster/health", self.r_health)
        api.add("POST", r"/api/cluster/job", self.r_job)
        api.add("POST", r"/api/cluster/script", self.r_script)

    def _err(self, e: ClusterError) -> Exception:
        from ultra.server import ApiError

        return ApiError(e.status, str(e))

    def r_status(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return self.cluster.status()

    def r_health(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        if not self.cluster.enabled:
            return {"enabled": False}
        return {"enabled": True, **self.cluster.health()}

    def r_job(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        try:
            out = self.cluster.job(
                str(b.get("job_id", "")), str(b.get("ticket_text", "")), int(b.get("lines") or 40)
            )
        except ClusterError as e:
            raise self._err(e) from e
        except (TypeError, ValueError) as e:
            raise self._err(ClusterError(400, "bad request")) from e
        if self.store is not None:
            self.store.journal("cluster_job", out["job_id"], True, {"draft": bool(out["draft"])})
        return out

    def r_script(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            return {"check": self.cluster.check_script(str((body or {}).get("script", "")))}
        except ClusterError as e:
            raise self._err(e) from e
