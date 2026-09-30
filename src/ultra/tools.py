"""v0.45 routes: research panel, web search, explain, AI audio (SPEC 7.8-7.10).

Shared by live and demo. Start-a-research-run is the only action here that spends
real money on a long job, so it needs an explicit confirm flag from the launcher
dialog, and the thread text is included only when the operator ticks it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any

from ultra.audio import Audio, AudioError
from ultra.research import ResearchError, report_text, safe_run_id
from ultra.server import FileResponse

if TYPE_CHECKING:
    from ultra.server import Api


def _bad(msg: str, status: int = 400) -> Exception:
    from ultra.server import ApiError

    return ApiError(status, msg)


class Tools:
    def __init__(
        self,
        research: Any,
        ai: Any,
        audio: Audio,
        thread_text: Callable[..., str],
        operator: str = "",
    ):
        self.research = research
        self.ai = ai
        self.audio = audio
        self.thread_text = thread_text
        self.operator = operator
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ultra-tools")

    def register(self, api: Api) -> None:
        api.add("GET", r"/api/research/status", self.r_status)
        api.add("POST", r"/api/research/search", self.r_search)
        api.add("GET", r"/api/research/runs", self.r_runs)
        api.add("GET", r"/api/research/show/(\d{1,9})", self.r_show)
        api.add("POST", r"/api/research/estimate", self.r_estimate)
        api.add("POST", r"/api/research/start", self.r_start)
        api.add("POST", r"/api/ai/web", self.r_web)
        api.add("POST", r"/api/ai/explain", self.r_explain)
        api.add("POST", r"/api/audio/estimate", self.r_audio_estimate)
        api.add("POST", r"/api/audio/make", self.r_audio_make)
        api.add("GET", r"/api/audio/job/([0-9a-f]{24})", self.r_audio_job)
        api.add("GET", r"/api/audio/file/([0-9a-f]{24})", self.r_audio_file)

    # ---------------------------------------------------------------- research
    def _r(self, fn: Callable[..., Any], *a: Any) -> Any:
        try:
            return fn(*a)
        except ResearchError as e:
            raise _bad(str(e), 502) from e

    def r_status(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        return {
            "enabled": self.research.enabled,
            "dashboard_url": self.research.dashboard_url,
            "depth": self.research.depth,
            "breadth": self.research.breadth,
            **self.research.state,
        }

    def r_search(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        return self._r(self.research.search, str(b.get("q", "")), int(b.get("limit", 5)))

    def r_runs(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        runs = self._r(self.research.runs, 20)
        return {
            "runs": [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "prompt",
                        "status",
                        "created_at",
                        "updated_at",
                        "depth",
                        "parent_id",
                        "result_chars",
                    )
                }
                for r in runs
            ]
        }

    def r_show(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        d = self._r(self.research.show, safe_run_id(m.group(1)))
        return {
            "id": d.get("id"),
            "prompt": d.get("prompt", ""),
            "status": d.get("status"),
            "created_at": d.get("created_at"),
            "report": report_text(d),
        }

    def _launch_args(self, b: dict[str, Any]) -> tuple[str, int, int, str | None]:
        prompt = str(b.get("prompt", "")).strip()
        depth = int(b.get("depth", self.research.depth))
        breadth = int(b.get("breadth", self.research.breadth))
        thread = None
        if b.get("include_thread") is True and b.get("thread"):
            key = str(b["thread"])
            if not re.fullmatch(r"(g|k|s)-[A-Za-z0-9_-]{1,80}", key):
                raise _bad("bad thread key")
            thread = self.thread_text(key)
        return prompt, depth, breadth, thread

    def r_estimate(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        prompt, depth, breadth, thread = self._launch_args(body or {})
        if len(prompt) < 3:
            raise _bad("write the question first")
        up = self.research._thread_file(thread) if thread else None
        try:
            est = self._r(self.research.estimate, prompt, depth, breadth, up)
        finally:
            if up:
                up.unlink(missing_ok=True)
        return {**(est or {}), "thread_chars": len(thread or "")}

    def r_start(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        if b.get("confirm") is not True:
            raise _bad("Start needs confirm: true (from the launcher dialog)")
        prompt, depth, breadth, thread = self._launch_args(b)
        return self._r(self.research.start, prompt, depth, breadth, thread)

    # ---------------------------------------------------------------- AI helpers
    def r_web(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        ctx = ""
        if b.get("include_thread") is True and b.get("thread"):
            ctx = self.thread_text(str(b["thread"]))
        try:
            return self.ai.web_search(str(b.get("q", "")), ctx)
        except Exception as e:
            raise _bad(str(e), 502) from e

    def r_explain(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        passage = str(b.get("text", "")).strip()
        if not passage:
            raise _bad("select some text first")
        ctx = self.thread_text(str(b["thread"])) if b.get("thread") else ""
        try:
            r = self.ai.explain(passage, ctx, self.operator)
        except Exception as e:
            raise _bad(str(e), 502) from e
        return {"text": r.text, "model": r.model, "seconds": r.seconds}

    # ---------------------------------------------------------------- audio
    def _audio_text(self, b: dict[str, Any]) -> tuple[str, str]:
        """Text to voice: explicit text, a thread, or a research report."""
        if b.get("text"):
            return str(b["text"])[:400_000], str(b.get("title", "Selection"))[:200]
        if b.get("thread"):
            key = str(b["thread"])
            if not re.fullmatch(r"(g|k|s)-[A-Za-z0-9_-]{1,80}", key):
                raise _bad("bad thread key")
            return self.thread_text(key, for_speech=True), str(b.get("title", "Conversation"))
        if b.get("run"):
            d = self._r(self.research.show, safe_run_id(b["run"]))
            return report_text(d), str(d.get("prompt", "Research report"))[:200]
        raise _bad("nothing to read")

    def r_audio_estimate(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        text, title = self._audio_text(b)
        mode = b.get("mode", "summary")
        return {
            **self.audio.estimate(text, mode),
            "title": title,
            "cached": bool(
                self.audio.cached(
                    self.audio.key(text, mode, str(b.get("voice") or self.audio.default_voice))
                )
            ),
        }

    def r_audio_make(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        b = body or {}
        text, title = self._audio_text(b)
        try:
            return self.audio.start(
                text,
                str(b.get("mode", "summary")),
                str(b.get("voice") or self.audio.default_voice),
                title,
                self.pool,
            )
        except AudioError as e:
            raise _bad(str(e)) from e

    def r_audio_job(self, q: dict, body: Any, m: re.Match[str]) -> dict:
        try:
            s = self.audio.status(m.group(1))
        except AudioError as e:
            raise _bad(str(e), 404) from e
        if s.get("result"):
            s["result"] = {k: v for k, v in s["result"].items() if k != "path"}
        return s

    def r_audio_file(self, q: dict, body: Any, m: re.Match[str]) -> Any:
        try:
            p, ctype = self.audio.file(m.group(1))
        except AudioError as e:
            raise _bad(str(e), 404) from e
        dl = (q.get("download") or [""])[0] == "1"
        return FileResponse(p, ctype, f"ultra-audio-{m.group(1)[:8]}{p.suffix}" if dl else "")
