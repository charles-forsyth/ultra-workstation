"""v1.2.1: a ticket card replies like an email thread (its newest notice), Ref line kept."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "src/ultra/static/app.js").read_text()


def test_ticket_card_uses_its_newest_thread_for_reply_and_studio():
    assert 'const mailKey = it.key.startsWith("g-") ? it.key : (it.source === "ticket"' in JS
    assert "openDraft(k, mailKey)" in JS  # reply / reply all / forward on the notice thread
    assert "studioStart(mailKey)" in JS  # Draft Studio gathers the notice thread
    assert "function newestThread(it, t)" in JS
    assert 'title="Email threads only for now"' in JS  # Slack keeps its own reply path


def test_ticket_messages_carry_their_thread_id(tmp_path):
    from ultra.config import Config
    from ultra.live import Live
    from ultra.store import Store

    live = Live(Config({"ledger": {"enabled": False}}), Store(tmp_path / "s.db"))
    live.store.cache_put(
        "mail:stream",
        {"items": [{"key": "k-RITM0000001", "source": "ticket", "threads": ["aaa111", "bbb222"]}]},
    )
    threads = {
        "aaa111": {"messages": [{"id": "m1", "ts": "2026-09-27T10:00:00-04:00", "body": "old"}]},
        "bbb222": {"messages": [{"id": "m2", "ts": "2026-09-28T10:00:00-04:00", "body": "new"}]},
    }
    live.mail.thread = lambda tid: threads[tid]  # type: ignore[method-assign]
    out = live.r_ticket({}, None, re.match(r"(.+)", "RITM0000001"))  # type: ignore[arg-type]
    assert [(m["id"], m["thread_id"]) for m in out["messages"]] == [
        ("m1", "aaa111"),
        ("m2", "bbb222"),
    ]


def test_reply_all_on_the_notice_thread_keeps_desk_and_ref_over_http_demo():
    import http.client

    from ultra.config import Config
    from ultra.server import build

    httpd, api = build(Config({}), 0, demo=True)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:

        def req(method: str, path: str, body: Any = None) -> tuple[int, Any]:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            h = {"Host": f"127.0.0.1:{port}"}
            data = None
            if body is not None:
                data = json.dumps(body)
                h["Content-Type"] = "application/json"
                h["X-Ultra-Token"] = api.token
            c.request(method, path, data, h)
            r = c.getresponse()
            return r.status, json.loads(r.read() or b"null")

        s, t = req("GET", "/api/thread/k-300")
        assert s == 200 and t["messages"][0]["thread_id"] == "300"
        s, d = req("POST", "/api/drafts", {"kind": "reply_all", "thread": "g-300"})
        assert s == 200
        v = d["current"]
        assert "desk@service-now.example" in v["to_addrs"] and "eli@example.org" in v["cc"]
        assert v["subject"].startswith("Re: Request Item RITM0000001")
        assert d["ticket_ref"] == "Ref:MSG00012345"
        s, d = req("POST", f"/api/drafts/{d['id']}/fix-ref", {})
        assert s == 200 and d["current"]["body"].rstrip().endswith("Ref:MSG00012345")
    finally:
        httpd.shutdown()
        httpd.server_close()
