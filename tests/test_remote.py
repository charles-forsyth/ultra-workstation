"""--host 0.0.0.0: only allowed client networks get in; public names are refused."""

from __future__ import annotations

import threading
import urllib.error
import urllib.request

import pytest

from ultra import guard
from ultra.config import Config
from ultra.server import build

NETS = guard.parse_networks(list(guard.DEFAULT_REMOTE_NETWORKS))


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


def test_peer_allow_list():
    for ok in (
        "127.0.0.1",
        "::1",
        "::ffff:127.0.0.1",
        "100.64.0.1",
        "100.127.255.254",
        "192.168.0.5",
        "192.168.1.200",
        "::ffff:192.168.1.9",
    ):
        assert guard.peer_allowed(ok, NETS), ok
    for bad in (
        "10.0.0.5",
        "172.17.0.2",
        "100.63.255.255",
        "100.128.0.1",
        "8.8.8.8",
        "192.169.0.1",
        "fe80::1",
        "not-an-ip",
    ):
        assert not guard.peer_allowed(bad, NETS), bad
    assert not guard.peer_allowed("192.168.1.9", ())  # loopback mode: nothing remote
    assert guard.peer_allowed("127.0.0.1", ())


def test_config_can_narrow_networks():
    httpd, _ = build(
        Config({"server": {"remote_networks": ["100.64.0.0/10"]}}),
        0,
        demo=True,
        host="127.0.0.2",
    )  # loopback host: no remote set
    httpd.server_close()
    with pytest.raises(ValueError):
        build(Config({}), 0, demo=True, host="0.0.0.0", networks=())  # noqa: S104


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
    for nets, want in ((NETS, 200), (guard.parse_networks(["100.64.0.0/10"]), 403)):
        httpd, _ = build(Config({}), 0, demo=True, host="0.0.0.0", networks=nets)  # noqa: S104
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            assert _get(f"http://{lan}:{port}/api/health", {"Host": f"{lan}:{port}"}) == want
            # a public host name is refused even from an allowed address (DNS rebinding)
            if want == 200:
                assert (
                    _get(
                        f"http://{lan}:{port}/api/health",
                        {"Host": f"evil.example.com:{port}"},
                    )
                    == 403
                )
            assert _get(f"http://127.0.0.1:{port}/api/health", {"Host": f"127.0.0.1:{port}"}) == 200
        finally:
            httpd.shutdown()
            httpd.server_close()
