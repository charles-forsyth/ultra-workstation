"""Checks the setup without printing any secret: config, file modes, tools, keys."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass

from ultra.config import Config, config_dir, data_dir, expand, insecure, load_env_file


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = False


def _tool_version(binary: str, args: list[str]) -> tuple[bool, str]:
    path = shutil.which(binary)
    if not path:
        return False, "not found on PATH"
    try:
        r = subprocess.run([path, *args], capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"failed to run: {e}"
    text = (r.stdout or r.stderr).strip()
    first = text.splitlines()[0] if text else f"exit {r.returncode}"
    return r.returncode == 0, first


def run(cfg: Config) -> list[Check]:
    out: list[Check] = []
    cfg_path = config_dir() / "config.toml"
    out.append(
        Check(
            "config",
            cfg_path.exists(),
            str(cfg_path) if cfg_path.exists() else "missing (run: ultra config init)",
            required=True,
        )
    )
    for p in (config_dir(), data_dir(), config_dir() / ".env", config_dir() / "tokens"):
        if p.exists():
            out.append(
                Check(
                    f"perms {p.name or p}",
                    not insecure(p),
                    "ok (owner only)" if not insecure(p) else "readable by others: chmod 700/600",
                    required=True,
                )
            )
    for cap in ("token_read", "token_modify", "token_send", "token_calendar"):
        v = cfg.get("google", cap)
        if not v:
            continue
        p = expand(str(v))
        if not p.exists():
            out.append(Check(cap, False, "not authorized yet (ultra auth google)"))
            continue
        scopes: list[str] = []
        try:
            scopes = json.loads(p.read_text()).get("scopes") or []
        except (OSError, ValueError):
            pass
        out.append(
            Check(
                cap,
                not insecure(p),
                (", ".join(s.rsplit("/", 1)[-1] for s in scopes) or "present")
                + ("" if not insecure(p) else "; file readable by others"),
            )
        )
    env = load_env_file()
    key = env.get("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY", "")
    out.append(
        Check(
            "gemini key",
            bool(key),
            f"set (...{key[-4:]})" if key else "not set (AI features off)",
        )
    )
    if cfg.get("ledger", "enabled", True):
        ok, d = _tool_version(str(cfg.get("ledger", "binary", "nexus")), ["--help"])
        out.append(Check("ledger (nexus)", ok, "found" if ok else d))
    if cfg.get("research", "enabled", True):
        binary = str(cfg.get("research", "binary", "deep-research"))
        ok, d = _tool_version(binary, ["--version"])
        if ok:
            ok2, d2 = _tool_version(binary, ["estimate", "x", "--json"])
            ok = ok and ok2 and d2.startswith("{")
            d = d + ("" if ok else " (no --json: needs deep-research 0.36.0+)")
        out.append(Check("research (deep-research)", ok, d))
    if cfg.get("slack", "enabled", False):
        claude = str(cfg.get("slack", "claude_binary", "claude"))
        ok, d = _tool_version(claude, ["--version"])
        note = d
        if os.getenv("ANTHROPIC_API_KEY"):
            note += "; ANTHROPIC_API_KEY is set here, Ultra removes it for Slack calls"
        out.append(Check("slack (claude)", ok, note))
    if cfg.get("hermes", "enabled", True):
        ok, d = _tool_version(str(cfg.get("hermes", "binary", "hermes")), ["--version"])
        out.append(Check("hermes (Ask Hermes)", ok, d))
    out += mcp_checks(cfg)
    ff = shutil.which("ffmpeg")
    out.append(Check("ffmpeg", bool(ff), "found (MP3 audio)" if ff else "missing (WAV only)"))
    return out


# Oldest server releases whose tool shapes Ultra reads (SPEC 8.8)
MIN_NEXUS_TOOLS_VERSION = 7
MIN_URSA_VERSION = (0, 9, 0)


def _ver(v: str) -> tuple[int, ...]:
    import re

    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def mcp_checks(cfg: Config) -> list[Check]:
    """One line per configured MCP server: reachable, version, signed in, as whom."""
    from ultra import mcpclient

    out: list[Check] = []
    for name in mcpclient.SERVERS:
        if not mcpclient.configured(cfg, name):
            continue
        client = mcpclient.client_from_config(cfg, name)
        if client is None:
            out.append(Check(f"mcp {name}", True, "disabled in config"))
            continue
        try:
            h = client.health()
        except mcpclient.McpError as e:
            out.append(Check(f"mcp {name}", False, str(e)))
            continue
        if name == "nexus":
            tv = int(str(h.get("tools_version") or 0) or 0)
            vok, vtxt = tv >= MIN_NEXUS_TOOLS_VERSION, f"tools_version {tv}"
        else:
            vv = str(h.get("version") or "")
            vok, vtxt = _ver(vv) >= MIN_URSA_VERSION, vv or "unknown version"
        if not client.signed_in():
            out.append(Check(f"mcp {name}", False, f"{vtxt}; not signed in (ultra auth {name})"))
            continue
        try:
            who = client.whoami()
        except mcpclient.McpError as e:
            out.append(Check(f"mcp {name}", False, f"{vtxt}; {e}"))
            continue
        role = who.get("role") or ",".join(who.get("tiers") or [])
        detail = f"{vtxt}; {who.get('email', '?')} ({role})"
        if who.get("calls_per_min"):
            detail += f", {who['calls_per_min']}/min"
        if not vok:
            detail += "; server too old for Ultra"
        if not insecure(client.tokens.path):
            out.append(Check(f"mcp {name}", vok, detail))
        else:
            out.append(Check(f"mcp {name}", False, detail + "; token file readable by others"))
    return out
