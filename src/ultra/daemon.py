"""Background server process: start, stop, restart, status (PID file)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from ultra.config import data_dir, private_dir

CHILD_BOOT = "from ultra.cli import main; main()"


def pid_file() -> Path:
    return data_dir() / "ultra.pid"


def log_file() -> Path:
    return data_dir() / "ultra.log"


def _alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_state() -> dict | None:
    try:
        state = json.loads(pid_file().read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict) or not _alive(int(state.get("pid", 0))):
        return None
    return state


def probe(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def start(port: int, demo: bool = False, host: str = "127.0.0.1") -> int:
    state = read_state()
    if state:
        print(f"[INFO] Ultra already running (PID {state['pid']}) on port {state['port']}.")
        return 0
    if probe(port):
        print(f"[ERROR] Something is already answering on port {port}.")
        return 1
    private_dir(data_dir())
    cmd = [
        sys.executable,
        "-I",
        "-u",
        "-c",
        CHILD_BOOT,
        "serve",
        "--port",
        str(port),
        "--host",
        host,
    ]
    if demo:
        cmd.append("--demo")
    fd = os.open(log_file(), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as log:
        proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(data_dir()),
        )
    pid_file().write_text(json.dumps({"pid": proc.pid, "port": port, "demo": demo, "host": host}))
    for _ in range(40):
        if proc.poll() is not None:
            pid_file().unlink(missing_ok=True)
            print(f"[ERROR] Ultra exited during startup (code {proc.returncode}).")
            print(f"        See {log_file()}")
            return 1
        if probe(port):
            print(f"[INFO] Ultra started (PID {proc.pid}): http://127.0.0.1:{port}")
            if host not in ("127.0.0.1", "localhost", "::1"):
                print(f"[WARN] Listening on {host}: other devices need the access key.")
                print("       Run `ultra remote-key` for the link to open on them.")
            return 0
        time.sleep(0.25)
    print(f"[WARN] Ultra PID {proc.pid} started but is not answering yet.")
    return 1


def stop() -> int:
    state = read_state()
    if not state:
        pid_file().unlink(missing_ok=True)
        print("[INFO] Ultra is not running.")
        return 0
    pid = int(state["pid"])
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    except PermissionError:
        os.kill(pid, signal.SIGTERM)
    for _ in range(40):
        if not _alive(pid):
            break
        time.sleep(0.125)
    pid_file().unlink(missing_ok=True)
    print(f"[INFO] Ultra stopped (PID {pid}).")
    return 0


def status() -> int:
    state = read_state()
    if not state:
        print("[INFO] Ultra is not running. Start it with: ultra start")
        return 3
    health = probe(int(state["port"]))
    host = state.get("host", "127.0.0.1")
    print(
        f"[INFO] Ultra running (PID {state['pid']}) on http://127.0.0.1:{state['port']}"
        f" - {'healthy' if health else 'NOT answering'}"
        + (" (demo)" if state.get("demo") else "")
        + (f" (remote: bound to {host})" if host not in ("127.0.0.1", "localhost", "::1") else "")
    )
    return 0 if health else 1
