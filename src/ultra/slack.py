"""Slack adapter, read side (v0.2), through Claude Code's Slack connector.

Runs `claude -p` with read-only Slack tools allowed and every write tool, Bash, Edit
and Write disallowed. ANTHROPIC_API_KEY is removed from the child environment
because it disables the claude.ai connectors (reads would then look empty). The
output must be a JSON array; anything else is an error, never "no messages".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from ultra.config import Config, expand
from ultra.store import Store

READ_TOOLS = (
    "slack_search_public_and_private",
    "slack_read_channel",
    "slack_read_thread",
    "slack_search_users",
    "slack_read_user_profile",
    "slack_search_channels",
)
DENY_TOOLS = (
    "slack_send_message",
    "slack_send_message_draft",
    "slack_schedule_message",
    "slack_add_reaction",
    "slack_create_canvas",
    "slack_update_canvas",
)
LOOKBACK_H = 96


class SlackError(Exception):
    pass


class Slack:
    def __init__(self, cfg: Config, store: Store):
        self.cfg = cfg
        self.store = store
        s = cfg.section("slack")
        self.binary = shutil.which(str(s.get("claude_binary", "claude"))) or ""
        self.enabled = bool(s.get("enabled", False)) and bool(self.binary)
        self.prefix = str(s.get("tool_prefix", "mcp__claude_ai_Slack__"))
        self.cwd = str(expand(str(s.get("claude_cwd", "~"))))
        self.me = str(cfg.get("operator", "slack_user_id", ""))
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"ok": None, "error": "", "at": 0.0, "busy": False}

    def _cmd(self, prompt: str) -> list[str]:
        allow = ",".join(self.prefix + t for t in READ_TOOLS)
        deny = (
            ",".join(self.prefix + t for t in DENY_TOOLS)
            + ",Bash,Edit,Write,NotebookEdit"
        )
        return [
            self.binary,
            "-p",
            prompt,
            "--allowedTools",
            allow,
            "--disallowedTools",
            deny,
            "--permission-mode",
            "dontAsk",
        ]

    def _prompt(self, since: datetime) -> str:
        who = f"The operator's Slack user ID is {self.me}. " if self.me else ""
        return (
            "READ-ONLY. Never send, draft, schedule, react, or edit anything in Slack. "
            f"{who}Find every Slack message from someone OTHER than the operator, "
            f"posted after {since.strftime('%Y-%m-%d %H:%M %Z')}, that is (a) a direct "
            "message to the operator, (b) in a group DM that includes the operator, or "
            "(c) an @mention of the operator anywhere, including replies in threads "
            "the operator is in. Search with is:dm and with the operator's mention, "
            "AND read "
            "the most recent group DMs directly, because mention search misses some. "
            "Skip bots and messages the operator wrote. Output ONLY a JSON array, no "
            'prose, no code fences. Each item: {"channel_id": "...", "channel": '
            '"<channel or DM name>", "ts": "<message ts>", "thread_ts": "<thread '
            'ts or empty>", "from": "<sender name>", "from_email": "<sender '
            'email if known, else empty>", "text": "<first 300 chars>", '
            '"permalink": "<url or empty>", "needs_me": true|false}. needs_me is '
            "true when the message asks the operator something, assigns them "
            "something, "
            "or @mentions them. If there are none, output []."
        )

    def refresh(self) -> list[dict[str, Any]]:
        if not self.enabled:
            raise SlackError("Slack is not enabled (set [slack] enabled = true)")
        if not self.lock.acquire(blocking=False):
            raise SlackError("A Slack refresh is already running")
        self.state["busy"] = True
        try:
            since = datetime.now().astimezone() - timedelta(hours=LOOKBACK_H)
            env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            r = subprocess.run(
                self._cmd(self._prompt(since)),
                cwd=self.cwd,
                env=env,
                capture_output=True,
                text=True,
                timeout=420,
                check=False,
                stdin=subprocess.DEVNULL,
            )
            out = (r.stdout or "").strip()
            if "connectors are disabled" in out + r.stderr:
                raise SlackError("Slack unreachable: claude.ai connectors are disabled")
            m = re.search(r"\[\s*(?:\{.*\}\s*)?\]", out, re.S)
            if r.returncode != 0 or not m:
                raise SlackError(
                    f"Slack read failed (exit {r.returncode}): "
                    f"{(out or r.stderr)[:200]}"
                )
            items = json.loads(m.group(0))
            if not isinstance(items, list):
                raise SlackError("Slack read returned something other than a list")
            self.store.cache_put("slack:items", items)
            self.state = {"ok": True, "error": "", "at": time.time(), "busy": False}
            return items
        except SlackError as e:
            self.state = {
                "ok": False,
                "error": str(e),
                "at": time.time(),
                "busy": False,
            }
            raise
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            self.state = {
                "ok": False,
                "error": str(e),
                "at": time.time(),
                "busy": False,
            }
            raise SlackError(str(e)) from e
        finally:
            self.state["busy"] = False
            self.lock.release()

    def items(self) -> tuple[list[dict[str, Any]], float | None]:
        hit = self.store.cache_get("slack:items")
        return (hit[0], hit[1]) if hit else ([], None)

    @staticmethod
    def to_stream(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Slack messages grouped per conversation, as stream rows."""
        convs: dict[str, dict[str, Any]] = {}
        for it in items:
            try:
                ts = float(it.get("ts") or 0)
            except ValueError:
                ts = 0.0
            key = str(it.get("channel_id") or it.get("channel") or "slack")
            thread = str(it.get("thread_ts") or "")
            ck = f"{key}:{thread}" if thread else key
            row = convs.get(ck)
            ms = int(ts * 1000)
            if row is None or ms > row["ts_ms"]:
                convs[ck] = {
                    "key": "s-" + re.sub(r"[^A-Za-z0-9_-]", "_", ck),
                    "source": "slack",
                    "from": it.get("from") or "",
                    "addr": (it.get("from_email") or "").lower(),
                    "subject": it.get("channel") or "Slack",
                    "snippet": (it.get("text") or "")[:200],
                    "ts": datetime.fromtimestamp(ts).astimezone().isoformat()
                    if ts
                    else "",
                    "ts_ms": ms,
                    "count": (row or {}).get("count", 0) + 1,
                    "court": "MINE" if it.get("needs_me") else "FYI",
                    "reason": "asks or mentions you" if it.get("needs_me") else "FYI",
                    "badges": [],
                    "waiting_days": 0,
                    "permalink": it.get("permalink") or "",
                    "messages": ((row or {}).get("messages") or []) + [it],
                }
            else:
                row["count"] += 1
                row["messages"].append(it)
                if it.get("needs_me"):
                    row["court"] = "MINE"
        return list(convs.values())
