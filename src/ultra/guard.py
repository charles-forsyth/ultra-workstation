"""Request guards for a loopback-only app.

Adapted from the deep-research dashboard (MIT, same author): loopback peer check,
Host allow-list against DNS rebinding, Origin check and JSON-only writes, plus a
per-start CSRF token that every write must carry.
"""

from __future__ import annotations

import hmac
import ipaddress
import secrets
from urllib.parse import urlparse

CSRF_HEADER = "X-Ultra-Token"

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; frame-src 'self'; "
    "img-src 'self' data:; media-src 'self'; connect-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
)


def new_token() -> str:
    return secrets.token_urlsafe(32)


def loopback_peer(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:  # ::ffff:127.0.0.1
        ip = ip.ipv4_mapped
    return ip.is_loopback


def split_host(host_header: str) -> tuple[str, int | None]:
    h = (host_header or "").strip().lower()
    if h.startswith("["):  # [::1]:7440
        end = h.find("]")
        host, rest = h[1:end], h[end + 1 :]
        port = rest[1:] if rest.startswith(":") else ""
    elif h.count(":") == 1:
        host, port = h.split(":")
    else:
        host, port = h, ""
    try:
        return host, int(port) if port else None
    except ValueError:
        return host, -1


def host_allowed(host_header: str, port: int) -> bool:
    """Only localhost names on our own port: blocks DNS rebinding."""
    host, p = split_host(host_header)
    if host not in {"127.0.0.1", "localhost", "::1"}:
        return False
    return p in (None, port)


def origin_allowed(origin: str | None, host_header: str) -> bool:
    if not origin:
        return True  # same-origin fetches without Origin (older browsers, curl)
    return urlparse(origin).netloc.lower() == (host_header or "").lower()


def token_ok(sent: str | None, expected: str) -> bool:
    return bool(sent) and hmac.compare_digest(str(sent), expected)
