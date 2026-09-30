"""--host 0.0.0.0: remote devices need the access key; public names are refused."""

from __future__ import annotations

import stat
import threading
import urllib.error
import urllib.request

import pytest

from ultra import guard
from ultra.config import Config
from ultra.remote import access_key
from ultra.server import build


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


def test_remote_key_check():
    assert guard.remote_key_ok("a=1; ultra_key=SECRET", None, "SECRET")
    assert guard.remote_key_ok(None, "SECRET", "SECRET")
    assert not guard.remote_key_ok("ultra_key=WRONG", "", "SECRET")
    assert not guard.remote_key_ok(None, None, "")  # no key configured: never open


def test_access_key_file_is_private_and_stable():
    k1 = access_key()
    assert len(k1) >= 32 and access_key() == k1
    from ultra.remote import key_file

    assert stat.S_IMODE(key_file().stat().st_mode) == 0o600
    assert access_key(rotate=True) != k1


def test_non_loopback_bind_requires_key():
    with pytest.raises(ValueError):
        build(Config({}), 0, demo=True, host="0.0.0.0", remote_key="")  # noqa: S104


def _get(url: str, headers: dict) -> tuple[int, dict, bytes]:
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers=headers)) as r:  # noqa: S310
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_remote_client_flow_on_real_socket():
    """A non-loopback peer: 401 without key, 303 + cookie with ?key=, then 200."""
    from ultra.remote import addresses

    lan = next((a for a in addresses() if a.count(".") == 3 and not a.startswith("127.")), None)
    if not lan:
        pytest.skip("no LAN or tailnet address on this machine")
    key = "K" * 40
    httpd, _ = build(Config({}), 0, demo=True, host="0.0.0.0", remote_key=key)  # noqa: S104
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        base = f"http://{lan}:{port}"
        h = {"Host": f"{lan}:{port}"}
        assert _get(base + "/api/health", h)[0] == 401
        code, hdrs, _ = _get(base + f"/?key={key}", h)
        assert code == 303 and hdrs["Location"] == "/"
        assert "HttpOnly" in hdrs["Set-Cookie"] and "SameSite=Strict" in hdrs["Set-Cookie"]
        code, _, body = _get(base + "/api/health", {**h, "Cookie": f"ultra_key={key}"})
        assert code == 200 and b'"ok": true' in body
        # a public host name is refused even with the key (DNS rebinding)
        code, _, _ = _get(
            base + "/api/health", {"Host": f"evil.example.com:{port}", "Cookie": f"ultra_key={key}"}
        )
        assert code == 403
        # this machine still works without any key
        assert _get(f"http://127.0.0.1:{port}/api/health", {"Host": f"127.0.0.1:{port}"})[0] == 200
    finally:
        httpd.shutdown()
