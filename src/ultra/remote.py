"""Remote access (--host 0.0.0.0): an access key, so only the operator's devices get in.

Ultra has the operator's mail, send and ledger write powers, so network access is
never open. The key lives in the private data folder (mode 600), is generated once,
and is presented by a remote browser as a cookie after visiting the link that
`ultra remote-key` prints. `ultra remote-key --rotate` invalidates every device.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
from pathlib import Path

from ultra.config import data_dir, private_dir


def key_file() -> Path:
    return data_dir() / "remote.key"


def access_key(rotate: bool = False) -> str:
    p = key_file()
    if p.exists() and not rotate:
        k = p.read_text().strip()
        if len(k) >= 32:
            return k
    private_dir(data_dir())
    k = secrets.token_urlsafe(32)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(k + "\n")
    os.chmod(p, 0o600)
    return k


def addresses() -> list[str]:
    """Names and IPs another device might use: tailnet first, then LAN."""
    out: list[str] = []
    try:
        ts = subprocess.run(
            ["tailscale", "ip", "-4"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        out += [x.strip() for x in ts.stdout.splitlines() if x.strip()]
    except (OSError, subprocess.SubprocessError):
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("192.0.2.1", 9))  # no packet is sent; picks the LAN interface
        out.append(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    out.append(socket.gethostname())
    return list(dict.fromkeys(out))
