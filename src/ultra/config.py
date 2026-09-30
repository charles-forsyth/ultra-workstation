"""Paths and configuration.

Nothing operator-specific lives in the package. Config is read from
$XDG_CONFIG_HOME/ultra-workstation/config.toml; state goes to
$XDG_DATA_HOME/ultra-workstation/. Both folders are created mode 700.
"""

from __future__ import annotations

import os
import stat
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

APP = "ultra-workstation"


def config_dir() -> Path:
    base = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP


def data_dir() -> Path:
    base = os.getenv("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / APP


def private_dir(p: Path) -> Path:
    """Create `p` (and parents) and make it mode 700."""
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o700)
    return p


def expand(p: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(p)))


def example_config_text() -> str:
    return resources.files("ultra").joinpath("config.example.toml").read_text()


def example_style_text() -> str:
    return resources.files("ultra").joinpath("style.example.toml").read_text()


def insecure(path: Path) -> bool:
    """True when a secret file or folder is readable by group or others."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))


@dataclass
class Config:
    raw: dict[str, Any] = field(default_factory=dict)
    path: Path | None = None

    def section(self, name: str) -> dict[str, Any]:
        v = self.raw.get(name)
        return v if isinstance(v, dict) else {}

    def get(self, section: str, key: str, default: Any = None) -> Any:
        return self.section(section).get(key, default)

    @property
    def port(self) -> int:
        return int(self.get("server", "port", 7440))

    @property
    def timezone(self) -> str:
        return str(self.get("operator", "timezone", "UTC"))

    @property
    def my_addresses(self) -> set[str]:
        return {str(a).lower() for a in self.get("operator", "addresses", []) or []}


def load(path: Path | None = None) -> Config:
    """Load config.toml, falling back to the shipped example (placeholders only)."""
    path = path or config_dir() / "config.toml"
    if path.exists():
        with path.open("rb") as f:
            return Config(tomllib.load(f), path)
    return Config(tomllib.loads(example_config_text()), None)


def load_env_file(path: Path | None = None) -> dict[str, str]:
    """Read KEY=VALUE lines from the app's .env without exporting them."""
    path = path or config_dir() / ".env"
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out
