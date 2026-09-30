"""Remote access helpers (--host 0.0.0.0).

Access is limited by client address in guard.peer_allowed (Tailscale range and 192.168
LANs by default); this module only lists the addresses other devices can use.
"""

from __future__ import annotations

import socket
import subprocess


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
