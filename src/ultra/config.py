"""Paths and configuration.

Nothing operator-specific lives in the package. Config is read from
$XDG_CONFIG_HOME/ultra-workstation/config.toml; state goes to
$XDG_DATA_HOME/ultra-workstation/. Both folders are created mode 700.

Workspaces (v1.11, SPEC 7.14): the folders above are the main workspace. Each other
workspace lives in `workspaces/<slug>/` under both folders, with its own config.toml,
style.toml, tokens and state. A Config knows its own folders (`config_home`,
`data_home`), so nothing a workspace reads or writes lands in another one's.
"""

from __future__ import annotations

import os
import re
import stat
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

APP = "ultra-workstation"
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
COLORS = ("cyan", "green", "amber", "magenta", "red")


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
    # a workspace other than the main one: its own folders (None = the main folders)
    home: Path | None = None
    data: Path | None = None
    slug: str = ""

    @property
    def config_home(self) -> Path:
        return self.home or config_dir()

    @property
    def data_home(self) -> Path:
        return self.data or data_dir()

    @property
    def style_path(self) -> Path:
        return self.config_home / "style.toml"

    @property
    def workspace(self) -> dict[str, str]:
        """slug, name and colour of this config's workspace."""
        w = self.section("workspace")
        slug = self.slug or "main"
        color = str(w.get("color") or ("cyan" if not self.slug else "green"))
        return {
            "slug": slug,
            "name": str(w.get("name") or ("Main" if not self.slug else slug.title()))[:24],
            "color": color if color in COLORS else "cyan",
        }

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


def workspace_home(slug: str) -> Path:
    return config_dir() / "workspaces" / slug


def load_workspace(slug: str) -> Config:
    """A workspace's config, with its own folders. Raises KeyError if there is none."""
    if not SLUG_RE.match(slug or "") or slug == "main":
        raise KeyError(f"bad workspace name: {slug!r}")
    home = workspace_home(slug)
    path = home / "config.toml"
    if not path.exists():
        raise KeyError(f"no workspace {slug!r} ({path} not found)")
    with path.open("rb") as f:
        raw = tomllib.load(f)
    return Config(raw, path, home=home, data=data_dir() / "workspaces" / slug, slug=slug)


def list_workspaces(main: Config) -> list[dict[str, Any]]:
    """The main workspace first, then every workspaces/<slug>/config.toml, by name."""
    out: list[dict[str, Any]] = [{**main.workspace, "main": True}]
    root = config_dir() / "workspaces"
    if root.is_dir():
        extra = []
        for d in sorted(root.iterdir()):
            if d.is_dir() and SLUG_RE.match(d.name) and d.name != "main":
                try:
                    extra.append({**load_workspace(d.name).workspace, "main": False})
                except (KeyError, OSError, tomllib.TOMLDecodeError):
                    continue
        out += sorted(extra, key=lambda w: w["name"].lower())
    return out


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
