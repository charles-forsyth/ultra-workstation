"""v0.45 tests: research adapter, launcher gates, web/explain routes, audio, Range."""

from __future__ import annotations

import io
import json
import re
import stat
import threading
import urllib.request
import wave
from pathlib import Path
from typing import Any

import pytest

from ultra.audio import Audio, AudioError, chunks, speakable
from ultra.config import Config
from ultra.research import Research, ResearchError, report_text, safe_run_id
from ultra.store import Store
from ultra.tools import Tools

FAKE_DR = r"""
import json, os, sys
a = sys.argv[1:]
rec = open(os.environ["DR_LOG"], "a")
up = None
if "--upload" in a:
    p = a[a.index("--upload") + 1]
    up = {"path": p, "mode": oct(os.stat(p).st_mode & 0o777), "text": open(p).read()}
rec.write(json.dumps({"argv": a, "gemini": os.environ.get("GEMINI_API_KEY"), "upload": up}) + "\n")
rec.close()
if "--" in a and "--json" in a[a.index("--"):]:
    print("error: unrecognized arguments: --json", file=sys.stderr); sys.exit(2)
cmd = a[0]
if cmd == "search":
    print(json.dumps({"query": a[-1], "answer": "ans", "matches": [{"session_id": 3}]}))
elif cmd == "list":
    print(json.dumps([{"id": 3, "status": "completed", "prompt": "p"}]))
elif cmd == "show":
    if a[1] == "404":
        print(json.dumps({"error": "Session '404' not found."})); sys.exit(1)
    print(json.dumps({"id": a[1], "result": "## R\n\nbody", "prompt": "p"}))
elif cmd == "estimate":
    print(json.dumps({"cost_usd": 0.95, "nodes": 1}))
elif cmd == "start":
    print(json.dumps({"session_id": 42, "status": "running"}))
else:
    print("not json"); sys.exit(2)
"""


@pytest.fixture
def research(tmp_path: Path, monkeypatch) -> tuple[Research, Path]:
    b = tmp_path / "deep-research"
    b.write_text("#!/usr/bin/env python3\n" + FAKE_DR)
    b.chmod(0o755)
    log = tmp_path / "dr.jsonl"
    monkeypatch.setenv("DR_LOG", str(log))
    monkeypatch.setenv("GEMINI_API_KEY", "ultra-own-key-should-not-leak")
    cfg = Config({"research": {"binary": str(b)}})
    return Research(cfg, Store(tmp_path / "s.db")), log


def _calls(log: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in log.read_text().splitlines()]


def test_research_reads_use_json_and_end_of_options(research):
    r, log = research
    assert r.search("--help me with ceph")["answer"] == "ans"
    assert r.runs()[0]["id"] == 3
    assert report_text(r.show(3)).startswith("## R")
    c = _calls(log)
    assert c[0]["argv"] == ["search", "--limit", "5", "--json", "--", "--help me with ceph"]
    for x in c:  # --json is always an option, never part of the prompt after "--"
        a = x["argv"]
        assert "--json" in a and ("--" not in a or a.index("--json") < a.index("--"))
    assert all(x["gemini"] is None for x in c)  # our key never reaches deep-research


def test_research_error_json_becomes_exception(research):
    r, _ = research
    with pytest.raises(ResearchError, match="not found"):
        r.show(404)


def test_start_with_thread_upload_is_private_and_journaled(research):
    r, log = research
    d = r.start("How should the lab store 10 TB cheaply?", 1, 3, "From: Ben\nWe need 10 TB")
    assert d["session_id"] == 42
    c = _calls(log)[-1]
    assert c["argv"][:6] == ["start", "--depth", "1", "--breadth", "3", "--upload"]
    assert c["argv"][-3:] == ["--json", "--", "How should the lab store 10 TB cheaply?"]
    assert c["upload"]["mode"] == "0o600" and "10 TB" in c["upload"]["text"]
    j = r.store.journal_recent(3, "research_")[0]
    assert j["action"] == "research_start" and j["detail"]["thread_included"] is True
    assert "10 TB" not in json.dumps(j)  # text never journaled


def test_start_without_thread_uploads_nothing(research):
    r, log = research
    r.start("A question long enough to run", 2, 2, None)
    assert "--upload" not in _calls(log)[-1]["argv"]


@pytest.mark.parametrize(
    "args", [("short", 1, 3), ("A long enough question", 9, 3), ("A long enough question", 1, 99)]
)
def test_start_validation(research, args):
    r, log = research
    with pytest.raises(ResearchError):
        r.start(*args, None)
    assert not log.exists()


def test_only_allowed_commands(research):
    r, _ = research
    for bad in (["delete", "3"], ["cleanup"], ["auth"], ["dashboard"]):
        with pytest.raises(ResearchError):
            r._run(bad)


def test_run_id_validation():
    assert safe_run_id("12") == 12
    for bad in ("1; rm", "-1", "abc", ""):
        with pytest.raises(ResearchError):
            safe_run_id(bad)


# ---------------------------------------------------------------- route gates
class _AI:
    enabled = True

    def __init__(self) -> None:
        self.web_calls: list[tuple[str, str]] = []

    def web_search(self, q: str, context: str = "") -> dict:
        self.web_calls.append((q, context))
        return {"text": "a", "sources": [], "model": "m", "seconds": 0}


def _m(v: str):
    return re.match(r"(.+)", v)


def _tools(research, tmp_path) -> tuple[Tools, _AI]:
    ai = _AI()
    audio = Audio(Config({}), Store(tmp_path / "a.db"), type("Off", (), {"enabled": False})())
    return Tools(research, ai, audio, lambda key, for_speech=False: f"THREAD {key}"), ai


def test_start_route_needs_confirm_and_thread_is_opt_in(research, tmp_path):
    r, log = research
    tools, _ = _tools(r, tmp_path)
    with pytest.raises(Exception, match="confirm"):
        tools.r_start({}, {"prompt": "A long enough question"}, _m("x"))
    assert not log.exists()
    tools.r_start(
        {},
        {
            "prompt": "A long enough question",
            "confirm": True,
            "thread": "g-abc",
            "include_thread": False,
        },
        _m("x"),
    )
    assert "--upload" not in _calls(log)[-1]["argv"]
    tools.r_start(
        {},
        {
            "prompt": "A long enough question",
            "confirm": True,
            "thread": "g-abc",
            "include_thread": True,
        },
        _m("x"),
    )
    assert _calls(log)[-1]["upload"]["text"] == "THREAD g-abc"


def test_estimate_upload_is_removed_after(research, tmp_path):
    r, log = research
    tools, _ = _tools(r, tmp_path)
    tools.r_estimate({}, {"prompt": "Question", "thread": "g-abc", "include_thread": True}, _m("x"))
    assert not Path(_calls(log)[-1]["upload"]["path"]).exists()


def test_web_search_sends_thread_only_when_ticked(research, tmp_path):
    r, _ = research
    tools, ai = _tools(r, tmp_path)
    tools.r_web({}, {"q": "what is ceph", "thread": "g-abc"}, _m("x"))
    tools.r_web({}, {"q": "what is ceph", "thread": "g-abc", "include_thread": True}, _m("x"))
    assert ai.web_calls == [("what is ceph", ""), ("what is ceph", "THREAD g-abc")]


# ---------------------------------------------------------------- audio
def test_speakable_drops_links_quotes_and_markup():
    t = speakable(
        "## Hi\n**Bold** see https://x.org/a and [doc](https://y)\n> old quote\nmail ben@example.org"
    )
    assert "https" not in t and "old quote" not in t and "**" not in t
    assert "doc" in t and "ben" in t and "example.org" not in t


def test_chunks_respect_limit():
    text = ("Sentence one is here. " * 400).strip()
    parts = chunks(text, 1000)
    assert len(parts) > 3 and all(len(p) <= 1000 for p in parts)


class _FakeTTS:
    """Stands in for AI: summary text + a TTS client returning 0.1 s of silence."""

    enabled = True

    def __init__(self) -> None:
        self.calls = 0

    def _gen(self, prompt: str, system: str, max_tokens: int = 0, **kw: Any) -> Any:
        return type("R", (), {"text": "Spoken summary. Ben needs 10 TB by Friday."})()

    def client(self) -> Any:
        outer = self

        class Models:
            def generate_content(self, model: str, contents: str, config: Any) -> Any:
                outer.calls += 1
                buf = io.BytesIO()
                with wave.open(buf, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(24000)
                    w.writeframes(b"\x00\x00" * 2400)
                data = type("D", (), {"data": buf.getvalue(), "mime_type": "audio/wav"})()
                part = type("P", (), {"inline_data": data})()
                content = type("C", (), {"parts": [part]})()
                return type(
                    "Resp", (), {"candidates": [type("Cand", (), {"content": content})()]}
                )()

        return type("Client", (), {"models": Models()})()


def _audio(tmp_path: Path) -> tuple[Audio, _FakeTTS]:
    tts = _FakeTTS()
    cfg = Config(
        {"audio": {"voices": ["Charon", "Kore"], "price_in_per_1m": 0.5, "price_out_per_1m": 9.0}}
    )
    return Audio(cfg, Store(tmp_path / "a.db"), tts), tts


class _Pool:
    def submit(self, fn: Any, *a: Any) -> None:
        fn(*a)  # run inline


def test_audio_make_cache_and_private_file(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)  # no ffmpeg: keep WAV
    au, tts = _audio(tmp_path)
    r = au.start("Ben wrote about storage.", "summary", "Kore", "Storage", _Pool())
    s = au.status(r["job"])
    assert s["state"] == "done"
    p, ctype = au.file(r["job"])
    assert ctype == "audio/wav" and stat.S_IMODE(p.stat().st_mode) == 0o600
    assert s["result"]["script"].startswith("Spoken summary")
    n = tts.calls
    again = au.start("Ben wrote about storage.", "summary", "Kore", "Storage", _Pool())
    assert again["state"] == "done" and again["result"]["cached"] and tts.calls == n
    assert au.purge() == 1 and au.cached(r["job"]) is None


def test_audio_validation(tmp_path):
    au, _ = _audio(tmp_path)
    with pytest.raises(AudioError):
        au.start("x", "podcast", "Kore", "t", _Pool())
    with pytest.raises(AudioError):
        au.start("x", "full", "NotAVoice", "t", _Pool())
    with pytest.raises(AudioError):
        au.start("   ", "full", "Kore", "t", _Pool())
    with pytest.raises(AudioError):
        au.file("../../etc/passwd")
    est = au.estimate("word " * 300, "full")
    assert est["words"] == 300 and est["cost_usd"] is not None


def test_audio_served_with_range(tmp_path, monkeypatch):
    """End to end through the real HTTP server: 206 partial content for seeking."""
    monkeypatch.setattr("shutil.which", lambda name: None)
    from http.server import ThreadingHTTPServer

    from ultra.server import Api, make_handler

    au, _ = _audio(tmp_path)
    job = au.start("Some text to read.", "full", "Kore", "t", _Pool())["job"]
    api = Api.__new__(Api)  # bare route table: no demo or live adapters
    api.routes, api.demo, api.live, api.token = [], True, None, "t"
    Tools(None, None, au, lambda k, for_speech=False: "").register(api)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(api, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(api, port)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/audio/file/{job}",
            headers={"Host": f"127.0.0.1:{port}", "Range": "bytes=0-99"},
        )
        with urllib.request.urlopen(req) as r:  # noqa: S310 - fixed http://127.0.0.1 URL
            assert r.status == 206
            assert r.headers["Content-Range"].startswith("bytes 0-99/")
            assert len(r.read()) == 100
            assert r.headers["Accept-Ranges"] == "bytes"
    finally:
        httpd.shutdown()
