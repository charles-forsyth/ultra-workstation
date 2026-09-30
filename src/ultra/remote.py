"""Remote access (--host 0.0.0.0): which client addresses may connect.

The policy admits this machine, any configured network (default 192.168 LANs), and the
operator's own tailnet devices. "tailnet:mine" is not a range: it is the set of
addresses of devices owned by the same Tailscale user as this machine, read from
`tailscale status --json`. Shared-in nodes and other users' devices are refused. The
set refreshes when an unknown tailnet address knocks (at most once a minute), so a
newly added device of the operator's works without a restart.
"""

from __future__ import annotations

import ipaddress
import json
import socket
import subprocess
import threading
import time
from collections.abc import Callable
from typing import Any

from ultra import guard

TAILNET_MINE = "tailnet:mine"
DEFAULT_POLICY = ("192.168.0.0/16", TAILNET_MINE)
TAILNET_RANGES = guard.parse_networks(["100.64.0.0/10", "fd7a:115c:a1e0::/48"])
REFRESH_SECONDS = 60.0


def _ip(addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip


def own_tailnet_ips(status: dict[str, Any]) -> dict[str, str]:
    """IP -> host name for this machine and peers owned by the same Tailscale user."""
    me = status.get("Self") or {}
    uid = me.get("UserID")
    if uid is None:
        return {}
    nodes = [me] + [
        p
        for p in (status.get("Peer") or {}).values()
        if p.get("UserID") == uid and not p.get("ShareeNode")
    ]
    out: dict[str, str] = {}
    for n in nodes:
        for a in n.get("TailscaleIPs") or []:
            out[str(a)] = str(n.get("HostName") or "?")
    return out


def tailscale_status() -> dict[str, Any]:
    try:
        r = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        data = json.loads(r.stdout)
        return data if isinstance(data, dict) else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}


class PeerPolicy:
    """Client-address check for the HTTP handler: loopback, networks, own tailnet."""

    def __init__(
        self,
        specs: list[str] | tuple[str, ...],
        status_fn: Callable[[], dict[str, Any]] = tailscale_status,
    ) -> None:
        self.specs = list(specs)
        self.tailnet = TAILNET_MINE in self.specs
        self.networks = guard.parse_networks([s for s in self.specs if s != TAILNET_MINE])
        self._status_fn = status_fn
        self._lock = threading.Lock()
        self._own: dict[str, str] = {}
        self._checked = 0.0
        if self.tailnet:
            self.refresh()

    def __bool__(self) -> bool:
        return bool(self.networks) or self.tailnet

    def refresh(self) -> dict[str, str]:
        with self._lock:
            self._own = own_tailnet_ips(self._status_fn())
            self._checked = time.monotonic()
            return dict(self._own)

    def own(self) -> dict[str, str]:
        return dict(self._own)

    def __call__(self, addr: str) -> bool:
        if guard.peer_allowed(addr, self.networks):
            return True
        if not self.tailnet:
            return False
        ip = _ip(addr)
        if ip is None or not any(ip.version == n.version and ip in n for n in TAILNET_RANGES):
            return False
        if str(ip) in self._own:
            return True
        if time.monotonic() - self._checked >= REFRESH_SECONDS:  # maybe a new device
            self.refresh()
            return str(ip) in self._own
        return False


def policy_from_config(specs: list[str] | None) -> PeerPolicy:
    return PeerPolicy(specs or list(DEFAULT_POLICY))


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
