"""CLI, config and doctor tests. Everything runs in temp XDG folders."""

from __future__ import annotations

import os
import stat

import pytest

from ultra import cli, config, doctor


@pytest.fixture
def xdg(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return tmp_path


def test_config_init_writes_private_files_and_never_overwrites(xdg, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["config", "init"])
    assert e.value.code == 0
    d = config.config_dir()
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    cfg = d / "config.toml"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    cfg.write_text('[operator]\nname = "Mine"\n')
    with pytest.raises(SystemExit):
        cli.main(["config", "init"])
    assert "Mine" in cfg.read_text()  # untouched
    assert "left unchanged" in capsys.readouterr().out


def test_load_falls_back_to_example(xdg):
    c = config.load()
    assert c.path is None and c.port == 7440
    assert c.my_addresses == {"ada@example.org"}


def test_example_config_has_no_real_data():
    text = config.example_config_text() + config.example_style_text()
    assert "example.org" in text
    assert "@" not in text.replace("@example.org", "").replace("@service-now", "")


def test_example_config_regexes_actually_match():
    """Regexes in the TOML examples are literal strings, so they really match."""
    import re
    import tomllib

    c = tomllib.loads(config.example_config_text())
    assert re.search(c["tickets"]["sender_patterns"][0], "desk@service-now.com")
    assert all(
        re.search(p, n)
        for p, n in zip(
            c["tickets"]["number_patterns"],
            ["RITM0352361", "INC0388724", "SCTASK0000001", "REQ0000001"],
            strict=True,
        )
    )
    assert re.search(c["court"]["done_signal_patterns"][0], "the project is ready")
    s = tomllib.loads(config.example_style_text())
    assert re.search(s["forbid"][0]["pattern"], "Sorry for the slow reply")


def test_env_file_parsing(xdg):
    d = config.private_dir(config.config_dir())
    (d / ".env").write_text("# c\nGEMINI_API_KEY='abc123'\nBAD LINE\n")
    assert config.load_env_file() == {"GEMINI_API_KEY": "abc123"}


def test_doctor_flags_world_readable_env_and_masks_key(xdg):
    d = config.private_dir(config.config_dir())
    (d / "config.toml").write_text(config.example_config_text())
    env = d / ".env"
    env.write_text("GEMINI_API_KEY=AIzaSECRETVALUE9876\n")
    os.chmod(env, 0o644)
    checks = {c.name: c for c in doctor.run(config.load())}
    assert checks["perms .env"].ok is False
    assert checks["gemini key"].detail == "set (...9876)"
    assert "SECRET" not in " ".join(c.detail for c in checks.values())
    os.chmod(env, 0o600)
    checks = {c.name: c for c in doctor.run(config.load())}
    assert checks["perms .env"].ok is True


def test_help_lists_commands():
    text = cli.build_parser().format_help()
    for c in ("start", "stop", "status", "open", "serve", "doctor", "config"):
        assert c in text
    assert cli.build_parser().format_usage().startswith("usage: ultra")
