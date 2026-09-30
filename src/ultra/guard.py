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


# Names that only resolve on a LAN or tailnet. A DNS-rebinding page is served from a
# public domain, so its requests carry that domain in Host (rule from deep-research).
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".localdomain", ".internal", ".ts.net")


def host_allowed(host_header: str, port: int, remote: bool = False) -> bool:
    """Loopback mode: only localhost names on our port. Remote mode (--host 0.0.0.0):
    also IP literals, bare machine names and LAN/tailnet names. Public DNS names are
    always refused, which blocks DNS rebinding."""
    host, p = split_host(host_header)
    if p not in (None, port):
        return False
    if host in {"127.0.0.1", "localhost", "::1"}:
        return True
    if not remote:
        return False
    host = host.rstrip(".")
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    return bool(host) and ("." not in host or host.endswith(LOCAL_SUFFIXES))


REMOTE_COOKIE = "ultra_key"


def remote_key_ok(cookie_header: str | None, query_key: str | None, expected: str) -> bool:
    """Remote clients must present the access key, as a cookie or once in ?key=."""
    if not expected:
        return False
    if query_key and hmac.compare_digest(query_key, expected):
        return True
    for part in (cookie_header or "").split(";"):
        name, _, value = part.strip().partition("=")
        if name == REMOTE_COOKIE and value and hmac.compare_digest(value, expected):
            return True
    return False


def origin_allowed(origin: str | None, host_header: str) -> bool:
    if not origin:
        return True  # same-origin fetches without Origin (older browsers, curl)
    return urlparse(origin).netloc.lower() == (host_header or "").lower()


def token_ok(sent: str | None, expected: str) -> bool:
    return bool(sent) and hmac.compare_digest(str(sent), expected)
