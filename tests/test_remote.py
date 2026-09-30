"""--host 0.0.0.0: only 192.168 LANs and the operator's own tailnet devices get in."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from typing import Any

import pytest

from ultra import guard
from ultra.config import Config
from ultra.remote import TAILNET_MINE, PeerPolicy, own_tailnet_ips
from ultra.server import build

ME = 111
STATUS: dict[str, Any] = {
    "Self": {
        "UserID": ME,
        "HostName": "workbox",
        "TailscaleIPs": ["100.64.0.10", "fd7a:115c:a1e0::10"],
    },
    "Peer": {
        "k1": {"UserID": ME, "HostName": "phone", "TailscaleIPs": ["100.64.0.20"]},
        "k2": {"UserID": 222, "HostName": "friend", "TailscaleIPs": ["100.64.0.30"]},
        "k3": {
            "UserID": ME,
            "HostName": "shared",
            "TailscaleIPs": ["100.64.0.40"],
            "ShareeNode": True,
        },
    },
}


def _policy(status: dict[str, Any] | None = None) -> PeerPolicy:
    return PeerPolicy(
        ["192.168.0.0/16", TAILNET_MINE], lambda: status if status is not None else STATUS
    )


def test_host_allow_list_by_mode():
    assert guard.host_allowed("127.0.0.1:7440", 7440)
    assert not guard.host_allowed("192.0.2.10:7440", 7440)  # loopback mode
    for ok in (
        "192.0.2.10:7440",
        "100.64.0.10:7440",
        "workbox:7440",
        "workbox.tail1234.ts.net:7440",
        "box.local:7440",
    ):
        assert guard.host_allowed(ok, 7440, remote=True), ok
    for bad in ("evil.example.com:7440", "attacker.org", "192.0.2.10:9999"):
        assert not guard.host_allowed(bad, 7440, remote=True), bad


def test_own_tailnet_ips_only_same_user():
    own = own_tailnet_ips(STATUS)
    assert own == {
        "100.64.0.10": "workbox",
        "fd7a:115c:a1e0::10": "workbox",
        "100.64.0.20": "phone",
    }
    assert own_tailnet_ips({}) == {}  # tailscale down: nobody from the tailnet


def test_policy_admits_lan_and_own_devices_only():
    p = _policy()
    for ok in (
        "100.64.0.10",
        "100.64.0.20",
        "::ffff:100.64.0.20",
        "fd7a:115c:a1e0::10",
        "192.168.0.5",
        "192.168.1.200",
        "::ffff:192.168.1.9",
    ):
        assert p(ok), ok
    for bad in (
        "100.64.0.30",  # another user's device
        "100.64.0.40",  # shared-in node
        "100.64.0.99",  # unknown tailnet address
        "100.127.255.254",
        "10.0.0.5",
        "172.17.0.2",
        "8.8.8.8",
        "192.169.0.1",
        "fe80::1",
        "not-an-ip",
    ):
        assert not p(bad), bad


def test_unknown_tailnet_address_refreshes_at_most_once_a_minute(monkeypatch):
    calls = {"n": 0}
    status = {"Self": {"UserID": ME, "TailscaleIPs": ["100.64.0.10"]}, "Peer": {}}

    def fn() -> dict[str, Any]:
        calls["n"] += 1
        return status

    p = PeerPolicy([TAILNET_MINE], fn)
    assert calls["n"] == 1 and not p("100.64.0.50")  # within a minute: no refresh
    assert calls["n"] == 1
    status["Peer"] = {"n": {"UserID": ME, "HostName": "new", "TailscaleIPs": ["100.64.0.50"]}}
    import ultra.remote as rm

    monkeypatch.setattr(rm, "REFRESH_SECONDS", 0.0)
    assert p("100.64.0.50") and calls["n"] == 2  # new own device admitted after refresh
    assert not p("10.0.0.1") and calls["n"] == 2  # non-tailnet addresses never trigger it


def test_empty_policy_refused_for_remote_bind():
    with pytest.raises(ValueError):
        build(Config({}), 0, demo=True, host="0.0.0.0", policy=PeerPolicy([]))  # noqa: S104


def _get(url: str, headers: dict) -> int:
    try:
        req = urllib.request.Request(url, headers=headers)  # noqa: S310 - local test URL
        with urllib.request.urlopen(req) as r:  # noqa: S310
            return int(r.status)
    except urllib.error.HTTPError as e:
        return int(e.code)


def _lan_ip() -> str | None:
    from ultra.remote import addresses

    return next((a for a in addresses() if a.startswith("192.168.")), None)


def test_real_socket_lan_peer_allowed_and_refused():
    lan = _lan_ip()
    if not lan:
        pytest.skip("no 192.168 address on this machine")
    tail_only = PeerPolicy([TAILNET_MINE], lambda: STATUS)
    for pol, want in ((_policy(), 200), (tail_only, 403)):
        httpd, _ = build(Config({}), 0, demo=True, host="0.0.0.0", policy=pol)  # noqa: S104
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            assert _get(f"http://{lan}:{port}/api/health", {"Host": f"{lan}:{port}"}) == want
            if want == 200:  # a public host name is refused even from an allowed address
                assert (
                    _get(f"http://{lan}:{port}/api/health", {"Host": f"evil.example.com:{port}"})
                    == 403
                )
            assert _get(f"http://127.0.0.1:{port}/api/health", {"Host": f"127.0.0.1:{port}"}) == 200
        finally:
            httpd.shutdown()
            httpd.server_close()
