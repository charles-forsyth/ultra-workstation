"""Test isolation: every test runs with temp config/data folders and no API keys."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    # Never reach a real `nexus serve` running on this machine: point the ledger's
    # serve client at a token file that does not exist (tests that exercise serve set
    # their own). Without this, ledger tests read real ledger data.
    import ultra.ledger_serve as ls

    monkeypatch.setattr(ls, "DEFAULT_TOKEN_FILE", str(tmp_path / "no-serve.token"))
    monkeypatch.setattr(ls, "DEFAULT_URL", "http://127.0.0.1:9")
