"""Google credentials, one token file per capability (least privilege).

Token paths come from config ([google] token_read / token_modify / token_send /
token_calendar). They can point at token files the operator already has; Ultra only
rewrites a file to store a refreshed access token (same content shape). If a token is
missing, expired beyond refresh, or lacks the scope, AuthNeeded tells the UI exactly
which capability to re-authorize with `ultra auth google --capability X`.
"""

from __future__ import annotations

import json
import os
import threading
from typing import Any

from ultra.config import Config, expand

SCOPES = {
    "read": ["https://www.googleapis.com/auth/gmail.readonly"],
    "modify": ["https://www.googleapis.com/auth/gmail.modify"],
    "send": ["https://www.googleapis.com/auth/gmail.send"],
    "calendar": ["https://www.googleapis.com/auth/calendar"],
}
# A token with any of these scopes can serve the capability.
SATISFIES = {
    "read": {
        "https://www.googleapis.com/auth/gmail.readonly",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://mail.google.com/",
    },
    "modify": {
        "https://www.googleapis.com/auth/gmail.modify",
        "https://mail.google.com/",
    },
    "send": {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.modify",
        "https://mail.google.com/",
    },
    "calendar": {"https://www.googleapis.com/auth/calendar"},
}

_lock = threading.Lock()
_services: dict[tuple[str, str, int], Any] = {}


class AuthNeeded(Exception):
    def __init__(self, capability: str, reason: str):
        super().__init__(
            f"Google {capability} access needs authorizing ({reason}). "
            f"Run: ultra auth google --capability {capability}"
        )
        self.capability = capability


def token_path(cfg: Config, capability: str):
    v = cfg.get("google", f"token_{capability}")
    if not v:
        raise AuthNeeded(capability, "no token path in config")
    return expand(str(v))


def credentials(cfg: Config, capability: str):
    from google.auth.exceptions import RefreshError
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = token_path(cfg, capability)
    if not path.exists():
        raise AuthNeeded(capability, "token file not found")
    try:
        info = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise AuthNeeded(capability, f"token file unreadable: {e}") from e
    have = set(info.get("scopes") or [])
    if have and not have & SATISFIES[capability]:
        raise AuthNeeded(capability, "token lacks the scope")
    creds = Credentials.from_authorized_user_info(info)
    if not creds.valid:
        if not creds.refresh_token:
            raise AuthNeeded(capability, "no refresh token")
        try:
            creds.refresh(Request())
        except RefreshError as e:
            raise AuthNeeded(capability, "refresh refused (revoked or expired)") from e
        _save(path, creds.to_json())
    return creds


def _save(path, text: str) -> None:
    """Rewrite the token file atomically, keeping it owner-only."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def service(cfg: Config, api: str, version: str, capability: str):
    """A cached API client for (api, capability) PER THREAD; rebuilt when creds expire.

    The client's HTTP transport (httplib2) is not thread-safe: two threads sharing one
    client interleave on the same TLS connection and fail with SSL "record layer" or
    "NoneType has no attribute close" errors. Background jobs (item context, gather,
    refresh) and page requests run on different threads, so each thread gets its own
    client. Building one is about a millisecond (no discovery fetch)."""
    from googleapiclient.discovery import build

    key = (api, capability, threading.get_ident())
    with _lock:
        svc = _services.get(key)
        creds = getattr(svc, "_ultra_creds", None) if svc else None
        if svc is not None and creds is not None and creds.valid:
            return svc
        creds = credentials(cfg, capability)
        svc = build(api, version, credentials=creds, cache_discovery=False)
        svc._ultra_creds = creds
        _services[key] = svc
        if len(_services) > 400:  # threads come and go in pools; drop the oldest
            for k in list(_services)[:200]:
                _services.pop(k, None)
        return svc


def authorize(cfg: Config, capability: str) -> str:
    """Run the installed-app OAuth flow for one capability; returns the token path."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    client = expand(str(cfg.get("google", "oauth_client", "")))
    if not client.exists():
        raise FileNotFoundError(
            f"OAuth client file not found: {client}. Download a Desktop OAuth client "
            "JSON from Google Cloud Console and set [google] oauth_client."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(client), SCOPES[capability])
    creds = flow.run_local_server(port=0, open_browser=True)
    path = token_path(cfg, capability)
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    _save(path, creds.to_json())
    with _lock:
        for k in [k for k in _services if k[1] == capability]:
            _services.pop(k, None)
    return str(path)
